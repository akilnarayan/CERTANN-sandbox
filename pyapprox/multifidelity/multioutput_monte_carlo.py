import copy
import warnings
from abc import abstractmethod
from functools import partial
from itertools import combinations
from multiprocessing import Pool

import torch
import numpy as np
from scipy.optimize import minimize

from pyapprox.util.utilities import get_correlation_from_covariance
from pyapprox.multifidelity.stats import (
    MultiOutputMean, MultiOutputVariance, MultiOutputMeanAndVariance,
    _nqoi_nqoi_subproblem)
from pyapprox.multifidelity._visualize import (
    _plot_allocation_matrix, _plot_model_recursion)
from pyapprox.multifidelity._optim import (
    _allocate_samples_mlmc,
    _allocate_samples_mfmc,
    _check_mfmc_model_costs_and_correlations,
    _cast_to_integers,
    _get_sample_allocation_matrix_mlmc,
    _get_sample_allocation_matrix_mfmc,
    _get_acv_recursion_indices)
from pyapprox.surrogates.autogp._torch_wrappers import asarray


def _combine_acv_values(reorder_allocation_mat, npartition_samples,
                        acv_values):
    r"""
    Extract the unique values from the sets
    :math:`f_\alpha(\mathcal{Z}_\alpha), `f_\alpha(\mathcal{Z}_\alpha^*)`
    for each model :math:`\alpha=0,\ldots,M`
    """
    nmodels = len(acv_values)
    values_per_model = [None for ii in range(nmodels)]
    values_per_model[0] = acv_values[0][1]
    for ii in range(1, nmodels):
        lb, ub = 0, 0
        lb2, ub2 = 0, 0
        values_per_model[ii] = []
        for jj in range(nmodels):
            found = False
            if reorder_allocation_mat[jj, 2*ii] == 1:
                ub = lb + int(npartition_samples[jj])
                values_per_model[ii] += [acv_values[ii][0][lb:ub]]
                lb = ub
                found = True
            if reorder_allocation_mat[jj, 2*ii+1] == 1:
                # there is no need to enter here is samle set has already
                # been added by acv_values[ii][0], hence the use of elseif here
                ub2 = lb2 + int(npartition_samples[jj])
                if not found:
                    values_per_model[ii] += [acv_values[ii][1][lb2:ub2]]
                lb2 = ub2
        values_per_model[ii] = np.vstack(values_per_model[ii])
    return values_per_model


def _combine_acv_samples(reorder_allocation_mat, npartition_samples,
                         acv_samples):
    r"""
    Extract the unique amples from the sets
:math:`\mathcal{Z}_\alpha, `\mathcal{Z}_\alpha^*` for each model
    :math:`\alpha=0,\ldots,M`
    """
    nmodels = len(acv_samples)
    samples_per_model = [None for ii in range(nmodels)]
    samples_per_model[0] = acv_samples[0][1]
    for ii in range(1, nmodels):
        lb, ub = 0, 0
        lb2, ub2 = 0, 0
        samples_per_model[ii] = []
        for jj in range(nmodels):
            found = False
            if reorder_allocation_mat[jj, 2*ii] == 1:
                ub = lb + int(npartition_samples[jj])
                samples_per_model[ii] += [acv_samples[ii][0][:, lb:ub]]
                lb = ub
                found = True
            if reorder_allocation_mat[jj, 2*ii+1] == 1:
                ub2 = lb2 + int(npartition_samples[jj])
                if not found:
                    # Only add samples if they were not in Z_m^*
                    samples_per_model[ii] += [acv_samples[ii][1][:, lb2:ub2]]
                lb2 = ub2
        samples_per_model[ii] = np.hstack(samples_per_model[ii])
    return samples_per_model


def _get_allocation_matrix_gmf(recursion_index):
    nmodels = len(recursion_index)+1
    mat = np.zeros((nmodels, 2*nmodels))
    for ii in range(nmodels):
        mat[ii, 2*ii+1] = 1.0
    for ii in range(1, nmodels):
        mat[:, 2*ii] = mat[:, recursion_index[ii-1]*2+1]
    for ii in range(2, 2*nmodels):
        II = np.where(mat[:, ii] == 1)[0][-1]
        mat[:II, ii] = 1.0
    return mat


def _get_allocation_matrix_acvis(recursion_index):
    nmodels = len(recursion_index)+1
    mat = np.zeros((nmodels, 2*nmodels))
    for ii in range(nmodels):
        mat[ii, 2*ii+1] = 1
    for ii in range(1, nmodels):
        mat[:, 2*ii] = mat[:, recursion_index[ii-1]*2+1]
    for ii in range(1, nmodels):
        mat[:, 2*ii+1] = np.maximum(mat[:, 2*ii], mat[:, 2*ii+1])
    return mat


def _get_allocation_matrix_acvrd(recursion_index):
    nmodels = len(recursion_index)+1
    allocation_mat = np.zeros((nmodels, 2*nmodels))
    for ii in range(nmodels):
        allocation_mat[ii, 2*ii+1] = 1
    for ii in range(1, nmodels):
        allocation_mat[:, 2*ii] = (
            allocation_mat[:, recursion_index[ii-1]*2+1])
    return allocation_mat


def log_determinant_variance(variance):
    #reg = 1e-10*torch.eye(variance.shape[0], dtype=torch.double)
    val = torch.logdet(variance)
    return val


def determinant_variance(variance):
    return torch.det(variance)


def log_trace_variance(variance):
    val = torch.log(torch.trace(variance))
    if not torch.isfinite(val):
        raise RuntimeError("trace is negative")
    return val


def log_linear_combination_diag_variance(weights, variance):
    # must be used with partial, e.g.
    # opt_criteria = partial(log_linear_combination_diag_variance, weights)
    return torch.log(torch.multi_dot(weights, torch.diag(variance)))


class MCEstimator():
    def __init__(self, stat, costs, cov, opt_criteria=None):
        r"""
        Parameters
        ----------
        stat : :class:`~pyapprox.multifidelity.multioutput_monte_carlo.MultiOutputStatistic`
            Object defining what statistic will be calculated

        costs : np.ndarray (nmodels)
            The relative costs of evaluating each model

        cov : np.ndarray (nmodels*nqoi, nmodels)
            The covariance C between each of the models. The highest fidelity
            model is the first model, i.e. covariance between its QoI
            is cov[:nqoi, :nqoi]

        opt_criteria : callable
            Function of the the covariance between the high-fidelity
            QoI estimators with signature

            ``opt_criteria(variance) -> float

            where variance is np.ndarray with size that depends on
            what statistics are being estimated. E.g. when estimating means
            then variance shape is (nqoi, nqoi), when estimating variances
            then variance shape is (nqoi**2, nqoi**2), when estimating mean
            and variance then shape (nqoi+nqoi**2, nqoi+nqoi**2)
        """
        # public variables (will be backwards compatible)
        self._stat = stat

        # private variables (no guarantee that these variables
        #                    will exist in the future)
        self._cov, self._costs, self._nmodels, self._nqoi = self._check_cov(
            cov, costs)
        self._optimization_criteria = self._set_optimization_criteria(
            opt_criteria)

        self._rounded_nsamples_per_model = None
        self._rounded_npartition_samples = None
        self._rounded_target_cost = None
        self._optimized_criteria = None
        self._optimized_covariance = None
        self._model_labels = None

    def _check_cov(self, cov, costs):
        nmodels = len(costs)
        if cov.shape[0] % nmodels:
            msg = "cov.shape {0} and costs.shape {1} are inconsistent".format(
                cov.shape, costs.shape)
            raise ValueError(msg)
        return (torch.as_tensor(cov, dtype=torch.double).clone(),
                torch.as_tensor(costs, dtype=torch.double),
                nmodels, cov.shape[0]//nmodels)

    def _set_optimization_criteria(self, opt_criteria):
        if opt_criteria is None:
            # opt_criteria = log_determinant_variance
            opt_criteria = log_trace_variance
        return opt_criteria

    def _covariance_from_npartition_samples(self, npartition_samples):
        """
        Get the variance of the Monte Carlo estimator from costs and cov
        and npartition_samples
        """
        return self._stat.high_fidelity_estimator_covariance(
            npartition_samples[0])

    def optimized_covariance(self):
        """
        Return the estimator covariance at the optimal sample allocation
        computed using self.allocate_samples()
        """
        return self._optimized_covariance

    def allocate_samples(self, target_cost, verbosity=0):
        self._rounded_nsamples_per_model = np.asarray(
            [int(np.floor(target_cost/self._costs[0]))])
        self._rounded_npartition_samples = self._rounded_nsamples_per_model
        est_covariance = self._covariance_from_npartition_samples(
            self._rounded_npartition_samples)
        self._optimized_covariance = est_covariance
        optimized_criteria = self._optimization_criteria(est_covariance)
        self._rounded_target_cost = (
            self._costs[0]*self._rounded_nsamples_per_model[0])
        self._optimized_criteria = optimized_criteria

    def generate_samples_per_model(self, rvs):
        """
        Returns the samples needed to the model

        Parameters
        ----------
        rvs : callable
            Function with signature

            `rvs(nsamples)->np.ndarray(nvars, nsamples)`

        Returns
        -------
        samples_per_model : list[np.ndarray] (1)
            List with one entry np.ndarray (nvars, nsamples_per_model[0])
        """
        return [rvs(self._rounded_nsamples_per_model)]

    def __call__(self, values):
        if not isinstance(values, np.ndarray):
            raise ValueError(
                "values must be an np.ndarray but type={0}".format(
                    type(values)))
        if ((values.ndim != 2) or
                (values.shape[0] != self._rounded_nsamples_per_model[0])):
            msg = "values has the incorrect shape {0} expected {1}".format(
                values.shape,
                (self._rounded_nsamples_per_model[0], self._nqoi))
            raise ValueError(msg)
        return self._stat.sample_estimate(values)

    def bootstrap(self, values, nbootstraps=1000):
        r"""
        Approximate the variance of the estimator using
        bootstraping. The accuracy of bootstapping depends on the number
        of values per model. As it gets large the boostrapped statistics
        will approach the theoretical values.

        Parameters
        ----------
        values : [np.ndarray(nsamples, nqoi)]
            A single entry list containing the unique values of each model.
            The list is required to allow consistent interface with 
            multi-fidelity estimators

        nbootstraps : integer
            The number of boostraps used to compute estimator variance

        Returns
        -------
        bootstrap_stats : float
            The bootstrap estimate of the estimator

        bootstrap_covar : float
            The bootstrap estimate of the estimator covariance
        """
        nbootstraps = int(nbootstraps)
        estimator_vals = np.empty((nbootstraps, self._stat._nqoi))
        nsamples = values[0].shape[0]
        indices = np.arange(nsamples)
        for kk in range(nbootstraps):
            bootstrapped_indices = np.random.choice(
                indices, size=nsamples, replace=True)
            estimator_vals[kk] = self._stat.sample_estimate(
                values[0][bootstrapped_indices])
        bootstrap_mean = estimator_vals.mean(axis=0)
        bootstrap_covar = np.cov(estimator_vals, rowvar=False, ddof=1)
        return bootstrap_mean, bootstrap_covar

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}(stat={1}, nqoi={2})".format(
                self.__class__.__name__, self._stat, self._nqoi)
        rep = "{0}(stat={1}, criteria={2:.3g}".format(
            self.__class__.__name__, self._stat, self._optimized_criteria)
        rep += " target_cost={0:.5g}, nsamples={1})".format(
            self._rounded_target_cost,
            self._rounded_nsamples_per_model[0])
        return rep


