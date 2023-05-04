import unittest
import numpy as np
from sklearn.gaussian_process.kernels import _approx_fprime
import copy
from scipy import stats

from pyapprox.util.utilities import cartesian_product
from pyapprox.variables.joint import IndependentMarginalsVariable
from pyapprox.surrogates.orthopoly.quadrature import (
    gauss_jacobi_pts_wts_1D)
from pyapprox.surrogates.integrate import integrate
from pyapprox.surrogates.gaussianprocess.kernels import (
    RBF, MultifidelityPeerKernel, MultilevelKernel, MultiTaskKernel,
    MonomialScaling, ConstantKernel)
from pyapprox.surrogates.gaussianprocess.multilevel import (
    MultilevelGaussianProcess, SequentialMultilevelGaussianProcess)
from pyapprox.surrogates.gaussianprocess.gaussian_process import (
    GaussianProcess)
from pyapprox.surrogates.gaussianprocess.multilevel import (
    GreedyMultilevelIntegratedVarianceSampler)


class TestMultilevelGaussianProcess(unittest.TestCase):
    def setUp(self):
        np.random.seed(1)

    def _setup_peer_model_ensemble(self, rho):
        nmodels = len(rho)

        def f1(x):
            return ((x.T*6-2)**2)*np.sin((x.T*6-2)*2)/5

        def f2(x):
            return ((x.T*4)**2)*np.sin((x.T*4-2)*2)/5

        def f3(x):
            if nmodels == 2:
                return (rho[0]*f2(x))+((x.T-0.5)*1. - 5)/5
            return (rho[0]*f1(x)+rho[1]*f2(x))+((x.T-0.5)*1. - 5)/5
        return f1, f2, f3

    def _setup_multilevel_model_ensemble(self, rho):
        def f1(x):
            return ((x.T*6-2)**2)*np.sin((x.T*6-2)*2)/5

        def f2(x):
            return rho[0]*f1(x)+np.cos(x.T*2)/10

        def f3(x):
            return rho[-1]*f2(x)+((x.T-0.5)*1. - 5)/5
        return f1, f2, f3

    def test_pyapprox_rbf_kernel(self):
        kernel = RBF(0.1)
        nvars, nsamples = 1, 3
        XX = np.random.uniform(0, 1, (nsamples, nvars))
        YY = np.random.uniform(0, 1, (nsamples-1, nvars))
        K_grad = kernel(XX, YY, eval_gradient=True)[1]

        def f(theta):
            kernel.theta = theta
            K = kernel(XX, YY)
            return K
        K_grad_f = _approx_fprime(kernel.theta, f, 1e-8)
        assert np.allclose(K_grad, K_grad_f)

        nvars, nsamples = 2, 4
        kernel = RBF([0.1, 0.2])
        XX = np.random.uniform(0, 1, (nsamples, nvars))

        YY = None
        K_grad = kernel(XX, YY, eval_gradient=True)[1]
        rbf_kernel = RBF([0.1, 0.2])
        assert np.allclose(rbf_kernel(XX, YY, eval_gradient=True)[1],
                           K_grad)

        YY = np.random.uniform(0, 1, (nsamples-1, nvars))

        def f(theta):
            kernel.theta = theta
            K = kernel(XX, YY)
            return K
        K_grad_fd = _approx_fprime(kernel.theta, f, 1e-8)
        K_grad = kernel(XX, YY, eval_gradient=True)[1]
        assert np.allclose(K_grad, K_grad_fd, atol=1e-6)

    def _check_multilevel_kernel(self, nvars, nmodels):
        nsamples = int(1e6)
        nsamples_per_model = [4**nvars, 3**nvars, 2**nvars][:nmodels]
        length_scales = np.hstack(
            [np.linspace(0.5, 1.1, nvars) for nn in range(nmodels)])
        print(length_scales)
        scalings = np.arange(2, 2+nmodels-1)/3
        # shared indices of samples from lower level
        shared_idx_list = [
            np.random.permutation(
                np.arange(nsamples_per_model[nn-1]))[:nsamples_per_model[nn]]
            for nn in range(1, nmodels)]
        XX_list = [cartesian_product(
            [np.linspace(-1, 1, int(nsamples_per_model[0]**(1/nvars)))]*nvars).T]
        for nn in range(1, nmodels):
            XX_list += [XX_list[nn-1][shared_idx_list[nn-1]]]

        assert nmodels*nvars == len(length_scales)
        # kernels = [
        #     Matern(length_scales[nn], nu=np.inf)
        #     for nn in range(nmodels)]
        kernels = [RBF(length_scales[nn*nvars:(nn+1)*nvars])
                   for nn in range(nmodels)]

        samples_list = [
            np.random.normal(0, 1, (nsamples_per_model[nn], nsamples))
            for nn in range(nmodels)]

        # Sample from discrepancies
        DD_list = [
            np.linalg.cholesky(kernels[nn](XX_list[nn])).dot(
                samples_list[nn]) for nn in range(nmodels)]
        # cannout use kernel1(XX2, XX2) here because this will generate
        # different samples to those used in YY1
        YY_list = [None for nn in range(nmodels)]
        YY_list[0] = DD_list[0]
        for nn in range(1, nmodels):
            YY_list[nn] = (
                scalings[nn-1]*YY_list[nn-1][shared_idx_list[nn-1], :] +
                DD_list[nn])

        assert np.allclose(
            YY_list[0][shared_idx_list[0]],
            (YY_list[1]-DD_list[1])/scalings[0])
        if nmodels > 2:
            assert np.allclose(
                YY_list[1][shared_idx_list[1]],
                (YY_list[2]-DD_list[2])/scalings[1])

        for nn in range(nmodels):
            assert np.allclose(YY_list[nn].mean(axis=1), 0, atol=1e-2)
        YY_centered_list = [YY_list[nn]-YY_list[nn].mean(axis=1)[:, None]
                            for nn in range(nmodels)]

        cov = [[None for nn in range(nmodels)] for kk in range(nmodels)]
        for nn in range(nmodels):
            cov[nn][nn] = np.cov(YY_list[nn])
            assert np.allclose(
                YY_centered_list[nn].dot(YY_centered_list[nn].T)/(nsamples-1),
                cov[nn][nn])
            
        assert np.allclose(cov[0][0], kernels[0](XX_list[0]), atol=1e-2)
        assert np.allclose(
            cov[1][1],
            scalings[0]**2*kernels[0](XX_list[1])+kernels[1](XX_list[1]),
            atol=1e-2)
        if nmodels > 2:
            assert np.allclose(
                cov[2][2], scalings[:2].prod()**2*kernels[0](XX_list[2]) +
                scalings[1]**2*kernels[1](XX_list[2]) +
                kernels[2](XX_list[2]),
                atol=1e-2)

        cov[0][1] = YY_centered_list[0].dot(
            YY_centered_list[1].T)/(nsamples-1)
        assert np.allclose(
            cov[0][1], scalings[0]*kernels[0](
                XX_list[0], XX_list[1]), atol=1e-2)

        if nmodels > 2:
            cov[0][2] = YY_centered_list[0].dot(
                YY_centered_list[2].T)/(nsamples-1)
            assert np.allclose(
                cov[0][2], scalings[:2].prod()*kernels[0](
                    XX_list[0], XX_list[2]), atol=1e-2)
            cov[1][2] = YY_centered_list[1].dot(
                YY_centered_list[2].T)/(nsamples-1)
            assert np.allclose(
                cov[1][2], scalings[0]**2*scalings[1]*kernels[0](
                    XX_list[1], XX_list[2])+scalings[1]*kernels[1](
                    XX_list[1], XX_list[2]), atol=1e-2)

        length_scale_bounds = [(1e-1, 10)]*(nvars*nmodels)
        kernel_scalings = [
            MonomialScaling(nvars, 0) for nn in range(nmodels-1)]
        mlgp_kernel = MultilevelKernel(
            nvars, kernels, kernel_scalings, length_scale=length_scales,
            length_scale_bounds=length_scale_bounds,
            rho=scalings)
        mlgp_kernel.set_nsamples_per_model(nsamples_per_model)

        # print(mlgp_kernel)
        XX_train = np.vstack(XX_list)
        # np.set_printoptions(linewidth=500)
        K = mlgp_kernel(XX_train)
        for nn in range(nmodels):
            assert np.allclose(
                K[sum(nsamples_per_model[:nn]):sum(nsamples_per_model[:nn+1]),
                  sum(nsamples_per_model[:nn]):sum(nsamples_per_model[:nn+1])],
                cov[nn][nn], atol=1e-2)

        assert np.allclose(
            K[:nsamples_per_model[0],
              nsamples_per_model[0]:sum(nsamples_per_model[:2])],
            cov[0][1], atol=2e-2)

        if nmodels > 2:
            assert np.allclose(
                K[:nsamples_per_model[0], sum(nsamples_per_model[:2]):],
                cov[0][2], atol=2e-2)
            assert np.allclose(
                K[nsamples_per_model[0]:sum(nsamples_per_model[:2]),
                  sum(nsamples_per_model[:2]):],
                cov[1][2], atol=2e-2)

        nsamples_test = 6
        XX_train = np.vstack(XX_list)
        XX_test = cartesian_product(
            [np.linspace(-1, 1, nsamples_test)]*nvars).T
        print(XX_test.shape, XX_train.shape)
        K = mlgp_kernel(XX_test, XX_train)
        assert np.allclose(K[:, :XX_list[0].shape[0]],
                           scalings.prod()*kernels[0](XX_test, XX_list[0]))
        if nmodels == 2:
            tnm1_prime = scalings[0]*kernels[0](XX_test, XX_list[1])
            assert np.allclose(
                K[:, nsamples_per_model[0]:sum(nsamples_per_model[:2])],
                scalings[0]*tnm1_prime +
                kernels[1](XX_test, XX_list[1]))
        elif nmodels == 3:
            t2m1_prime = scalings[1]*scalings[0]*kernels[0](
                XX_test, XX_list[1])
            assert np.allclose(
                K[:, nsamples_per_model[0]:sum(nsamples_per_model[:2])],
                scalings[0]*t2m1_prime +
                scalings[1]*kernels[1](XX_test, XX_list[1]))

            t2m1_prime = scalings[1]*scalings[0]*kernels[0](
                XX_test, XX_list[2])
            t3m1_prime = (scalings[0]*t2m1_prime +
                          scalings[1]*kernels[1](XX_test, XX_list[2]))
            assert np.allclose(
                K[:, sum(nsamples_per_model[:2]):],
                scalings[1]*t3m1_prime +
                kernels[2](XX_test, XX_list[2]))

        # samples_test = [np.random.normal(0, 1, (nsamples_test, nsamples))
        #                 for nn in range(nmodels)]
        # # to evaluate lower fidelity model change kernel index
        # DD2_list = [
        #     np.linalg.cholesky(kernels[nn](XX_test)).dot(
        #         samples_test[nn]) for nn in range(nmodels)]
        # YY2_test = DD2_list[0]
        # for nn in range(1, nmodels):
        #     YY2_test = (
        #         scalings[nn-1]*YY2_test + DD2_list[nn])
        # YY2_test_centered = YY2_test-YY2_test.mean(axis=1)[:, None]
        # t0 = YY2_test_centered.dot(YY_centered_list[0].T)/(nsamples-1)
        # print(t0)
        K = mlgp_kernel(XX_list[nmodels-1], XX_train)
        t0 = YY_centered_list[nmodels-1].dot(YY_centered_list[0].T)/(
            nsamples-1)
        assert np.allclose(t0, K[:, :nsamples_per_model[0]], atol=1e-2)
        t1 = YY_centered_list[nmodels-1].dot(YY_centered_list[1].T)/(
            nsamples-1)
        assert np.allclose(
            t1, K[:, nsamples_per_model[0]:sum(nsamples_per_model[:2])],
            atol=1e-2)
        if nmodels > 2:
            t2 = YY_centered_list[nmodels-1].dot(YY_centered_list[2].T)/(
                nsamples-1)
            assert np.allclose(
                t2, K[:, sum(nsamples_per_model[:2]):], atol=1e-2)

        def f(theta):
            mlgp_kernel.theta = theta
            K = mlgp_kernel(XX_train)
            return K
        from sklearn.gaussian_process.kernels import _approx_fprime
        K_grad_fd = _approx_fprime(mlgp_kernel.theta, f, 1e-8)
        K_grad = mlgp_kernel(XX_train, eval_gradient=True)[1]
        # idx = 3
        # print(K_grad[:, :, idx])
        # print(K_grad_fd[:, :, idx])
        # np.set_printoptions(precision=3, suppress=True)
        # print(np.absolute(K_grad[:, :, idx]-K_grad_fd[:, :, idx]))#.max())
        # print(K_grad_fd.shape, K_grad.shape)
        assert np.allclose(K_grad, K_grad_fd, atol=1e-6)

        diags = []
        for nn in range(mlgp_kernel.nmodels):
            mlgp_kernel.model_eval_id = nn
            diags.append(mlgp_kernel.diag(XX_list[nn]))
        assert np.allclose(np.diag(mlgp_kernel(XX_train)), np.hstack(diags))

    def test_multilevel_kernel(self):
        self._check_multilevel_kernel(1, 2)
        self._check_multilevel_kernel(1, 3)
        self._check_multilevel_kernel(2, 2)
        self._check_multilevel_kernel(2, 3)

    def _check_multifidelity_peer_kernel(self, nvars, degree):
        np.set_printoptions(linewidth=2000, precision=3)
        nsamples = int(1e6)
        nmodels = 3
        nsamples_per_model = [3**nvars, 3**nvars, 2**nvars]
        length_scales = np.hstack(
            [np.linspace(0.5, 1.1, nvars)/(nn+1) for nn in range(nmodels)])
        shared_idx_list = [
            np.random.permutation(
                np.arange(nsamples_per_model[nn-1]))[:nsamples_per_model[nn]]
            for nn in range(1, nmodels)]

        XX_list = [cartesian_product(
            [np.linspace(-1, 1, int(nsamples_per_model[0]**(1/nvars)))]*nvars).T]
        for nn in range(1, nmodels):
            XX_list += [XX_list[nn-1][shared_idx_list[nn-1]]]
        XX_train = np.vstack(XX_list)

        kernels = [RBF(length_scales[nn*nvars:(nn+1)*nvars])
                   for nn in range(nmodels)]
        kernel_scalings = [
            MonomialScaling(nvars, degree) for nn in range(nmodels-1)]
        rho = np.random.uniform(
            0.5, 0.9, sum([s.nhyperparams for s in kernel_scalings]))
        rho[::2] = 0.25 # hack
        rho[1::2] = 0.5
        # rho[::2] = [0.5, 0.75] # hack
        # rho[1::2] = 0
        length_scale_bounds = (1e-1, 10)
        kernel = MultifidelityPeerKernel(
            nvars, kernels, kernel_scalings, length_scale=length_scales,
            length_scale_bounds=length_scale_bounds,
            rho=rho)
        kernel.set_nsamples_per_model(nsamples_per_model)

        kernel._set_scaling_hyperparameters()
        scalings = [
            kernel_scalings[nn](XX_train)[0]
            for nn in range(nmodels-1)]
        # may need to ensure evaluations of scalings are in [0, 1]
        splits = np.split(
            np.arange(kernel.nsamples_per_model.sum()),
            np.cumsum(kernel.nsamples_per_model))[:-1]

        samples_list = [
            np.random.normal(0, 1, (nsamples_per_model[nn], nsamples))
            for nn in range(nmodels)]
        # Sample from discrepancies
        DD_list = [
            np.linalg.cholesky(kernels[nn](XX_list[nn])).dot(
                samples_list[nn]) for nn in range(nmodels)]
        # cannout use kernel1(XX2, XX2) here because this will generate
        # different samples to those used in YY1
        YY_list = [None for nn in range(nmodels)]
        for nn in range(nmodels):
            YY_list[nn] = DD_list[nn]
        idx0 = shared_idx_list[0][shared_idx_list[1]]
        YY_list[nmodels-1] = (
            DD_list[nmodels-1] +
            scalings[0][splits[nmodels-1]]*YY_list[0][idx0, :])
        YY_list[nmodels-1] += (
            scalings[1][splits[nmodels-1]] *
            YY_list[1][shared_idx_list[1], :])
        YY_centered_list = [YY_list[nn]-YY_list[nn].mean(axis=1)[:, None]
                            for nn in range(nmodels)]
        cov = [[None for nn in range(nmodels)] for kk in range(nmodels)]
        for nn in range(nmodels):
            cov[nn][nn] = np.cov(YY_list[nn])

        cov[0][1] = YY_centered_list[0].dot(
            YY_centered_list[1].T)/(nsamples-1)
        cov[0][2] = YY_centered_list[0].dot(
            YY_centered_list[2].T)/(nsamples-1)
        cov[1][2] = YY_centered_list[1].dot(
            YY_centered_list[2].T)/(nsamples-1)
        cov[1][0] = cov[0][1].T
        cov[2][0] = cov[0][2].T
        cov[2][1] = cov[1][2].T

        assert np.allclose(cov[0][0], kernels[0](XX_list[0]), atol=1e-2)
        assert np.allclose(cov[1][1], kernels[1](XX_list[1]), atol=1e-2)

        assert np.allclose(
            cov[2][2],
            scalings[0][splits[2]].dot(scalings[0][splits[2]].T) *
            kernels[0](XX_list[2]) +
            scalings[1][splits[2]].dot(scalings[1][splits[2]].T) *
            kernels[1](XX_list[2])+kernels[2](XX_list[2]),
            atol=1e-2)
        assert np.allclose(
            cov[0][2],
            # np.ones((nsamples_per_model[0], 1)).dot(scalings[0][splits[2]].T) *
            # kernels[0](XX_list[0], XX_list[2]),
            kernels[0](XX_list[0], XX_list[2])*scalings[0][splits[2]][:, 0],
            atol=1e-2)
        assert np.allclose(
            cov[1][2],
            kernels[1](XX_list[1], XX_list[2])*scalings[1][splits[2]][:, 0],
            atol=1e-2)

        def f(theta):
            kernel.theta = theta
            K = kernel(XX_train)
            return K

        assert np.allclose(
            kernel(XX_train), np.vstack([np.hstack(row) for row in cov]),
            atol=1e-2)

        from sklearn.gaussian_process.kernels import _approx_fprime
        K_grad_fd = _approx_fprime(kernel.theta, f, 1e-8)
        K_grad = kernel(XX_train, eval_gradient=True)[1]
        # print(K_grad.shape)
        # idx = 6
        # print(K_grad[:, :, idx])
        # print(K_grad_fd[:, :, idx])
        # print(np.linalg.norm(K_grad[:, :, idx]-K_grad_fd[:, :, idx]))
        # np.set_printoptions(precision=3, suppress=True)
        # print(np.absolute(K_grad[:, :, idx]-K_grad_fd[:, :, idx]))#.max())
        # print(K_grad_fd.shape, K_grad.shape)
        assert np.allclose(K_grad, K_grad_fd, atol=1e-6)

        diags = []
        for nn in range(kernel.nmodels):
            kernel.model_eval_id = nn
            diags.append(kernel.diag(XX_list[nn]))
        assert np.allclose(np.diag(kernel(XX_train)), np.hstack(diags))

    def test_multifidelity_peer_kernel(self):
        #self._check_multifidelity_peer_kernel(1, 0)
        #self._check_multifidelity_peer_kernel(2, 0)
        self._check_multifidelity_peer_kernel(1, 1)
        self._check_multifidelity_peer_kernel(2, 1)

    def _check_multitask_kernel(self, nvars):
        np.set_printoptions(linewidth=2000, precision=3)
        nsamples = int(1e6)
        nmodels = 3
        nsamples_per_model = [3**nvars, 3**nvars, 2**nvars]
        length_scales = np.hstack(
            [np.linspace(0.5, 1.1, nvars)/(nn+1) for nn in range(nmodels)])
        scalings = np.arange(2, 2+nmodels-1)/3-0.1
        shared_idx_list = [
            np.random.permutation(
                np.arange(nsamples_per_model[nn-1]))[:nsamples_per_model[nn]]
            for nn in range(1, nmodels)]

        XX_list = [cartesian_product(
            [np.linspace(-1, 1, int(nsamples_per_model[0]**(1/nvars)))]*nvars).T]
        for nn in range(1, nmodels):
            XX_list += [XX_list[nn-1][shared_idx_list[nn-1]]]
        XX_train = np.vstack(XX_list)

        kernels = [RBF(length_scales[nn*nvars:(nn+1)*nvars])
                   for nn in range(nmodels)]
        length_scale_bounds = (1e-1, 10)
        kernel_scalings = [
            MonomialScaling(nvars, 0) for nn in range(nmodels-1)]
        kernel = MultiTaskKernel(
            nvars, kernels, kernel_scalings,
            length_scale=length_scales,
            length_scale_bounds=length_scale_bounds,
            rho=scalings)
        kernel.set_nsamples_per_model(nsamples_per_model)

        samples_list = [
            np.random.normal(0, 1, (nsamples_per_model[nn], nsamples))
            for nn in range(nmodels)]
        # Sample from discrepancies
        DD_list = [
            np.linalg.cholesky(kernels[nn](XX_list[nn])).dot(
                samples_list[nn]) for nn in range(nmodels)]
        # cannout use kernel1(XX2, XX2) here because this will generate
        # different samples to those used in YY1
        YY_list = [None for nn in range(nmodels)]

        YY_list[0] = DD_list[0]
        shared_idx = np.arange(nsamples_per_model[0])
        for nn in range(1, nmodels):
            shared_idx = shared_idx[shared_idx_list[nn-1]]
            print(shared_idx)
            YY_list[nn] = (
                DD_list[nn]+scalings[nn-1]*YY_list[0][shared_idx, :])

        YY_centered_list = [YY_list[nn]-YY_list[nn].mean(axis=1)[:, None]
                            for nn in range(nmodels)]
        cov = [[None for nn in range(nmodels)] for kk in range(nmodels)]
        for nn in range(nmodels):
            cov[nn][nn] = np.cov(YY_list[nn])

        cov[0][1] = YY_centered_list[0].dot(
            YY_centered_list[1].T)/(nsamples-1)
        cov[0][2] = YY_centered_list[0].dot(
            YY_centered_list[2].T)/(nsamples-1)
        cov[1][2] = YY_centered_list[1].dot(
            YY_centered_list[2].T)/(nsamples-1)
        cov[1][0] = cov[0][1].T
        cov[2][0] = cov[0][2].T
        cov[2][1] = cov[1][2].T

        assert np.allclose(cov[0][0], kernels[0](XX_list[0]), atol=1e-2)
        assert np.allclose(
            cov[1][1], scalings[0]**2*kernels[0](XX_list[1]) +
            kernels[1](XX_list[1]), atol=1e-2)
        assert np.allclose(
            cov[2][2], scalings[1]**2*kernels[0](XX_list[2]) +
            kernels[2](XX_list[2]), atol=1e-2)
        assert np.allclose(
            cov[0][2], scalings[1]*kernels[0](XX_list[0], XX_list[2]),
            atol=1e-2)
        assert np.allclose(
            cov[1][2],
            scalings[0]*scalings[1]*kernels[0](XX_list[1], XX_list[2]),
            atol=1e-2)

        def f(theta):
            kernel.theta = theta
            K = kernel(XX_train)
            return K
        assert np.allclose(
            kernel(XX_train), np.vstack([np.hstack(row) for row in cov]),
            atol=1e-2)

        from sklearn.gaussian_process.kernels import _approx_fprime
        K_grad_fd = _approx_fprime(kernel.theta, f, 1e-8)
        K_grad = kernel(XX_train, eval_gradient=True)[1]
        # idx = 3
        # print(K_grad[:, :, idx])
        # print(K_grad_fd[:, :, idx])
        # print(np.linalg.norm(K_grad[:, :, idx]-K_grad_fd[:, :, idx]))
        # np.set_printoptions(precision=3, suppress=True)
        # print(np.absolute(K_grad[:, :, idx]-K_grad_fd[:, :, idx]))#.max())
        # print(K_grad_fd.shape, K_grad.shape)
        assert np.allclose(K_grad, K_grad_fd, atol=1e-6)

        diags = []
        for nn in range(kernel.nmodels):
            kernel.model_eval_id = nn
            diags.append(kernel.diag(XX_list[nn]))
        assert np.allclose(np.diag(kernel(XX_train)), np.hstack(diags))

    def test_multitask_kernel(self):
        self._check_multitask_kernel(1)
        self._check_multitask_kernel(2)

    def _check_2_models(self, nested):
        # TODO Add Test which builds gp on two models data separately when
        # data2 is subset data and hyperparameters are fixed.
        # Then Gp should just be sum of separate GPs.

        lb, ub = 0, 1
        nvars, nmodels = 1, 2
        true_rho = [2]

        def f1(x):
            return ((x.T*6-2)**2)*np.sin((x.T*6-2)*2)/5

        def f2(x):
            return true_rho[0]*f1(x)+((x.T-0.5)*1. - 5)/5

        if not nested:
            x2 = np.array([[0.0], [0.4], [0.6], [1.0]]).T
            x1 = np.array([[0.1], [0.2], [0.3], [0.5], [0.7],
                           [0.8], [0.9], [0.0], [0.4], [0.6], [1.0]]).T
        else:
            # nested
            x1 = np.array([[0.1], [0.2], [0.3], [0.5], [0.7],
                           [0.8], [0.9], [0.0], [0.4], [0.6], [1.0]]).T
            x2 = x1[:, [0, 2, 4, 6]]
            # x1 = x1[:, ::2]
            # x2 = x1[:, [0, 2]]

        train_samples = [x1, x2]
        train_values = [f(x) for f, x in zip([f1, f2], train_samples)]
        nsamples_per_model = [s.shape[1] for s in train_samples]

        rho = np.ones(nmodels-1)
        length_scale = [1]*(nmodels*(nvars))
        length_scale_bounds = [(1e-1, 10)]*(nmodels*nvars)

        # length_scale_bounds='fixed'
        kernels = [RBF(0.1) for nn in range(nmodels)]
        kernel_scalings = [
            MonomialScaling(nvars, 0) for nn in range(nmodels-1)]
        rho = np.random.uniform(
            0.5, 0.9, sum([s.nhyperparams for s in kernel_scalings]))
        mlgp_kernel = MultilevelKernel(
            nvars, kernels, kernel_scalings, length_scale=length_scale,
            length_scale_bounds=length_scale_bounds, rho=rho)

        gp = MultilevelGaussianProcess(mlgp_kernel)
        gp.set_data(train_samples, train_values)
        gp.fit()
        print(gp.kernel_.rho-true_rho)
        assert np.allclose(gp.kernel_.rho, true_rho, atol=4e-3)
        print(gp.kernel_)
        # point used to evaluate diag does not matter for stationary kernels
        # sf_var = gp.kernel_.diag(np.zeros((1, nvars)))
        # shf_kernel = RBF(
        #     length_scale_bounds=length_scale_bounds[:nvars])*ConstantKernel(
        #         sf_var, constant_value_bounds="fixed")
        # shf_gp = GaussianProcess(shf_kernel)
        # shf_gp.fit(train_samples[1], train_values[1])
        slf_kernel = RBF(length_scale_bounds=length_scale_bounds[:nvars])
        slf_gp = GaussianProcess(slf_kernel)
        slf_gp.fit(train_samples[0], train_values[0])

        # print('ml')
        # print(get_gp_samples_kernel(gp).length_scale[-1], true_rho)

        # xx = np.linspace(lb, ub, 2**8+1)[np.newaxis, :]
        xx = np.linspace(lb, ub, 2**3+1)[np.newaxis, :]

        hf_gp_mean, hf_gp_std = gp(xx, return_std=True)
        slf_gp_mean, slf_gp_std = slf_gp(xx, return_std=True)
        lf_gp_mean, lf_gp_std = gp(xx, return_std=True, model_eval_id=0)
        # shf_gp_mean, shf_gp_std = shf_gp(xx, return_std=True)

        # print(np.abs(lf_gp_mean-slf_gp_mean).max())
        assert np.allclose(lf_gp_mean, slf_gp_mean, atol=1e-5)

        # print(hf_gp_mean-f2(xx))
        assert np.allclose(f2(xx), hf_gp_mean, atol=5e-2)

        x1 = np.linspace(lb, ub, 30)[None, :]
        x2 = np.linspace(lb, ub, 5)[None, :]
        train_samples = [x1, x2]
        train_values = [f(x) for f, x in zip([f1, f2], train_samples)]
        nsamples_per_model = [s.shape[1] for s in train_samples]

        rho = np.ones(nmodels-1)
        length_scale = [1]*(nmodels*(nvars))
        length_scale_bounds = (1e-1, 10)

        # length_scale_bounds='fixed'
        kernels = [RBF(0.1) for nn in range(nmodels)]
        mlgp_kernel = MultilevelKernel(
            nvars, kernels, kernel_scalings, length_scale=length_scale,
            length_scale_bounds=length_scale_bounds, rho=rho)

        gp = MultilevelGaussianProcess(mlgp_kernel)
        gp.set_data(train_samples, train_values)
        gp.fit()

        marginals = [stats.uniform(lb, ub-lb)]
        variable = IndependentMarginalsVariable(marginals)
        means = [None, None]
        gp.kernel_.model_eval_id = 0
        means[0] = gp.integrate(variable, 100)[0]
        gp.kernel_.model_eval_id = 1
        means[1] = gp.integrate(variable, 100)[0]
        xx, ww = gauss_jacobi_pts_wts_1D(100, 0, 0)
        xx = (xx[None, :]+1)/2
        true_means = [ww.dot(f1(xx)), ww.dot(f2(xx))]
        print(true_means, means)
        assert np.allclose(true_means, means, rtol=1e-4)

    def test_2_models(self):
        self._check_2_models(True)
        self._check_2_models(False)

        # TODO add 2d model check

    def test_sequential_multilevel_gaussian_process(self):
        lb, ub = 0, 1
        nvars, nmodels = 1, 2
        n_restarts_optimizer = 10
        true_rho = [2]

        def f1(x):
            vals = ((x.sum(axis=0)*6-2)**2)*np.sin((x.sum(axis=0)*6-2)*2)/5
            return vals[:, None]

        def f2(x):
            vals = true_rho[0]*f1(x)+(((x.sum(axis=0)-0.5)*1. - 5)/5)[:, None]
            return vals

        x1 = cartesian_product([np.linspace(lb, ub, 2**5+1)]*nvars)
        x2 = cartesian_product([np.linspace(lb, ub, 2**2+1)]*nvars)

        train_samples = [x1, x2]
        train_values = [f(x) for f, x in zip([f1, f2], train_samples)]

        length_scale_bounds = (1e-1, 1)
        sml_kernels = [
            RBF(length_scale=0.1, length_scale_bounds=length_scale_bounds)
            for ii in range(nmodels)]
        sml_gp = SequentialMultilevelGaussianProcess(
            sml_kernels, n_restarts_optimizer=n_restarts_optimizer,
            default_rho=[1.0])
        sml_gp.set_data(train_samples, train_values)
        sml_gp.fit()

        print([g.kernel_ for g in sml_gp._gps])

        xx = np.linspace(0, 1, 101)[None, :]
        print(np.abs(f1(xx)-sml_gp(xx, model_idx=[0])[0]).max())
        assert np.allclose(f1(xx), sml_gp(xx, model_idx=[0])[0], atol=1e-3)
        error = np.linalg.norm(
            (f2(xx)-sml_gp(xx, model_idx=[1])[0]))/np.linalg.norm(f2(xx))
        print(error)
        assert error < 7e-2

        # from pyapprox.util.configure_plots import plt
        # fig, axs = plt.subplots(1, 3, figsize=(3*8, 6))
        # axs[0].plot(xx[0, :], f2(xx), 'k--')
        # axs[0].plot(xx[0, :], sml_gp(xx)[0], ':b')
        # axs[0].plot(train_samples[1][0, :], f2(train_samples[1]), 'ko')
        # axs[1].plot(xx[0, :], f1(xx), 'k-')
        # axs[1].plot(train_samples[0][0, :], f1(train_samples[0]), 'ko')
        # axs[1].plot(xx[0, :], sml_gp(xx, model_idx=[0])[0], ':b')
        # rho = sml_gp.rho[0]
        # axs[2].plot(xx[0, :], f2(xx)-rho*f1(xx), 'k--')
        # axs[2].plot(xx[0, :], sml_gp._gps[1](xx), ':b')
        # axs[2].plot(
        #     train_samples[1][0, :],
        #     f2(train_samples[1])-rho*sml_gp._gps[0](train_samples[1]), 'ko')
        # plt.show()

    def test_greedy_multilevel_sampler(self):
        nmodels, nvars = 2, 1
        nquad_samples = 40
        ncandidate_samples_per_model = 11
        model_costs = [1, 2.0]
        variable = IndependentMarginalsVariable(
            [stats.uniform(0, 1) for ii in range(nvars)])
        sampler = GreedyMultilevelIntegratedVarianceSampler(
            nmodels, nvars, nquad_samples, ncandidate_samples_per_model,
            variable.rvs, variable, use_gauss_quadrature=True, econ=False,
            compute_cond_nums=False, nugget=0, model_costs=model_costs)

        # important not to start with rho = 1
        # or kernel matrix will be singular
        rho = np.full((nmodels-1), 0.8)

        length_scale = [.1]*(nmodels*nvars)
        length_scale_bounds = "fixed"

        # length_scale_bounds='fixed'
        # kernels = [RBF(0.1) for nn in range(nmodels)]
        kernels = [RBF(0.1), ConstantKernel(0.1, "fixed")*RBF(0.1)]
        kernel_scalings = [
            MonomialScaling(nvars, 0) for nn in range(nmodels-1)]
        kernel = MultilevelKernel(
            nvars, kernels, kernel_scalings,
            length_scale=length_scale,
            length_scale_bounds=length_scale_bounds, rho=rho,
            rho_bounds="fixed")

        np.set_printoptions(linewidth=1000)
        num_samples = 5 #20

        sampler.set_kernel(kernel)
        samples = sampler(num_samples)[0]
        samples_per_model = np.split(
            samples, np.cumsum(sampler.nsamples_per_model).astype(int)[:-1],
            axis=1)
        # print(sampler.pivots)
        # print(samples_per_model)
        # print(sampler.A)

        samples_per_model_2 = sampler.samples_per_model(sampler.pivots)
        for ii in range(sampler.nmodels):
            assert np.allclose(samples_per_model[ii], samples_per_model_2[ii])

        xx_quad, ww_quad = integrate(
            "tensorproduct", variable, rule="gauss", levels=nquad_samples-1)

        prior_ivar = (kernel.diag(xx_quad.T).dot(ww_quad[:, 0]))

        prev_best_ivar = prior_ivar
        gp = MultilevelGaussianProcess(kernel)
        for ii in range(len(sampler.pivots)):
            # print("$$$", ii)
            samples_per_model = sampler.samples_per_model(
                sampler.pivots[:ii])
            obj_vals, ivars = [], []
            for jj in range(sampler.candidate_samples.shape[1]):
                if jj in sampler.pivots[:ii]:
                    ivars.append(np.inf)
                    obj_vals.append(np.inf)
                    continue
                ivar_delta = sampler._ivar_delta(jj, sampler.pivots[:ii])
                model_id = sampler._model_id(jj)
                samples_per_model_jj = copy.deepcopy(samples_per_model)
                samples_per_model_jj[model_id] = np.hstack((
                    samples_per_model[model_id],
                    sampler.candidate_samples[:, jj:jj+1]))
                gp.set_data(
                    samples_per_model_jj,
                    [s.T*0 for s in samples_per_model_jj])
                gp.fit()
                gp_mean, gp_std = gp(
                    xx_quad, return_std=True, model_eval_id=sampler.nmodels-1)
                ivars.append((gp_std**2).dot(ww_quad[:, 0]))
                # print(jj)
                # print((prior_ivar-ivar_delta, ivars[-1]))
                assert np.allclose(prior_ivar-ivar_delta, ivars[-1])
                obj_vals.append(
                    (ivars[-1]-prev_best_ivar)/sampler.model_costs[model_id])
                # print(obj_vals[-1])

            next_index = np.argmin(obj_vals)
            prev_best_ivar = ivars[next_index]
            # print(next_index, sampler.pivots)
            assert np.allclose(next_index, sampler.pivots[ii])

        gp = MultilevelGaussianProcess(kernel)
        gp.set_data(samples_per_model, [s.T*0 for s in samples_per_model])
        gp.fit()
        prior_kwargs = {"color": "gray", "alpha": 0.3}
        gp.plot_1d(101, [0, 1], prior_fill_kwargs=prior_kwargs)
        import matplotlib.pyplot as plt
        plt.plot(samples_per_model[-1][0], samples_per_model[-1][0]*0, 'o',
                 ms=20)
        plt.plot(samples_per_model[0][0], samples_per_model[0][0]*0, 'ks')
        # plt.show()

    def _check_integrate_multifidelity_gp(self, kernel_type, nmodels, nvars):
        kernels = [RBF(0.1), RBF(0.2), 0.1*RBF(0.1)][-nmodels:]
        length_scale = [.1]*(nmodels*nvars)
        length_scale_bounds =[(1e-3, 1)]*(nmodels*nvars)
        rho = np.full((nmodels-1), 0.8)
        lb, ub = 0, 1
        variable = IndependentMarginalsVariable(
            [stats.uniform(lb, ub-lb) for ii in range(nvars)])
        if kernel_type == "multilevel":
            models = self._setup_multilevel_model_ensemble(rho)[-nmodels:]
            Kernel = MultilevelKernel
        elif kernel_type == "peer":
            models = self._setup_peer_ensemble(rho)[-nmodels:]
            Kernel = MultifidelityPeerKernel
        kernel = Kernel(
            nvars, [None for ii in range(nmodels)], kernels,
            length_scale=length_scale,
            length_scale_bounds=length_scale_bounds, rho=rho,
            rho_bounds="fixed")
        print(kernel)

        x1 = np.linspace(lb, ub, 20)[None, :]
        x2 = np.linspace(lb, ub, 20)[None, :]
        x3 = np.linspace(lb, ub, 10)[None, :]
        train_samples = [x1, x2, x3][-nmodels:]
        train_values = [f(x) for f, x in zip(models, train_samples)]

        gp = MultilevelGaussianProcess(kernel)
        gp.set_data(train_samples, train_values)
        print(gp.kernel.nsamples_per_model)
        gp.fit()
        xx = np.linspace(lb, ub, 101)[None, :]
        ax = gp.plot_1d(101, [lb, ub], plt_kwargs={"color":'k'}, model_eval_id=0)
        ax.plot(xx[0], models[0](xx), 'r--')
        ax.plot(xx[0], models[1](xx), 'b:')
        ax.plot(train_samples[0][0], train_values[0], 'ro')
        from pyapprox.util.configure_plots import plt
        # plt.show()
        means = np.empty(nmodels)
        for nn in range(nmodels):
            gp.kernel_.model_eval_id = nn
            means[nn] = gp.integrate(variable, 100)[0]
        xx, ww = integrate(
            "tensorproduct", variable, {"rule": "leja", "levels": 40})
        ww = ww[:, 0]
        true_means = np.hstack([ww.dot(m(xx)) for m in models])
        print(true_means)
        print(means)
        assert np.allclose(true_means, means, rtol=1e-4)

    def test_integrate_multifidelity_gp(self):
        self._check_integrate_multifidelity_gp("multilevel", 2, 1)
        # self._check_integrate_multifidelity_gp("peer", 2, 1)

    def test_greedy_multifidelity_sampler(self):
        nmodels, nvars = 3, 1
        nquad_samples = 40
        ncandidate_samples_per_model = 11
        model_costs = [1, 1.0, 4.0]
        variable = IndependentMarginalsVariable(
            [stats.uniform(0, 1) for ii in range(nvars)])
        sampler = GreedyMultilevelIntegratedVarianceSampler(
            nmodels, nvars, nquad_samples, ncandidate_samples_per_model,
            variable.rvs, variable, use_gauss_quadrature=True, econ=False,
            compute_cond_nums=False, nugget=0, model_costs=model_costs)

        # important not to start with rho = 1
        # or kernel matrix will be singular
        rho = np.full((nmodels-1), 0.8)

        length_scale = [.1]*(nmodels*nvars)
        length_scale_bounds = "fixed"

        kernels = [RBF(0.1), RBF(0.2), ConstantKernel(0.1, "fixed")*RBF(0.1)]
        kernel_scalings = [
            MonomialScaling(nvars, 0) for nn in range(nmodels-1)]
        kernel = MultifidelityPeerKernel(
            nvars, kernels, kernel_scalings,
            length_scale=length_scale,
            length_scale_bounds=length_scale_bounds, rho=rho,
            rho_bounds="fixed")

        np.set_printoptions(linewidth=1000)
        num_samples = 5 #20

        sampler.set_kernel(kernel)
        samples = sampler(num_samples)[0]
        samples_per_model = np.split(
            samples, np.cumsum(sampler.nsamples_per_model).astype(int)[:-1],
            axis=1)
        # print(sampler.pivots)
        # print(samples_per_model)
        # print(sampler.A)

        samples_per_model_2 = sampler.samples_per_model(sampler.pivots)
        for ii in range(sampler.nmodels):
            assert np.allclose(samples_per_model[ii], samples_per_model_2[ii])

        xx_quad, ww_quad = integrate(
            "tensorproduct", variable, rule="gauss", levels=nquad_samples-1)

        prior_ivar = (kernel.diag(xx_quad.T).dot(ww_quad[:, 0]))

        print(sampler.pivots, ncandidate_samples_per_model)
        prev_best_ivar = prior_ivar
        gp = MultilevelGaussianProcess(kernel)
        for ii in range(len(sampler.pivots)):
            # print("$$$", ii)
            samples_per_model = sampler.samples_per_model(
                sampler.pivots[:ii])
            print(samples_per_model, ii)
            obj_vals, ivars = [], []
            for jj in range(sampler.candidate_samples.shape[1]):
                if jj in sampler.pivots[:ii]:
                    ivars.append(np.inf)
                    obj_vals.append(np.inf)
                    continue
                ivar_delta = sampler._ivar_delta(jj, sampler.pivots[:ii])
                model_id = sampler._model_id(jj)
                samples_per_model_jj = copy.deepcopy(samples_per_model)
                samples_per_model_jj[model_id] = np.hstack((
                    samples_per_model[model_id],
                    sampler.candidate_samples[:, jj:jj+1]))
                gp.set_data(
                    samples_per_model_jj,
                    [s.T*0 for s in samples_per_model_jj])
                gp.fit()
                gp_mean, gp_std = gp(
                    xx_quad, return_std=True, model_eval_id=sampler.nmodels-1)
                ivars.append((gp_std**2).dot(ww_quad[:, 0]))
                # print(jj)
                # print((prior_ivar-ivar_delta, ivars[-1]))
                assert np.allclose(prior_ivar-ivar_delta, ivars[-1])
                obj_vals.append(
                    (ivars[-1]-prev_best_ivar)/sampler.model_costs[model_id])
                # print(obj_vals[-1])

            next_index = np.argmin(obj_vals)
            prev_best_ivar = ivars[next_index]
            # print(next_index, sampler.pivots)
            assert np.allclose(next_index, sampler.pivots[ii])

        gp = MultilevelGaussianProcess(kernel)
        gp.set_data(samples_per_model, [s.T*0 for s in samples_per_model])
        gp.fit()
        prior_kwargs = {"color": "gray", "alpha": 0.3}
        gp.plot_1d(101, [0, 1], prior_fill_kwargs=prior_kwargs)
        import matplotlib.pyplot as plt
        plt.plot(samples_per_model[-1][0], samples_per_model[-1][0]*0, 'o',
                 ms=20)
        for ii in range(nmodels-1):
            plt.plot(samples_per_model[ii][0], samples_per_model[ii][0]*0, 's')
        plt.show()


if __name__ == "__main__":
    multilevel_test_suite = unittest.TestLoader().loadTestsFromTestCase(
        TestMultilevelGaussianProcess)
    unittest.TextTestRunner(verbosity=2).run(multilevel_test_suite)

    # warning normalize_y does not make a lot of sense for multi-fidelity
    # GPs because the mean and std of the data is computed from the low
    # and high-fidelity values
