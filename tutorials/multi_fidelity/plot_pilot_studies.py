r"""
Pilot Studies
=============
The covariance of an ACV estimator depends on the covariance between the different model fidelities and other statistics, such as variance, depend on additional statistics properties of the model. In previous tutorials, we have assumed that thse statistics are available. However, in practice they must be estimated, because if we knew them we would not have to construct an MC estimator in the first place. 

This tutorial presents how to use a pilot-study to compute the statistics needed to compute a MC-based estimator and compute its estimator covariance. We will focus on estimating the mean of a scalar model, but the procedures we describe here can easily be extended to estimation of other statistics such as variance. One simlpy must use a pilot study to compute the relevant quantities defined in :ref:`sphx_glr_auto_tutorials_acv_covariances.py`

Computing an ACV estimator of a statistic requires computing  :math:`\covar{\mat{Q}_0}{\mat{\Delta}}\text{ and} \covar{\mat{\Delta}}{\mat{\Delta}}`. These quantities in turn depend on the quantities in :ref:`sphx_glr_auto_tutorials_acv_covariances.py`. For example, when estimating the mean we must compute 

.. math:: \covar{f_\alpha}{f_\beta}\qquad \forall \alpha,\beta

Typically, this is not available so we compute it with a pilot study. A pilot study evaluates all the available models at a small set of samples :math:`\rvset_\text{pilot}` and computes the quantities necessary to construct an ACV estimator. For example when computing the mean we estimate

.. math:: \covar{f_\alpha}{f_\beta}\approx{N_\text{\pilot}^{-1}}\sum_{n=1}^N_\text{\pilot} \left(f_\alpha(\rv^{(n)})-Q_\alpha(\rvset_\text{pilot})\right)\left(f_\beta(\rv^{(n)})-Q_\beta(\rvset_\text{pilot})\right)

where

.. math::  Q_\alpha={N_\text{\pilot}^{-1}}\sum_{n=1}^N_\text{\pilot} f_\alpha(\rv^{(n)})\approx \mean{f_\alpha}

With an unlimited computational budget, we would drive :math:`N_\text{\pilot}\toinfty`, however in practice we must use a finite number of samples which introduces an error in to an ACV estimator.

The following code quantities the impact of the size of the pilot sample on the accuracy of an ACV estimator. If the pilot size is too small the accuracy of the control variate coefficieints will be poor, however it is made larger, constructing the pilot study will eat into the computational budget used to construct the estimator which also degrades accuracy. These two concerns need to be balanced.
"""

#%%
# First setup the example
import numpy as np
import matplotlib.pyplot as plt

from pyapprox.benchmarks import setup_benchmark
from pyapprox.multifidelity.multioutput_monte_carlo import (
    get_estimator)
from pyapprox.util.visualization import mathrm_label

np.random.seed(1)
shifts = [.1, .2]
benchmark = setup_benchmark(
    "tunable_model_ensemble", theta1=np.pi/2*.95, shifts=shifts)
costs = np.array([1, 0.1, 0.01])
benchmark = setup_benchmark("short_column_ensemble", nmodels=2)
costs = np.array([1, 0.5, 0.05, 0.01, 0.002])[:2]
benchmark = setup_benchmark("polynomial_ensemble", nmodels=3)
costs = np.array([1, 0.1, 0.05])
nmodels = len(benchmark.fun.models)

#%%
#Now choose an estimator and optimally allocate it with oracle information,
#that is the exact model covariance.
from pyapprox.util.utilities import get_correlation_from_covariance
print(get_correlation_from_covariance(benchmark.fun.get_covariance_matrix()))
target_cost = 100
est_name = "mfmc"
oracle_est = get_estimator(
    est_name, "mean", 1, costs, benchmark.fun.get_covariance_matrix())
oracle_est.allocate_samples(target_cost)
print(oracle_est)

#%%
#Now lets look at the MSE of the ACV estimator when pilot samples of different sizes are used. First, create a function that computes an acv estimator for a single pilot study.  We will then repeatedly call this function to compute the MSE.
#
#Note, the function we define below can be replicated for most practical application of ACV estimation, but in such situations it sill only be called once.
from pyapprox.multifidelity.stats import MultiOutputMean
from functools import partial