class CVEstimator(MCEstimator):
    def __init__(self, stat, costs, cov, lowfi_stats=None, opt_criteria=None):
        super().__init__(stat, costs, cov, opt_criteria=opt_criteria)
        self._lowfi_stats = lowfi_stats

        self._optimized_CF = None
        self._optimized_cf = None
        self._optimized_weights = None

    def _get_discrepancy_covariances(self, npartition_samples):
        return self._stat._get_cv_discrepancy_covariances(npartition_samples)

    def _covariance_from_npartition_samples(self, npartition_samples):
        CF, cf = self._get_discrepancy_covariances(npartition_samples)
        weights = self._weights(CF, cf)
        return (self._stat.high_fidelity_estimator_covariance(
            npartition_samples[0]) + torch.linalg.multi_dot((weights, cf.T)))

    def _set_optimized_params_base(self, rounded_npartition_samples,
                                   rounded_nsamples_per_model,
                                   rounded_target_cost):
        r"""
        Set the parameters needed to generate samples for evaluating the
        estimator

        Parameters
        ----------
        rounded_npartition_samples : np.ndarray (npartitions, dtype=int)
            The number of samples in the independent sample partitions.

        rounded_nsamples_per_model :  np.ndarray (nmodels)
            The number of samples allocated to each model

        rounded_target_cost : float
            The cost of the new sample allocation

        Sets attributes
        ----------------
        self._rounded_target_cost : float
            The computational cost of the estimator using the rounded
            npartition_samples

        self._rounded_npartition_samples :  np.ndarray (npartitions)
            The number of samples in each partition corresponding to the
            rounded partition_ratios

        self._rounded_nsamples_per_model :  np.ndarray (nmodels)
            The number of samples allocated to each model

        self._optimized_covariance : np.ndarray (nstats, nstats)
            The optimal estimator covariance

        self._optimized_criteria: float
            The value of the sample allocation objective using the rounded
            partition_ratios

        self._rounded_nsamples_per_model : np.ndarray (nmodels)
            The number of samples allocated to each model using the rounded
            partition_ratios

        self._optimized_CF : np.ndarray (nstats*(nmodels-1),nstats*(nmodels-1))
            The covariance between :math:`\Delta_i`, :math:`\Delta_j`

        self._optimized_cf : np.ndarray (nstats, nstats*(nmodels-1))
            The covariance between :math:`Q_0`, :math:`\Delta_j`

        self._optimized_weights : np.ndarray (nstats, nmodels-1)
            The optimal control variate weights
        """
        self._rounded_npartition_samples = rounded_npartition_samples
        self._rounded_nsamples_per_model = rounded_nsamples_per_model
        self._rounded_target_cost = rounded_target_cost
        self._optimized_covariance = self._covariance_from_npartition_samples(
            self._rounded_npartition_samples)
        self._optimized_criteria = self._optimization_criteria(
            self._optimized_covariance)
        self._optimized_CF, self._optimized_cf = (
            self._get_discrepancy_covariances(
                self._rounded_npartition_samples))
        self._optimized_weights = self._weights(
            self._optimized_CF, self._optimized_cf)

    def _estimator_cost(self, npartition_samples):
        return (npartition_samples[0]*self._costs).sum()

    def _set_optimized_params(self, rounded_npartition_samples):
        rounded_target_cost = self._estimator_cost(rounded_npartition_samples)
        self._set_optimized_params_base(
            rounded_npartition_samples, rounded_npartition_samples,
            rounded_target_cost)

    def allocate_samples(self, target_cost):
        npartition_samples = [target_cost/self._costs.sum()]
        rounded_npartition_samples = [int(np.floor(npartition_samples[0]))]
        if isinstance(self._stat,
                      (MultiOutputVariance, MultiOutputMeanAndVariance)):
            min_nhf_samples = 2
        else:
            min_nhf_samples = 1
        if rounded_npartition_samples[0] < min_nhf_samples:
            msg = "target_cost is to small. Not enough samples of each model"
            msg += " can be taken {0} < {1}".format(
                npartition_samples[0], min_nhf_samples)
            raise ValueError(msg)

        rounded_nsamples_per_model = np.full(
            (self._nmodels,), rounded_npartition_samples[0])
        rounded_target_cost = (
            self._costs*rounded_nsamples_per_model).sum()
        self._set_optimized_params_base(
            rounded_npartition_samples, rounded_nsamples_per_model,
            rounded_target_cost)

    def generate_samples_per_model(self, rvs):
        """
        Returns the samples needed to the model

        Parameters
        ----------
        rvs : callable
            Function with signature

            `rvs(nsamples)->np.ndarray(nvars, nsamples)`

        Returns
        -------
        samples_per_model : list[np.ndarray] (1)
            List with one entry np.ndarray (nvars, nsamples_per_model[0])
        """
        samples = rvs(self._rounded_nsamples_per_model[0])
        return [samples.copy() for ii in range(self._nmodels)]

    def _weights(self, CF, cf):
        return -torch.linalg.multi_dot(
            (torch.linalg.pinv(CF), cf.T)).T
        # try:
        #     direct solve is usually not a good idea because of ill
        #     conditioning which can be larges especially for mean_variance
        #
        #     return -torch.linalg.solve(CF, cf.T).T
        # except (torch._C._LinAlgError):
        #     return -torch.linalg.multi_dot(
        #         (torch.linalg.pinv(CF), cf.T)).T
        # try:
        #     weights = -torch.linalg.multi_dot(
        #         (torch.linalg.pinv(CF), cf.T))
        # except (np.linalg.LinAlgError, RuntimeError):
        #     weights = torch.ones(cf.T.shape, dtype=torch.double)*1e16
        # return weights.T

    @staticmethod
    def _covariance_non_optimal_weights(
            hf_est_covar, weights, CF, cf):
        # The expression below, e.g. Equation 8
        # from Dixon 2024, can be used for non optimal control variate weights
        # Warning: Even though this function is general,
        # it should only ever be used for MLMC, because
        # expression for optimal weights is more efficient
        return (
            hf_est_covar
            + torch.linalg.multi_dot((weights, CF, weights.T))
            + torch.linalg.multi_dot((cf, weights.T))
            + torch.linalg.multi_dot((weights, cf.T))
        )

    def _estimate(self, values_per_model, weights, bootstrap=False):
        if len(values_per_model) != self._nmodels:
            print(len(self._lowfi_stats), self._nmodels)
            msg = "Must provide the values for each model."
            msg += " {0} != {1}".format(len(values_per_model), self._nmodels)
            raise ValueError(msg)
        nsamples = values_per_model[0].shape[0]
        for values in values_per_model[1:]:
            if values.shape[0] != nsamples:
                msg = "Must provide the same number of samples for each model"
                raise ValueError(msg)
        indices = np.arange(nsamples)
        if bootstrap:
            indices = np.random.choice(
                indices, size=indices.shape[0],replace=True)
            
        deltas = np.hstack(
            [self._stat.sample_estimate(values_per_model[ii][indices]) -
             self._lowfi_stats[ii-1] for ii in range(1, self._nmodels)])
        est = (self._stat.sample_estimate(values_per_model[0][indices]) +
               weights.numpy().dot(deltas))
        return est

    def __call__(self, values_per_model):
        r"""
        Return the value of the Monte Carlo like estimator

        Parameters
        ----------
        values_per_model : list (nmodels)
            The unique values of each model

        Returns
        -------
        est : np.ndarray (nqoi, nqoi)
            The covariance of the estimator values for
            each high-fidelity model QoI
        """
        return self._estimate(values_per_model, self._optimized_weights)

    def insert_pilot_values(self, pilot_values, values_per_model):
        """
        Only add pilot values to the fist indepedent partition and thus
        only to models that use that partition
        """
        new_values_per_model = []
        for ii in range(self._nmodels):
            active_partition = ((self._allocation_mat[0, 2*ii] == 1) or 
                               (self._allocation_mat[0, 2*ii+1] == 1))
            if active_partition:
                new_values_per_model.append(np.vstack((
                    pilot_values[ii], values_per_model[ii])))
            else:
                new_values_per_model.append(values_per_model[ii].copy())
        return new_values_per_model

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}(stat={1}, recursion_index={2})".format(
                self.__class__.__name__, self._stat, self._recursion_index)
        rep = "{0}(stat={1}, criteria={2:.3g}".format(
            self.__class__.__name__, self._stat, self._optimized_criteria)
        rep += " target_cost={0:.5g}, nsamples={1})".format(
            self._rounded_target_cost,
            self._rounded_nsamples_per_model[0])
        return rep

    def bootstrap(self, values_per_model, nbootstraps=1000):
        r"""
        Approximate the variance of the estimator using
        bootstraping. The accuracy of bootstapping depends on the number
        of values per model. As it gets large the boostrapped statistics
        will approach the theoretical values.

        Parameters
        ----------
        values_per_model : list (nmodels)
            The unique values of each model

        nbootstraps : integer
            The number of boostraps used to compute estimator variance

        Returns
        -------
        bootstrap_stats : float
            The bootstrap estimate of the estimator

        bootstrap_covar : float
            The bootstrap estimate of the estimator covariance
        """
        nbootstraps = int(nbootstraps)
        estimator_vals = np.empty((nbootstraps, self._stat._nqoi))
        for kk in range(nbootstraps):
            estimator_vals[kk] = self._estimate(
                values_per_model, self._optimized_weights, bootstrap=True)
        bootstrap_mean = estimator_vals.mean(axis=0)
        bootstrap_covar = np.cov(estimator_vals, rowvar=False, ddof=1)
        return bootstrap_mean, bootstrap_covar


class ACVEstimator(CVEstimator):
    def __init__(self, stat, costs, cov,
                 recursion_index=None, opt_criteria=None,
                 tree_depth=None, allow_failures=False):
        """
        Constructor.

        Parameters
        ----------
        stat : :class:`~pyapprox.multifidelity.multioutput_monte_carlo.MultiOutputStatistic`
            Object defining what statistic will be calculated

        costs : np.ndarray (nmodels)
            The relative costs of evaluating each model

        cov : np.ndarray (nmodels*nqoi, nmodels)
            The covariance C between each of the models. The highest fidelity
            model is the first model, i.e. covariance between its QoI
            is cov[:nqoi, :nqoi]

        recursion_index : np.ndarray (nmodels-1)
            The recusion index that specifies which ACV estimator is used

        opt_criteria : callable
            Function of the the covariance between the high-fidelity
            QoI estimators with signature

            ``opt_criteria(variance) -> float

            where variance is np.ndarray with size that depends on
            what statistics are being estimated. E.g. when estimating means
            then variance shape is (nqoi, nqoi), when estimating variances
            then variance shape is (nqoi**2, nqoi**2), when estimating mean
            and variance then shape (nqoi+nqoi**2, nqoi+nqoi**2)

        tree_depth: integer (default=None)
            The maximum depth of the recursion tree.
            If not None, then recursion_index is ignored.

        allow_failures: boolean (default=False)
            Allow optimization of estimators to fail when enumerating
            each recursion tree. This is useful for estimators, like MFMC,
            that have optimization that enforce constraints on the structure
            of the model ensemble
        """
        super().__init__(stat, costs, cov, None, opt_criteria=opt_criteria)
        self._set_initial_guess(None)

        if tree_depth is not None and recursion_index is not None:
            msg = "Only tree_depth or recurusion_index must be specified"
            raise ValueError(msg)
        if tree_depth is None:
            self._set_recursion_index(recursion_index)
        self._tree_depth = tree_depth
        self._allow_failures = allow_failures

        self._rounded_partition_ratios = None

    def _get_discrepancy_covariances(self, npartition_samples):
        return self._stat._get_acv_discrepancy_covariances(
            self._get_allocation_matrix(), npartition_samples)

    @staticmethod
    def _get_partition_indices(npartition_samples):
        """
        Get the indices, into the flattened array of all samples/values,
        of each indpendent sample partition
        """
        ntotal_independent_samples = npartition_samples.sum()
        total_indices = torch.arange(ntotal_independent_samples)
        # round the cumsum to make sure values like 3.9999999999999999
        # do not get rounded down to 3
        indices = np.split(
            total_indices,
            np.round(np.cumsum(npartition_samples.numpy()[:-1])).astype(int))
        return [torch.as_tensor(idx, dtype=int) for idx in indices]

    def _get_partition_indices_per_acv_subset(self, bootstrap=False):
        r"""
        Get the indices, into the flattened array of all samples/values
        for each model, of each acv subset
        :math:`\mathcal{Z}_\alpha,\mathcal{Z}_\alpha^*`
        """
        partition_indices = self._get_partition_indices(
            self._rounded_npartition_samples)
        if bootstrap:
            npartitions = len(self._rounded_npartition_samples)
            random_partition_indices = [
                None for jj in range(npartitions)]
            random_partition_indices[0] = np.random.choice(
                np.arange(partition_indices[0].shape[0], dtype=int),
                size=partition_indices[0].shape[0], replace=True)
            partition_indices_per_acv_subset = [
                np.array([], dtype=int),
                partition_indices[0][random_partition_indices[0]]]
        else:
            partition_indices_per_acv_subset = [
                np.array([], dtype=int), partition_indices[0]]
        for ii in range(1, self._nmodels):
            active_partitions = np.where(
                (self._allocation_mat[:, 2*ii] == 1) |
                (self._allocation_mat[:, 2*ii+1] == 1))[0]
            subset_indices = [None for ii in range(self._nmodels)]
            lb, ub = 0, 0
            for idx in active_partitions:
                ub += partition_indices[idx].shape[0]
                subset_indices[idx] = np.arange(lb, ub)
                if bootstrap:
                    if random_partition_indices[idx] is None:
                        # make sure the same random permutation for partition
                        # idx is used for all acv_subsets
                        random_partition_indices[idx] = np.random.choice(
                            np.arange(ub-lb, dtype=int), size=ub-lb,
                            replace=True)
                    subset_indices[idx] = (
                        subset_indices[idx][random_partition_indices[idx]])
                lb = ub
            active_partitions_1 = np.where(
                (self._allocation_mat[:, 2*ii] == 1))[0]
            active_partitions_2 = np.where(
                (self._allocation_mat[:, 2*ii+1] == 1))[0]
            indices_1 = np.hstack(
                [subset_indices[idx] for idx in active_partitions_1])
            indices_2 = np.hstack(
                [subset_indices[idx] for idx in active_partitions_2])
            partition_indices_per_acv_subset += [indices_1, indices_2]
        return partition_indices_per_acv_subset

    def _partition_ratios_to_model_ratios(self, partition_ratios):
        """
        Convert the partition ratios defining the number of samples per
        partition relative to the number of samples in the
        highest-fidelity model partition
        to ratios defining the number of samples per mdoel
        relative to the number of highest-fidelity model samples
        """
        model_ratios = torch.empty_like(partition_ratios, dtype=torch.double)
        for ii in range(1, self._nmodels):
            active_partitions = np.where(
                (self._allocation_mat[1:, 2*ii] == 1) |
                (self._allocation_mat[1:, 2*ii+1] == 1))[0]
            model_ratios[ii-1] = partition_ratios[active_partitions].sum()
            if ((self._allocation_mat[0, 2*ii] == 1) or
                    (self._allocation_mat[0, 2*ii+1] == 1)):
                model_ratios[ii-1] += 1
        return model_ratios

    def _get_num_high_fidelity_samples_from_partition_ratios(
            self, target_cost, partition_ratios):
        model_ratios = self._partition_ratios_to_model_ratios(partition_ratios)
        return target_cost/(
            self._costs[0]+(model_ratios*self._costs[1:]).sum())

    def _npartition_samples_from_partition_ratios(
            self, target_cost, partition_ratios):
        nhf_samples = (
            self._get_num_high_fidelity_samples_from_partition_ratios(
                target_cost, partition_ratios))
        npartition_samples = torch.empty(
            partition_ratios.shape[0]+1, dtype=torch.double)
        npartition_samples[0] = nhf_samples
        npartition_samples[1:] = partition_ratios*nhf_samples
        return npartition_samples

    def _covariance_from_partition_ratios(self, target_cost, partition_ratios):
        """
        Get the variance of the Monte Carlo estimator from costs and cov
        and nsamples ratios. Needed for optimization.

        Parameters
        ----------
        target_cost : float
            The total cost budget

        partition_ratios : np.ndarray (nmodels-1)
            The sample ratios r used to specify the number of samples
            in the indepedent sample partitions

        Returns
        -------
        variance : float
            The variance of the estimator
            """
        npartition_samples = self._npartition_samples_from_partition_ratios(
            target_cost, partition_ratios)
        return self._covariance_from_npartition_samples(npartition_samples)

    def _separate_values_per_model(self, values_per_model, bootstrap=False):
        r"""
        Seperate values per model into the acv subsets associated with
        :math:`\mathcal{Z}_\alpha,\mathcal{Z}_\alpha^*`
        """
        if len(values_per_model) != self._nmodels:
            msg = "len(values_per_model) {0} != nmodels {1}".format(
                len(values_per_model), self._nmodels)
            raise ValueError(msg)
        for ii in range(self._nmodels):
            if (values_per_model[ii].shape[0] !=
                    self._rounded_nsamples_per_model[ii]):
                msg = "{0} != {1}".format(
                    "len(values_per_model[{0}]): {1}".format(
                        ii, values_per_model[ii].shape[0]),
                    "nsamples_per_model[ii]: {0}".format(
                        self._rounded_nsamples_per_model[ii]))
                raise ValueError(msg)

        acv_partition_indices = self._get_partition_indices_per_acv_subset(
            bootstrap)
        nacv_subsets = len(acv_partition_indices)
        # atleast_2d is needed for when acv_partition_indices[ii].shape[0] == 1
        # in this case python automatically reduces the values array from
        # shape (1, N) to (N)
        acv_values = [
            np.atleast_2d(values_per_model[ii//2][acv_partition_indices[ii]])
            for ii in range(nacv_subsets)]
        return acv_values

    def _separate_samples_per_model(self, samples_per_model):
        if len(samples_per_model) != self._nmodels:
            msg = "len(samples_per_model) {0} != nmodels {1}".format(
                len(samples_per_model), self._nmodels)
            raise ValueError(msg)
        for ii in range(self._nmodels):
            if (samples_per_model[ii].shape[1] !=
                    self._rounded_nsamples_per_model[ii]):
                msg = "{0} != {1}".format(
                    "len(samples_per_model[{0}]): {1}".format(
                        ii, samples_per_model[ii].shape[0]),
                    "nsamples_per_model[ii]: {0}".format(
                        self._rounded_nsamples_per_model[ii]))
                raise ValueError(msg)

        acv_partition_indices = self._get_partition_indices_per_acv_subset()
        nacv_subsets = len(acv_partition_indices)
        acv_samples = [
            samples_per_model[ii//2][:, acv_partition_indices[ii]]
            for ii in range(nacv_subsets)]
        return acv_samples

    def generate_samples_per_model(self, rvs, npilot_samples=0):
        ntotal_independent_samples = (
            self._rounded_npartition_samples.sum()-npilot_samples)
        independent_samples = rvs(ntotal_independent_samples)
        samples_per_model = []
        rounded_npartition_samples = self._rounded_npartition_samples.clone()
        if npilot_samples > rounded_npartition_samples[0]:
            raise ValueError(
                "npilot_samples is larger than optimized first partition size")
        rounded_npartition_samples[0] -= npilot_samples
        rounded_nsamples_per_model = self._compute_nsamples_per_model(
            rounded_npartition_samples)
        partition_indices = self._get_partition_indices(
            rounded_npartition_samples)
        for ii in range(self._nmodels):
            active_partitions = np.where(
                (self._allocation_mat[:, 2*ii] == 1) |
                (self._allocation_mat[:, 2*ii+1] == 1))[0]
            indices = np.hstack(
                [partition_indices[idx] for idx in active_partitions])
            if indices.shape[0] != rounded_nsamples_per_model[ii]:
                msg = "Rounding has caused {0} != {1}".format(
                    indices.shape[0], rounded_nsamples_per_model[ii])
                raise RuntimeError(msg)
            samples_per_model.append(independent_samples[:, indices])
        return samples_per_model

    def _compute_single_model_nsamples(self, npartition_samples, model_id):
        active_partitions = np.where(
            (self._allocation_mat[:, 2*model_id] == 1) |
            (self._allocation_mat[:, 2*model_id+1] == 1))[0]
        return npartition_samples[active_partitions].sum()

    def _compute_single_model_nsamples_from_partition_ratios(
            self, partition_ratios, target_cost, model_id):
        npartition_samples = self._npartition_samples_from_partition_ratios(
            target_cost, partition_ratios)
        return self._compute_single_model_nsamples(
            npartition_samples, model_id)

    def _compute_nsamples_per_model(self, npartition_samples):
        nsamples_per_model = np.empty(self._nmodels)
        for ii in range(self._nmodels):
            nsamples_per_model[ii] = self._compute_single_model_nsamples(
                npartition_samples, ii)
        return nsamples_per_model

    def _estimate(self, values_per_model, weights, bootstrap=False):
        nmodels = len(values_per_model)
        acv_values = self._separate_values_per_model(
            values_per_model, bootstrap)
        deltas = np.hstack(
            [self._stat.sample_estimate(acv_values[2*ii]) -
             self._stat.sample_estimate(acv_values[2*ii+1])
             for ii in range(1, nmodels)])
        est = (self._stat.sample_estimate(acv_values[1]) +
               weights.numpy().dot(deltas))
        return est

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}(stat={1}, recursion_index={2})".format(
                self.__class__.__name__, self._stat, self._recursion_index)
        rep = "{0}(stat={1}, recursion_index={2}, criteria={3:.3g}".format(
            self.__class__.__name__, self._stat, self._recursion_index,
            self._optimized_criteria)
        rep += " target_cost={0:.5g}, ratios={1}, nsamples={2})".format(
            self._rounded_target_cost,
            self._rounded_partition_ratios,
            self._rounded_nsamples_per_model.numpy())
        return rep

    @abstractmethod
    def _create_allocation_matrix(self, recursion_index):
        r"""
        Return the allocation matrix corresponding to
        self._rounded_nsamples_per_model set by _set_optimized_params

        Returns
        -------
        mat : np.ndarray (nmodels, 2*nmodels)
            For columns :math:`2j, j=0,\ldots,M-1` the ith row contains a
            flag specifiying if :math:`z_i^\star\subseteq z_j^\star`
            For columns :math:`2j+1, j=0,\ldots,M-1` the ith row contains a
            flag specifiying if :math:`z_i\subseteq z_j`
        """
        raise NotImplementedError

    def _get_allocation_matrix(self):
        """return allocation matrix as torch tensor"""
        return torch.as_tensor(self._allocation_mat, dtype=torch.double)

    def _set_recursion_index(self, index):
        """Set the recursion index of the parameterically defined ACV
        Estimator.

        This function intializes the allocation matrix.

        Parameters
        ----------
        index : np.ndarray (nmodels-1)
            The recusion index
        """
        if index is None:
            index = np.zeros(self._nmodels-1, dtype=int)
        else:
            index = np.asarray(index)
        if index.shape[0] != self._nmodels-1:
            msg = "index {0} is the wrong shape. Should be {1}".format(
                index, self._nmodels-1)
            raise ValueError(msg)
        self._create_allocation_matrix(index)
        self._recursion_index = index

    def combine_acv_samples(self, acv_samples):
        return _combine_acv_samples(
            self._allocation_mat, self._rounded_npartition_samples,
            acv_samples)

    def combine_acv_values(self, acv_values):
        return _combine_acv_values(
            self._allocation_mat, self._rounded_npartition_samples, acv_values)

    def plot_allocation(self, ax, show_npartition_samples=False, **kwargs):
        if show_npartition_samples:
            if self._rounded_npartition_samples is None:
                msg = "set_optimized_params must be called"
                raise ValueError(msg)
            return _plot_allocation_matrix(
                self._allocation_mat, self._rounded_npartition_samples, ax,
                **kwargs)

        return _plot_allocation_matrix(
                self._allocation_mat, None, ax, **kwargs)

    def plot_recursion_dag(self, ax):
         return _plot_model_recursion(self._recursion_index, ax)

    def _objective(self, target_cost, x, return_grad=True):
        partition_ratios = torch.as_tensor(x, dtype=torch.double)
        if return_grad:
            partition_ratios.requires_grad = True
            covariance = self._covariance_from_partition_ratios(
                target_cost, partition_ratios)
            val = self._optimization_criteria(covariance)
        if not return_grad:
            return val.item()
        val.backward()
        grad = partition_ratios.grad.detach().numpy().copy()
        partition_ratios.grad.zero_()
        return val.item(), grad

    def _set_initial_guess(self, initial_guess):
        if initial_guess is not None:
            self._initial_guess = torch.as_tensor(
                initial_guess, dtype=torch.double)
        else:
            self._initial_guess = None

    def _allocate_samples_opt_minimize(
            self, costs, target_cost, initial_guess, optim_method,
            optim_options, cons):
        if optim_options is None:
            if optim_method == "SLSQP":
                optim_options = {'disp': False, 'ftol': 1e-10,
                                 'maxiter': 10000, "iprint": 0}
            elif optim_method == "trust-constr":
                optim_options = {'disp': False, 'gtol': 1e-10,
                                 'maxiter': 10000, "verbose": 0}
            else:
                raise ValueError(f"{optim_method} not supported")

        if target_cost < costs.sum():
            msg = "Target cost does not allow at least one sample from "
            msg += "each model"
            raise ValueError(msg)

        nmodels = len(costs)
        nunknowns = len(initial_guess)
        assert nunknowns == nmodels-1
        bounds = None  # [(0, np.inf) for ii in range(nunknowns)]

        return_grad = True
        with warnings.catch_warnings():
            # ignore scipy warnings
            warnings.simplefilter("ignore")
            opt = minimize(
                partial(self._objective, target_cost, return_grad=return_grad),
                initial_guess, method=optim_method, jac=return_grad,
                bounds=bounds, constraints=cons, options=optim_options)
        return opt

    def _get_initial_guess(self, initial_guess, cov, costs, target_cost):
        if initial_guess is not None:
            return initial_guess
        return np.full((self._nmodels-1,), 1)

    def _allocate_samples_opt(self, cov, costs, target_cost,
                              cons=[],
                              initial_guess=None,
                              optim_options=None, optim_method='trust-constr'):
        initial_guess = self._get_initial_guess(
            initial_guess, cov, costs, target_cost)
        assert optim_method == "SLSQP" or optim_method == "trust-constr"
        opt = self._allocate_samples_opt_minimize(
            costs, target_cost, initial_guess, optim_method, optim_options,
            cons)
        partition_ratios = torch.as_tensor(opt.x, dtype=torch.double)
        if not opt.success:  # and (opt.status!=8 or not np.isfinite(opt.fun)):
            raise RuntimeError('optimizer failed'+f'{opt}')
        else:
            val = opt.fun
        return partition_ratios, val

    def _allocate_samples_user_init_guess(self, cons, target_cost, **kwargs):
        opt = self._allocate_samples_opt(
            self._cov, self._costs, target_cost, cons,
            initial_guess=self._initial_guess, **kwargs)
        try:
            opt = self._allocate_samples_opt(
                self._cov, self._costs, target_cost, cons,
                initial_guess=self._initial_guess, **kwargs)
            return opt
        except RuntimeError:
            return None, np.inf

    def _allocate_samples_mfmc(self, cons, target_cost, **kwargs):
        # TODO convert MFMC allocation per model to npartition_samples
        assert False
        if (not (_check_mfmc_model_costs_and_correlations(
                self._costs,
                get_correlation_from_covariance(self._cov.numpy()))) or
                len(self._cov) != len(self._costs)):
            # second condition above will not be true for multiple qoi
            return None, np.inf
        mfmc_model_ratios = torch.as_tensor(_allocate_samples_mfmc(
            self._cov, self._costs, target_cost)[0], dtype=torch.double)
        mfmc_initial_guess = MFMCEstimator._native_ratios_to_npartition_ratios(
            mfmc_model_ratios)
        try:
            opt = self._allocate_samples_opt(
                self._cov, self._costs, target_cost, cons,
                initial_guess=mfmc_initial_guess, **kwargs)
            return opt
        except RuntimeError:
            return None, np.inf

    @abstractmethod
    def _get_specific_constraints(self, target_cost):
        raise NotImplementedError()

    @staticmethod
    def _constraint_jacobian(constraint_fun, partition_ratios_np, *args):
        partition_ratios = torch.as_tensor(
            partition_ratios_np, dtype=torch.double)
        partition_ratios.requires_grad = True
        val = constraint_fun(partition_ratios, *args, return_numpy=False)
        val.backward()
        jac = partition_ratios.grad.detach().numpy().copy()
        partition_ratios.grad.zero_()
        return jac

    def _acv_npartition_samples_constraint(
            self, partition_ratios_np, target_cost, min_nsamples, partition_id,
            return_numpy=True):
        partition_ratios = torch.as_tensor(
            partition_ratios_np, dtype=torch.double)
        nsamples = self._npartition_samples_from_partition_ratios(
            target_cost, partition_ratios)[partition_id]
        val = nsamples-min_nsamples
        if return_numpy:
            return val.item()
        return val

    def _acv_npartition_samples_constraint_jac(
            self, partition_ratios_np, target_cost, min_nsamples,
            partition_id):
        return self._constraint_jacobian(
            self._acv_npartition_samples_constraint, partition_ratios_np,
            target_cost, min_nsamples, partition_id)

    def _npartition_ratios_constaint(self, partition_ratios_np, ratio_id):
        # needs to be positiv e
        return partition_ratios_np[ratio_id]-1e-8

    def _npartition_ratios_constaint_jac(
            self, partition_ratios_np, ratio_id):
        jac = np.zeros(partition_ratios_np.shape[0], dtype=float)
        jac[ratio_id] = 1.0
        return jac

    def _get_constraints(self, target_cost):
        # Ensure the each partition has enough samples to compute
        # the desired statistic. Techinically we only need the number
        # of samples in each acv subset have enough. But this constraint
        # is easy to implement and not really restrictive practically
        if isinstance(
                self._stat,
                (MultiOutputVariance, MultiOutputMeanAndVariance)):
            partition_min_nsamples = 2
        else:
            partition_min_nsamples = 1
        cons = [
            {'type': 'ineq',
             'fun': self._acv_npartition_samples_constraint,
             'jac': self._acv_npartition_samples_constraint_jac,
             'args': (target_cost, partition_min_nsamples, ii)}
            for ii in range(self._nmodels)]

        # Ensure ratios are positive
        cons += [
            {'type': 'ineq',
             'fun': self._npartition_ratios_constaint,
             'jac': self._npartition_ratios_constaint_jac,
             'args': (ii,)}
            for ii in range(self._nmodels-1)]

        # Note target cost is satisfied by construction using the above
        # constraints because nsamples is determined based on target cost
        cons += self._get_specific_constraints(target_cost)
        return cons

    def _allocate_samples(self, target_cost, **kwargs):
        cons = self._get_constraints(target_cost)
        opts = []
        # kwargs["optim_method"] = "trust-constr"
        # opt_user_tr = self._allocate_samples_user_init_guess(
        #     cons, target_cost, **kwargs)
        # opts.append(opt_user_tr)

        if True:  # opt_user_tr[0] is None:
            kwargs["optim_method"] = "SLSQP"
            opt_user_sq = self._allocate_samples_user_init_guess(
                cons, target_cost, **kwargs)
            opts.append(opt_user_sq)

        # kwargs["optim_method"] = "trust-constr"
        # opt_mfmc_tr = self._allocate_samples_mfmc(cons, target_cost, **kwargs)
        # opts.append(opt_mfmc_tr)
        # if opt_mfmc_tr[0] is None:
        #     kwargs["optim_method"] = "SLSQP"
        #     opt_mfmc_sq = self._allocate_samples_mfmc(
        #         cons, target_cost, **kwargs)
        #     opts.append(opt_mfmc_sq)
        obj_vals = np.array([o[1] for o in opts])
        if not np.any(np.isfinite(obj_vals)):
            raise RuntimeError(
                "no solution found from multiple initial guesses {0}")
        II = np.argmin(obj_vals)
        return opts[II]

    def _round_partition_ratios(self, target_cost, partition_ratios):
        npartition_samples = self._npartition_samples_from_partition_ratios(
            target_cost, partition_ratios)
        if ((npartition_samples[0] < 1-1e-8)):
            print(npartition_samples)
            raise RuntimeError("Rounding will cause nhf samples to be zero")
        rounded_npartition_samples = np.floor(
            npartition_samples.numpy()+1e-8).astype(int)
        assert rounded_npartition_samples[0] >= 1
        rounded_target_cost = (
            self._compute_nsamples_per_model(rounded_npartition_samples) *
            self._costs.numpy()).sum()
        rounded_partition_ratios = (
            rounded_npartition_samples[1:]/rounded_npartition_samples[0])
        return rounded_partition_ratios, rounded_target_cost

    def _estimator_cost(self, npartition_samples):
        nsamples_per_model = self._compute_nsamples_per_model(
            asarray(npartition_samples))
        return (nsamples_per_model*self._costs.numpy()).sum()

    def _set_optimized_params(self, partition_ratios, target_cost):
        """
        Set the parameters needed to generate samples for evaluating the
        estimator

        Parameters
        ----------
        rounded_nsample_ratios : np.ndarray (nmodels-1, dtype=int)
            The sample ratios r used to specify the number of samples in
            the independent sample partitions.

        rounded_target_cost : float
            The cost of the new sample allocation

        Sets attrributes
        ----------------
        self._rounded_partition_ratios : np.ndarray (nmodels-1)
            The optimal partition ratios rounded so that each partition
            contains an integer number of samples

        And all attributes set by super()._set_optimized_params. See
        the docstring of that function for further details
        """
        self._rounded_partition_ratios, rounded_target_cost = (
            self._round_partition_ratios(
                target_cost,
                torch.as_tensor(partition_ratios, dtype=torch.double)))
        rounded_npartition_samples = (
            self._npartition_samples_from_partition_ratios(
                rounded_target_cost,
                torch.as_tensor(self._rounded_partition_ratios,
                                dtype=torch.double)))
        # round because sometimes round_partition_ratios
        # will produce floats slightly smaller
        # than an integer so when converted to an integer will produce
        # values 1 smaller than the correct value
        rounded_npartition_samples = np.round(rounded_npartition_samples)
        rounded_nsamples_per_model = torch.as_tensor(
            self._compute_nsamples_per_model(rounded_npartition_samples),
            dtype=torch.int)
        super()._set_optimized_params_base(
            rounded_npartition_samples, rounded_nsamples_per_model,
            rounded_target_cost)

    def _allocate_samples_for_single_recursion(self, target_cost, **kwargs):
        partition_ratios, obj_val = self._allocate_samples(
            target_cost, **kwargs)
        self._set_optimized_params(partition_ratios, target_cost)

    def get_all_recursion_indices(self):
        return _get_acv_recursion_indices(self._nmodels, self._tree_depth)

    def _allocate_samples_for_all_recursion_indices(
            self, target_cost, **kwargs):
        verbosity = kwargs.pop("verbosity", 0)
        best_criteria = torch.as_tensor(np.inf, dtype=torch.double)
        best_result = None
        for index in self.get_all_recursion_indices():
            self._set_recursion_index(index)
            try:
                self._allocate_samples_for_single_recursion(
                    target_cost, **kwargs)
            except RuntimeError as e:
                # typically solver fails because trying to use
                # uniformative model as a recursive control variate
                if not self._allow_failures:
                    raise e
                self._optimized_criteria = torch.as_tensor([np.inf])
                if verbosity > 0:
                    print("Optimizer failed")
            if verbosity > 0:
                msg = "Recursion: {0} Objective: best {1}, current {2}".format(
                    index, best_criteria.item(),
                    self._optimized_criteria.item())
                print(msg)
            if self._optimized_criteria < best_criteria:
                best_result = [self._rounded_partition_ratios,
                               self._rounded_target_cost,
                               self._optimized_criteria, index]
                best_criteria = self._optimized_criteria
        if best_result is None:
            raise RuntimeError("No solutions were found")
        self._set_recursion_index(best_result[3])
        self._set_optimized_params(
            torch.as_tensor(best_result[0], dtype=torch.double),
            target_cost)

    def allocate_samples(self, target_cost, **kwargs):
        if self._tree_depth is not None:
            return self._allocate_samples_for_all_recursion_indices(
                target_cost, **kwargs)
        kwargs.pop("verbosity", 0)
        return self._allocate_samples_for_single_recursion(
            target_cost, **kwargs)


class GMFEstimator(ACVEstimator):
    def _create_allocation_matrix(self, recursion_index):
        self._allocation_mat = _get_allocation_matrix_gmf(
            recursion_index)

    def _get_specific_constraints(self, target_cost):
        return []


class GISEstimator(ACVEstimator):
    """
    The GIS estimator from Gorodetsky et al. and Bomorito et al
    """
    def _create_allocation_matrix(self, recursion_index):
        self._allocation_mat = _get_allocation_matrix_acvis(
            recursion_index)

    def _get_specific_constraints(self, target_cost):
        return []


class GRDEstimator(ACVEstimator):
    """
    The GRD estimator.
    """
    def _create_allocation_matrix(self, recursion_index):
        self._allocation_mat = _get_allocation_matrix_acvrd(
            recursion_index)

    def _get_specific_constraints(self, target_cost):
        return []


class MFMCEstimator(GMFEstimator):
    def __init__(self, stat, costs, cov, opt_criteria=None,
                 opt_qoi=0):
        # Use the sample analytical sample allocation for estimating a scalar
        # mean when estimating any statistic
        nmodels = len(costs)
        super().__init__(stat, costs, cov,
                         recursion_index=np.arange(nmodels-1),
                         opt_criteria=None)
        # The qoi index used to generate the sample allocation
        self._opt_qoi = opt_qoi

    def _allocate_samples(self, target_cost):
        # nsample_ratios returned will be listed in according to
        # self.model_order which is what self.get_rsquared requires
        nqoi = self._cov.shape[0]//len(self._costs)
        nsample_ratios, val = _allocate_samples_mfmc(
            self._cov.numpy()[self._opt_qoi::nqoi, self._opt_qoi::nqoi],
            self._costs.numpy(), target_cost)
        nsample_ratios = (
            self._native_ratios_to_npartition_ratios(nsample_ratios))
        return torch.as_tensor(nsample_ratios, dtype=torch.double), val

    @staticmethod
    def _native_ratios_to_npartition_ratios(ratios):
        partition_ratios = np.hstack((ratios[0]-1, np.diff(ratios)))
        return partition_ratios

    def _get_allocation_matrix(self):
        return _get_sample_allocation_matrix_mfmc(self._nmodels)


class MLMCEstimator(GRDEstimator):
    def __init__(self, stat, costs, cov, opt_criteria=None,
                 opt_qoi=0):
        """
        Use the sample analytical sample allocation for estimating a scalar
        mean when estimating any statistic

        Use optimal ACV weights instead of all weights=-1 used by
        classical MLMC.
        """
        nmodels = len(costs)
        super().__init__(stat, costs, cov,
                         recursion_index=np.arange(nmodels-1),
                         opt_criteria=None)
        # The qoi index used to generate the sample allocation
        self._opt_qoi = opt_qoi

    @staticmethod
    def _weights(CF, cf):
        # raise NotImplementedError("check weights size is correct")
        return -torch.ones(cf.shape, dtype=torch.double)

    def _covariance_from_npartition_samples(self, npartition_samples):
        CF, cf = self._get_discrepancy_covariances(npartition_samples)
        weights = self._weights(CF, cf)
        # cannot use formulation of variance that uses optimal weights
        # must use the more general expression below, e.g. Equation 8
        # from Dixon 2024.
        return self._covariance_non_optimal_weights(
            self._stat.high_fidelity_estimator_covariance(
                npartition_samples[0]), weights, CF, cf)

    def _allocate_samples(self, target_cost):
        nqoi = self._cov.shape[0]//len(self._costs)
        nsample_ratios, val = _allocate_samples_mlmc(
            self._cov.numpy()[self._opt_qoi::nqoi, self._opt_qoi::nqoi],
            self._costs.numpy(), target_cost)
        return torch.as_tensor(nsample_ratios, dtype=torch.double), val

    def _create_allocation_matrix(self, dummy):
        self._allocation_mat = _get_sample_allocation_matrix_mlmc(
            self._nmodels)

    @staticmethod
    def _native_ratios_to_npartition_ratios(ratios):
        partition_ratios = [ratios[0]-1]
        for ii in range(1, len(ratios)):
            partition_ratios.append(ratios[ii]-partition_ratios[ii-1])
        return np.hstack(partition_ratios)


class BestEstimator():
    def __init__(self, est_types, stat_type, costs, cov,
                 max_nmodels, *est_args, **est_kwargs):
        
        self.best_est = None

        self._estimator_types = est_types
        self._stat_type = stat_type
        self._candidate_cov, self._candidate_costs = cov, np.asarray(costs)
        # self._ncandidate_nmodels is the number of total models
        self._ncandidate_models = len(self._candidate_costs)
        self._lf_model_indices = np.arange(1, self._ncandidate_models)
        self._nqoi = self._candidate_cov.shape[0]//self._ncandidate_models
        self._max_nmodels = max_nmodels
        self._args = est_args
        self._allow_failures = est_kwargs.get("allow_failures", False)
        if "allow_failures" in est_kwargs:
            del est_kwargs["allow_failures"]
        self._kwargs = est_kwargs
        self._best_model_indices = None
        self._all_model_labels = None

        self._save_candidate_estimators = False
        self._candidate_estimators = None

    @property
    def model_labels(self):
        return [self._all_model_labels[idx]
                for idx in self._best_model_indices]

    @model_labels.setter
    def model_labels(self, labels):
        self._all_model_labels = labels

    def _validate_kwargs(self, nsubset_lfmodels):
        sub_kwargs = copy.deepcopy(self._kwargs)
        if "recursion_index" in sub_kwargs:
            index = sub_kwargs["recursion_index"]
            if (np.allclose(index, np.arange(len(index))) or
                    np.allclose(index, np.zeros(len(index)))):
                sub_kwargs["recursion_index"] = index[:nsubset_lfmodels]
            else:
                msg = "model selection can only be used with recursion indices"
                msg += " (0, 1, 2, ...) or (0, ..., 0) or tree_depth is"
                msg += " not None"
                # There is no logical way to reduce a recursion index to use
                # a subset of model unless they are one of these two indices
                # or tree_depth is not None so that all possible recursion
                # indices are considered
                raise ValueError(msg)
        if "tree_depth" in sub_kwargs:
            sub_kwargs["tree_depth"] = min(
                sub_kwargs["tree_depth"], nsubset_lfmodels)
        return sub_kwargs

    def _get_estimator(self, est_type, subset_costs, subset_cov, 
                       target_cost, sub_args, sub_kwargs, allocate_kwargs):
        try:
            est = get_estimator(
                est_type, self._stat_type, self._nqoi,
                subset_costs, subset_cov, *sub_args, **sub_kwargs)
        except ValueError as e:
            if sub_kwargs.pop("verbosity", 0) > 0:
                print(e)
            # Some estimators, e.g. MFMC, fail when certain criteria
            # are not satisfied
            return None
        try:
            est.allocate_samples(target_cost, **allocate_kwargs)
            if sub_kwargs.pop("verbosity", 0) > 0:
                msg = "Model: {0} Objective: {1}".format(
                    idx, est._optimized_criteria.item())
                print(msg)
            return est
        except (RuntimeError, ValueError) as e:
            if self._allow_failures:
                return None
            raise e
        
    def _get_model_subset_estimator(
            self, qoi_idx, nsubset_lfmodels, allocate_kwargs,
            target_cost, lf_model_subset_indices):

        idx = np.hstack(([0], lf_model_subset_indices)).astype(int)
        subset_cov = _nqoi_nqoi_subproblem(
            self._candidate_cov, self._ncandidate_models, self._nqoi,
            idx, qoi_idx)
        subset_costs = self._candidate_costs[idx]
        sub_args = multioutput_stats[self._stat_type]._args_model_subset(
            self._ncandidate_models, self._nqoi, idx, *self._args)
        sub_kwargs = self._validate_kwargs(nsubset_lfmodels)

        best_est = None
        best_criteria = np.inf
        for est_type in self._estimator_types:
            est = self._get_estimator(
                est_type, subset_costs, subset_cov, 
                target_cost, sub_args, sub_kwargs, allocate_kwargs)
            if self._save_candidate_estimators:
                self._candidate_estimators.append(est)
            if est is not None and est._optimized_criteria < best_criteria:
                best_est = est
                best_criteria = est._optimized_criteria.item()
        return best_est
 
    def _get_best_model_subset_for_estimator_pool(
            self, nsubset_lfmodels, target_cost,
           best_criteria, best_model_indices, best_est, **allocate_kwargs):
        qoi_idx = np.arange(self._nqoi)
        nprocs = allocate_kwargs.get("nprocs", 1)
        pool = Pool(nprocs)
        indices = list(
            combinations(self._lf_model_indices, nsubset_lfmodels))
        result = pool.map(
            partial(self._get_model_subset_estimator,
                    qoi_idx, nsubset_lfmodels, allocate_kwargs,
                    target_cost), indices)
        pool.close()
        criteria = [
            np.array(est._optimized_criteria)
            if est is not None else np.inf for est in result]
        II = np.argmin(criteria)
        if not np.isfinite(criteria[II]):
            best_est = None
        else:
            best_est = result[II]
            best_model_indices = np.hstack(
                ([0], indices[II])).astype(int)
            best_criteria = best_est._optimized_criteria
        return best_criteria, best_model_indices, best_est

    def _get_best_model_subset_for_estimator_serial(
            self, nsubset_lfmodels, target_cost,
            best_criteria, best_model_indices, best_est, **allocate_kwargs):
        qoi_idx = np.arange(self._nqoi)
        for lf_model_subset_indices in combinations(
                self._lf_model_indices, nsubset_lfmodels):
            est = self._get_model_subset_estimator(
                qoi_idx, nsubset_lfmodels, allocate_kwargs,
                target_cost, lf_model_subset_indices)
            if est is not None and est._optimized_criteria < best_criteria:
                best_est = est
                best_model_indices = np.hstack(
                    ([0], lf_model_subset_indices)).astype(int)
                best_criteria = best_est._optimized_criteria
        return best_criteria, best_model_indices, best_est

    def _get_best_estimator(self, target_cost, **allocate_kwargs):
        best_criteria = np.inf
        best_est, best_model_indices = None, None
        nprocs = allocate_kwargs.get("nprocs", 1)
        
        if allocate_kwargs.get("verbosity", 0) > 0:
            print(f"Finding best model using {nprocs} processors")
        if "nprocs" in allocate_kwargs:
            del allocate_kwargs["nprocs"]

        if self._max_nmodels is None:
            min_nlfmodels = self._ncandidate_models-1
            max_nmodels = self._ncandidate_models
        else:
            min_nlfmodels = 1
            max_nmodels = self._ncandidate_models
            
        for nsubset_lfmodels in range(min_nlfmodels, max_nmodels):
            if nprocs > 1:
                 best_criteria, best_model_indices, best_est = (
                     self._get_best_model_subset_for_estimator_pool(
                         nsubset_lfmodels, target_cost,
                         best_criteria, best_model_indices, best_est,
                         **allocate_kwargs))
            else:
                 best_criteria, best_model_indices, best_est = (
                     self._get_best_model_subset_for_estimator_serial(
                         nsubset_lfmodels, target_cost,
                         best_criteria, best_model_indices, best_est,
                         **allocate_kwargs))
            
        if best_est is None:
            raise RuntimeError("No solutions found for any model subset")
        return best_est, best_model_indices

    def allocate_samples(self, target_cost, **allocate_kwargs):
        if self._save_candidate_estimators:
            self._candidate_estimators = []
        best_est, best_model_indices = self._get_best_estimator(
            target_cost, **allocate_kwargs)
        self.best_est = best_est
        self._best_model_indices = best_model_indices
        self._set_best_est_attributes()

    def _set_best_est_attributes(self):
        # allow direct access of important self.best_est attributes
        # __call__ cannot be set using this approach.
        attr_list = [
            # public functions
            "combine_acv_samples",
            "combine_acv_values",
            "generate_samples_per_model",
            "insert_pilot_values",
            "bootstrap",
            "plot_allocation",
            # private functions and variables
            "_separate_values_per_model",
            "_covariance_from_npartition_samples",
            "_covariance_from_partition_ratios",
            "_rounded_partition_ratios", "_stat",
            "_nmodels", "_cov", "_rounded_npartition_samples",
            "_rounded_nsamples_per_model", "_costs",
            "_optimized_criteria", "_get_discrepancy_covariances",
            "_rounded_target_cost",
            "_get_allocation_matrix",
            "_optimization_criteria",
            "_optimized_covariance",
            "_allocation_mat"]
        for attr in attr_list:
            setattr(self, attr, getattr(self.best_est, attr))

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}".format(self.__class__.__name__)
        return "{0}(est={1}, subset={2})".format(
            self.__class__.__name__, self.best_est, self._best_model_indices)

    def __call__(self, values):
        return self.best_est(values)