def build_acv(funs, variable, target_cost, npilot_samples, adjust_cost=True,
              seed=1):
    # run pilot study

    # random state is only needed if running build acv using multiprocesing.Pool
    # random_state = np.random.RandomState(seed)
    # pilot_samples = variable.rvs(npilot_samples, random_state=random_state)
    pilot_samples = variable.rvs(npilot_samples)
    pilot_values_per_model = [fun(pilot_samples) for fun in funs]
    pilot_cov = MultiOutputMean.compute_pilot_quantities(
        pilot_values_per_model)
    # print(get_correlation_from_covariance(pilot_cov))

    # optimize the ACV estimator
    est = get_estimator(est_name, "mean", 1, costs, pilot_cov)
    # remaining_budget_after_pilot
    if adjust_cost:
        adjusted_target_cost = target_cost - (costs*npilot_samples).sum()
    else:
        adjusted_target_cost = target_cost
    # print(adjusted_target_cost)
    est.allocate_samples(adjusted_target_cost)

    # compute the ACV estimator
    random_state = np.random.RandomState(seed+npilot_samples)
    # samples_per_model = est.generate_samples_per_model(partial(
    #    variable.rvs, random_state=random_state))
    samples_per_model = est.generate_samples_per_model(variable.rvs)
    values_per_model = [
        fun(samples) for fun, samples in zip(funs, samples_per_model)]
    est_stats = est(values_per_model)
    return est_stats

#%%
#Now define a function to compute the MSE. Note nprocs cannot be set > 1 unless all of this code is placed inside a function, e.g. called main,  which is then
#run inside the following conditional
#if __name__ == '__main__':
#    main()
from multiprocessing import Pool
def compute_mse(build_acv, funs, variable, target_cost, npilot_samples,
                adjust_cost, ntrials, nprocs, exact_stats):
    build = partial(
        build_acv, funs, variable, target_cost, npilot_samples, adjust_cost)
    if nprocs > 1:
        pool = Pool(nprocs)
        est_vals = pool.map(build, list(range(ntrials)))
        pool.close()
    else:
        est_vals = np.asarray([build(ii) for ii in range(ntrials)])
    mse = ((est_vals-exact_stats)**2).mean()
    return mse

#%%
#Now we will build many realiaztions of the ACV estimator for each different samples size. We must ensure that the cost of the pilot study and the construction of the ACV estimator do not exceed the target cost.

print("oracle_mse", oracle_est._optimized_covariance[0, 0].item()) # hack remove it is below

np.random.seed(1)
ntrials = int(1e4)
mse_list = []
npilot_samples_list = [10, 20, 40, 80, 160, 320, 640, 1280]
for npilot_samples in npilot_samples_list:
    print(npilot_samples)
    mse_list.append(compute_mse(
        build_acv, benchmark.fun.models, benchmark.variable, target_cost,
        npilot_samples, False, ntrials, 1, benchmark.fun.get_means()[0]))
    print(mse_list[-1])

#%%
# Now compare the MSE of the oracle ACV estimator which is just equal to its estimator variance, with the estimators constructed for different pilot samples sizes.

mc_est = get_estimator(
    "mc", "mean", 1, costs, benchmark.fun.get_covariance_matrix())
mc_est.allocate_samples(target_cost)
mc_mse = mc_est._optimized_covariance[0, 0].item()

oracle_mse = oracle_est._optimized_covariance[0, 0].item()
ax = plt.subplots(1, 1, figsize=(8, 6))[1]
ax.axhline(y=oracle_mse, ls='--', color='k', label=mathrm_label("Oracle MSE"))
ax.axhline(y=mc_mse, ls=':', color='r', label=mathrm_label("MC MSE"))
ax.plot(npilot_samples_list, mse_list, '-o', label=mathrm_label("Pilot MSE"))
ax.set_xlabel(mathrm_label("Number of pilot samples"))#)
_ = ax.legend()

#%%
#Estimating model costs
#----------------------
#The pilot study is also used to approximate the model costs. Typically, we take the median run time of each model over the pilot samples