multioutput_estimators = {
    "cv": CVEstimator,
    "gmf": GMFEstimator,
    "gis": GISEstimator,
    "grd": GRDEstimator,
    "mfmc": MFMCEstimator,
    "mlmc": MLMCEstimator,
    "mc": MCEstimator}


multioutput_stats = {
    "mean": MultiOutputMean,
    "variance": MultiOutputVariance,
    "mean_variance": MultiOutputMeanAndVariance,
}


def get_estimator(estimator_types, stat_type, nqoi, costs, cov, *stat_args,
                  max_nmodels=None, **est_kwargs):
    """
    Parameters
    ----------
    estimator_types : list [str] or str
        If str (or len(estimators_types==1), then return the estimator 
        named estimator_type (or estimator_types[0])

    stat_type : str
        The type of statistics to compute

    nqoi : integer
        The number of quantities of interest (QoI) that each model returns

    costs : np.ndarray (nmodels)
        The computational cost of evaluating each model

    cov : np.ndarray (nmodels*nqoi, nmodels*nqoi)
        The covariance between all the QoI of all the models

    stat_args : list or tuple
        The arguments that are needed to compute the statistic

    max_nmodels : integer
        If None, compute the estimator using all the models. If not None,
        find the model subset that uses at most max_nmodels that minimizes
        the estimator covariance.

    est_kwargs : dict
        Keyword arguments that will be passed when creating each estimator.
    """
    if isinstance(estimator_types, list) or max_nmodels is not None:
        if not isinstance(estimator_types, list):
            estimator_types = [estimator_types]
        return BestEstimator(
            estimator_types, stat_type, costs, cov,
            max_nmodels, *stat_args, **est_kwargs)

    if isinstance(estimator_types, list):
        estimator_type = estimator_types[0]
    else:
        estimator_type = estimator_types
    
    if estimator_type not in multioutput_estimators:
        msg = f"Estimator {estimator_type} not supported. "
        msg += f"Must be one of {multioutput_estimators.keys()}"
        raise ValueError(msg)

    if stat_type not in multioutput_stats:
        msg = f"Statistic {stat_type} not supported. "
        msg += f"Must be one of {multioutput_stats.keys()}"
        raise ValueError(msg)

    stat = multioutput_stats[stat_type](nqoi, cov, *stat_args)
    return multioutput_estimators[estimator_type](
        stat, costs, cov, **est_kwargs)


def _estimate_components(variable, est, funs, ii):
    """
    Notes
    -----
    To create reproducible results when running numpy.random in parallel
    must use RandomState. If not the results will be non-deterministic.
    This is happens because of a race condition. numpy.random.* uses only
    one global PRNG that is shared across all the threads without
    synchronization. Since the threads are running in parallel, at the same
    time, and their access to this global PRNG is not synchronized between
    them, they are all racing to access the PRNG state (so that the PRNG's
    state might change behind other threads' backs). Giving each thread its
    own PRNG (RandomState) solves this problem because there is no longer
    any state that's shared by multiple threads without synchronization.
    Also see new features
    https://docs.scipy.org/doc/numpy/reference/random/parallel.html
    https://docs.scipy.org/doc/numpy/reference/random/multithreading.html
    """
    random_state = np.random.RandomState(ii)
    samples_per_model = est.generate_samples_per_model(
        partial(variable.rvs, random_state=random_state))
    values_per_model = [
        fun(samples) for fun, samples in zip(funs, samples_per_model)]

    mc_est = est._stat.sample_estimate
    if (isinstance(est, ACVEstimator) or
            isinstance(est, BestEstimator)):
        # the above condition does not allow BestEstimator to be
        # applied to CVEstimator
        est_val = est(values_per_model)
        acv_values = est._separate_values_per_model(values_per_model)
        Q = mc_est(acv_values[1])
        delta = np.hstack([mc_est(acv_values[2*ii]) -
                           mc_est(acv_values[2*ii+1])
                           for ii in range(1, est._nmodels)])
    elif isinstance(est, CVEstimator):
        est_val = est(values_per_model)
        Q = mc_est(values_per_model[0])
        delta = np.hstack(
            [mc_est(values_per_model[ii]) - est._lowfi_stats[ii-1]
             for ii in range(1, est._nmodels)])
    else:
        est_val = est(values_per_model[0])
        Q = mc_est(values_per_model[0])
        delta = Q*0
    return est_val, Q, delta


def _estimate_components_loop(
        variable, ntrials, est, funs, max_eval_concurrency):
    if max_eval_concurrency == 1:
        Q = []
        delta = []
        estimator_vals = []
        for ii in range(ntrials):
            est_val, Q_val, delta_val = _estimate_components(
                variable, est, funs, ii)
            estimator_vals.append(est_val)
            Q.append(Q_val)
            delta.append(delta_val)
        Q = np.array(Q)
        delta = np.array(delta)
        estimator_vals = np.array(estimator_vals)
        return estimator_vals, Q, delta

    from multiprocessing import Pool
    # set flat funs to none so funs can be pickled
    pool = Pool(max_eval_concurrency)
    func = partial(_estimate_components, variable, est, funs)
    result = pool.map(func, list(range(ntrials)))
    pool.close()
    estimator_vals = np.asarray([r[0] for r in result])
    Q = np.asarray([r[1] for r in result])
    delta = np.asarray([r[2] for r in result])
    return estimator_vals, Q, delta


def numerically_compute_estimator_variance(
        funs, variable, est, ntrials=int(1e3), max_eval_concurrency=1,
        return_all=False):
    r"""
    Numerically estimate the variance of an approximate control variate
    estimator.

    Parameters
    ----------
    funs : list [callable]
        List of functions with signature

        `fun(samples) -> np.ndarray (nsamples, nqoi)`

    where samples has shape (nvars, nsamples)

    est : :class:`pyapprox.multifidelity.multioutput_monte_carlo.MCEstimator`
        A Monte Carlo like estimator for computing sample based statistics

    ntrials : integer
        The number of times to compute estimator using different randomly
        generated set of samples

    max_eval_concurrency : integer
        The number of processors used to compute realizations of the estimators
        which can be run independently and in parallel.

    Returns
    -------
    hf_covar_numer : np.ndarray (nstats, nstats)
        The estimator covariance of the single high-fidelity Monte Carlo
        estimator

    hf_covar : np.ndarray (nstats, nstats)
        The analytical value of the estimator covariance of the single
       high-fidelity Monte Carlo estimator


    covar_numer : np.ndarray (nstats, nstats)
        The estimator covariance of est

    hf_covar : np.ndarray (nstats, nstats)
        The analytical value of the estimator covariance of est

    est_vals : np.ndarray (ntrials, nstats)
        The values for the est for each trial. Only returned if return_all=True

    Q0 : np.ndarray (ntrials, nstats)
        The values for the single fidelity MC estimator for each trial.
        Only returned if return_all=True

    delta : np.ndarray (ntrials, nstats)
        The values for the differences between the low-fidelty estimators
        :math:`\mathcal{Z}_\alpha` and :math:`\mathcal{Z}_\alpha^*`
        for each trial. Only returned if return_all=True
    """
    ntrials = int(ntrials)
    est_vals, Q0, delta = _estimate_components_loop(
        variable, ntrials, est, funs, max_eval_concurrency)

    hf_covar_numer = np.cov(Q0, ddof=1, rowvar=False)
    hf_covar = est._stat.high_fidelity_estimator_covariance(
        est._rounded_npartition_samples[0])

    covar_numer = np.cov(est_vals, ddof=1, rowvar=False)
    covar = est._covariance_from_npartition_samples(
        est._rounded_npartition_samples).numpy()

    if not return_all:
        return hf_covar_numer, hf_covar, covar_numer, covar
    return hf_covar_numer, hf_covar, covar_numer, covar, est_vals, Q0, delta


def compare_estimator_variances(target_costs, estimators):
    """
    Compute the variances of different Monte-Carlo like estimators.

    Parameters
    ----------
    target_costs : np.ndarray (ntarget_costs)
        Different total cost budgets

    estimators : list (nestimators)
        List of Monte Carlo estimator objects, e.g.
        :class:`~pyapprox.multifidelity.multioutput_monte_carlo.MCEstimator`

    Returns
    -------
        optimized_estimators : list
         Each entry is a list of optimized estimators for a set of target costs
    """
    optimized_estimators = []
    for est in estimators:
        est_copies = []
        for target_cost in target_costs:
            est_copy = copy.deepcopy(est)
            est_copy.allocate_samples(target_cost)
            est_copies.append(est_copy)
        optimized_estimators.append(est_copies)
    return optimized_estimators


class ComparisionCriteria():
    def __init__(self, criteria_type):
        self._criteria_type = criteria_type

    def __call__(self, est_covariance, est):
        if self._criteria_type == "det":
            return determinant_variance(est_covariance)
        if self._criteria_type == "trace":
            return np.exp(log_trace_variance(est_covariance))
        raise ValueError(
            "Criteria {0} not supported".format(self._criteria_type))

    def __repr__(self):
        return "{0}(citeria={1})".format(
            self.__class__.__name__, self._criteria_type)


class SingleQoiAndStatComparisonCriteria(ComparisionCriteria):
    def __init__(self, stat_type, qoi_idx):
        """
        Compare estimators based on the variance of a single statistic
        for a single QoI even though mutiple QoI may have been used to compute
        multiple statistics

        Parameters
        ----------
        stat_type: str
            The stat type. Must be one of ["mean", "variance", "mean_variance"]

        qoi_idx: integer
            The index of the QoI as it appears in the covariance matrix
        """
        self._stat_type = stat_type
        self._qoi_idx = qoi_idx

    def __call__(self, est_covariance, est):
        if self._stat_type != "mean" and isinstance(
                est._stat, MultiOutputMeanAndVariance):
            return (
                est_covariance[est.nqoi+self._qoi_idx,
                               est._nqoi+self._qoi_idx])
        elif (isinstance(
                est._stat, (MultiOutputVariance, MultiOutputMean)) or
              self._stat_type == "mean"):
            return est_covariance[self._qoi_idx, self._qoi_idx]
        raise ValueError("{0} not supported".format(est._stat))

    def __repr__(self):
        return "{0}(stat={1}, qoi={2})".format(
            self.__class__.__name__, self._stat_type, self._qoi_idx)


def compute_variance_reductions(optimized_estimators,
                                criteria=ComparisionCriteria("det"),
                                nhf_samples=None):
    """
    Compute the variance reduction (relative to single model MC) for a
    list of optimized estimtors.

    Parameters
    ----------
    optimized_estimators : list
         Each entry is a list of optimized estimators for a set of target costs

    est_labels : list (nestimators)
        String used to label each estimator

    criteria : callable
        A function that returns a scalar metric of the estimator covariance
        with signature

        `criteria(cov) -> float`

        where cov is an np.ndarray (nstats, nstats) is the estimator covariance

    nhf_samples : int
        The number of samples of the high-fidelity model used for the
        high-fidelity only estimator. If None, then the number of high-fidelity
        evaluations that produce a estimator cost equal to the optimized
        target cost of the estimator is used. Usually, nhf_samples should be
        set to None.
    """
    var_red, est_criterias, sf_criterias = [], [], []
    optimized_estimators = optimized_estimators.copy()
    nestimators = len(optimized_estimators)
    for ii in range(nestimators):
        est = optimized_estimators[ii]
        est_criteria = criteria(est._covariance_from_npartition_samples(
            est._rounded_npartition_samples), est)
        if nhf_samples is None:
            nhf_samples = int(est._rounded_target_cost/est._costs[0])
        sf_criteria = criteria(
            est._stat.high_fidelity_estimator_covariance(
                nhf_samples), est)
        var_red.append(sf_criteria/est_criteria)
        sf_criterias.append(sf_criteria)
        est_criterias.append(est_criteria)
    return (np.asarray(var_red), np.asarray(est_criterias),
            np.asarray(sf_criterias))

# COMMON TORCH AUTOGRAD MISTAKES
# Do not use hstack to form a vector
# The following will create an numerical error in gradient
# but not error is thrown
# torch.hstack([nhf_samples, nhf_samlpes*npartition_ratios])
# So instead use
# npartition_samples = torch.empty(
# partition_ratios.shape[0]+1, dtype=torch.double)
# npartition_samples[0] = nhf_samples
# npartition_samples[1:] = partition_ratios*nhf_samples
