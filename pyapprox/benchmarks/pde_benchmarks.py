import torch
from functools import partial
from scipy import stats
import numpy as np

from pyapprox.util.utilities import cartesian_product
from pyapprox.pde.autopde.solvers import (
    SteadyStatePDE, SteadyStateAdjointPDE, TransientPDE, TransientFunction
)
from pyapprox.pde.autopde.physics import (
    AdvectionDiffusionReaction
)
from pyapprox.pde.autopde.mesh import (
    full_fun_axis_1, CartesianProductCollocationMesh,
    subdomain_integral_functional, cartesian_mesh_solution_functional,
    final_time_functional
)
from pyapprox.variables import IndependentMarginalsVariable
from pyapprox.pde.karhunen_loeve_expansion import MeshKLE
from pyapprox.interface.wrappers import (
    evaluate_1darray_function_on_2d_array, MultiIndexModel)


def constant_vel_fun(vels, xx):
    return torch.hstack([
        full_fun_axis_1(vels[ii], xx, oned=False) for ii in range(len(vels))])


def gauss_forc_fun(amp, scale, loc, xx):
    loc = torch.as_tensor(loc)
    if loc.ndim == 1:
        loc = loc[:, None]
    return amp*torch.exp(
        -torch.sum((torch.as_tensor(xx)-loc)**2/scale**2, axis=0))[:, None]


def mesh_locations_obs_functional(obs_indices, sol, params):
    return sol[obs_indices]


def transient_multi_index_forcing(source1_args, xx, time=0,
                                  source2_args=None):
    vals = gauss_forc_fun(*source1_args, xx)
    if time == 0:
        return gauss_forc_fun(*source1_args, xx)
    if source2_args is not None:
        vals -= gauss_forc_fun(*source2_args, xx)
    return vals


def negloglike_functional(obs, obs_indices, noise_std, sol, params,
                          ignore_constants=False):
    assert obs.ndim == 1 and sol.ndim == 1
    nobs = obs_indices.shape[0]
    if not ignore_constants:
        tmp = 1/(2*noise_std**2)
        # ll = 0.5*np.log(tmp/np.pi)*nobs
        ll = -nobs/2*np.log(2*noise_std**2*np.pi)
    else:
        ll, tmp = 0, 1
    pred_obs = sol[obs_indices]
    ll += -torch.sum((obs-pred_obs)**2*tmp)
    return -ll


def negloglike_functional_dqdu(obs, obs_indices, noise_std, sol, params,
                               ignore_constants=False):
    if not ignore_constants:
        tmp = 1/(2*noise_std**2)
    else:
        tmp = 1
    pred_obs = sol[obs_indices]
    grad = torch.zeros_like(sol)
    grad[obs_indices] = (obs-pred_obs)*2*tmp
    return -grad


def loglike_functional_dqdp(obs, obs_indices, noise_std, sol, params):
    return params*0


def advection_diffusion_reaction_kle_dRdp(kle, residual, sol, param_vals):
    mesh = residual.mesh
    dmats = [residual.mesh._dmat(dd) for dd in range(mesh.nphys_vars)]
    if kle.use_log:
        # compute gradient of diffusivity with respect to KLE coeff
        assert param_vals.ndim == 1
        kle_vals = kle(param_vals[:, None])
        assert kle_vals.ndim == 2
        dkdp = kle_vals*kle.eig_vecs
    Du = [torch.linalg.multi_dot((dmats[dd], sol))
          for dd in range(mesh.nphys_vars)]
    kDu = [Du[dd][:, None]*dkdp for dd in range(mesh.nphys_vars)]
    dRdp = sum([torch.linalg.multi_dot((dmats[dd], kDu[dd]))
               for dd in range(mesh.nphys_vars)])
    return dRdp


class AdvectionDiffusionReactionKLEModel():
    def __init__(self, mesh, bndry_conds, kle, vel_fun, react_funs, forc_fun,
                 functional, functional_deriv_funs=[None, None],
                 newton_kwargs={}):

        import inspect
        if "mesh" == inspect.getfullargspec(functional).args[0]:
            functional = partial(functional, mesh)
        for ii in range(len(functional_deriv_funs)):
            if (functional_deriv_funs[ii] is not None and
                "mesh" == inspect.getfullargspec(
                    functional_deriv_funs[ii]).args[0]):
                functional_deriv_funs[ii] = partial(
                    functional_deriv_funs[ii], mesh)

        self._newton_kwargs = newton_kwargs
        self._kle = kle
        # TODO pass in parameterized functions for diffusiviy and forcing and
        # reaction and use same process as used for KLE currently

        self._fwd_solver = self._set_forward_solver(
            mesh, bndry_conds, vel_fun, react_funs, forc_fun)
        self._functional = functional
        
        self._mesh_basis_mat = mesh._get_lagrange_basis_mat(
            mesh._canonical_mesh_pts_1d,
            mesh._map_samples_to_canonical_domain(mesh.mesh_pts))

        if issubclass(type(self._fwd_solver), SteadyStatePDE):
            dqdu, dqdp = functional_deriv_funs
            dRdp = partial(advection_diffusion_reaction_kle_dRdp, self._kle)
            self._adj_solver = SteadyStateAdjointPDE(
                self._fwd_solver, self._functional, dqdu, dqdp, dRdp)

    def _set_forward_solver(self, mesh, bndry_conds, vel_fun, react_funs,
                            forc_fun):
        if react_funs is None:
            react_funs = [self._default_react_fun, self._default_react_fun_jac]
        return SteadyStatePDE(AdvectionDiffusionReaction(
            mesh, bndry_conds, partial(full_fun_axis_1, 1), vel_fun,
            react_funs[0], forc_fun, react_funs[1]))

    def _default_react_fun(self, sol):
        return 0*sol

    def _default_react_fun_jac(self, sol):
        return torch.zeros((sol.shape[0], sol.shape[0]))

    def _fast_interpolate(self, values, xx):
        # interpolate assuming need to evaluate all mesh points
        mesh = self._fwd_solver.residual.mesh
        assert xx.shape[1] == mesh.mesh_pts.shape[1]
        assert np.allclose(xx, mesh.mesh_pts)
        interp_vals = torch.linalg.multi_dot((self._mesh_basis_mat, values))
        # assert np.allclose(interp_vals, mesh.interpolate(values, xx))
        return interp_vals

    def _set_random_sample(self, sample):
        self._fwd_solver.residual._diff_fun = partial(
            self._fast_interpolate,
            self._kle(sample[:, None]))

    def _eval(self, sample, jac=False):
        sample_copy = torch.as_tensor(sample.copy())
        self._set_random_sample(sample_copy)
        sol = self._fwd_solver.solve(**self._newton_kwargs)
        qoi = self._functional(sol, sample_copy).numpy()
        if not jac:
            return qoi
        grad = self._adj_solver.compute_gradient(
            lambda r, p: None, sample_copy, **self._newton_kwargs)
        return qoi, grad.detach().numpy().squeeze()

    def __call__(self, samples, jac=False):
        return evaluate_1darray_function_on_2d_array(
            self._eval, samples, jac=jac)


class TransientAdvectionDiffusionReactionKLEModel(
        AdvectionDiffusionReactionKLEModel):
    def __init__(self, mesh, bndry_conds, kle, vel_fun, react_funs, forc_fun,
                 functional, init_sol_fun, init_time, final_time,
                 deltat, butcher_tableau, newton_kwargs={}):
        if callable(init_sol_fun):
            self._init_sol = torch.as_tensor(init_sol_fun(mesh.mesh_pts))
            if self._init_sol.ndim == 2:
                self._init_sol = self._init_sol[:, 0]
        else:
            assert init_sol_fun is None
            self._init_sol = None
        self._init_time = init_time
        self._final_time = final_time
        self._deltat = deltat
        self._butcher_tableau = butcher_tableau
        super().__init__(mesh, bndry_conds, kle, vel_fun, react_funs, forc_fun,
                         functional, newton_kwargs=newton_kwargs)
        self._steady_state_fwd_solver = super()._set_forward_solver(
            mesh, bndry_conds, vel_fun, react_funs, forc_fun)

    def _eval(self, sample, jac=False):
        if jac:
            raise ValueError("jac=True is not supported")
        sample_copy = torch.as_tensor(sample.copy())
        self._set_random_sample(sample_copy)
        if self._init_sol is None:
            self._steady_state_fwd_solver.residual._diff_fun = partial(
                self._fast_interpolate,
                self._kle(sample_copy[:, None]))
            self._fwd_solver.residual._set_time(self._init_time)
            init_sol = self._steady_state_fwd_solver.solve(
                **self._newton_kwargs)
        else:
            init_sol = self._init_sol
            assert False
        sols, times = self._fwd_solver.solve(
            init_sol, 0, self._final_time,
            newton_kwargs=self._newton_kwargs, verbosity=0)
        qoi = self._functional(sols, sample_copy).numpy()
        return qoi

    def _set_forward_solver(self, mesh, bndry_conds, vel_fun, react_funs,
                            forc_fun):
        if react_funs is None:
            react_funs = [self._default_react_fun, self._default_react_fun_jac]
        return TransientPDE(
            AdvectionDiffusionReaction(
                mesh, bndry_conds, partial(full_fun_axis_1, 1), vel_fun,
                react_funs[0], forc_fun, react_funs[1]),
            self._deltat, self._butcher_tableau)


def _setup_advection_diffusion_benchmark(
        amp, scale, loc, length_scale, sigma, nvars, orders, functional,
        functional_deriv_funs=[None, None], kle_args=None,
        newton_kwargs={}, time_scenario=None):
    variable = IndependentMarginalsVariable([stats.norm(0, 1)]*nvars)
    orders = np.asarray(orders, dtype=int)

    domain_bounds = [0, 1, 0, 1]
    mesh = CartesianProductCollocationMesh(domain_bounds, orders)
    bndry_conds = [
        [partial(full_fun_axis_1, 0, oned=False), "D"],
        [partial(full_fun_axis_1, 0, oned=False), "D"],
        [partial(full_fun_axis_1, 0, oned=False), "D"],
        [partial(full_fun_axis_1, 0, oned=False), "D"]]
    react_funs = None
    vel_fun = partial(constant_vel_fun, [5, 0])

    if kle_args is None:
        kle = MeshKLE(mesh.mesh_pts, use_log=True, use_torch=True)
        kle.compute_basis(
            length_scale, sigma=sigma, nterms=nvars)
    else:
        kle = InterpolatedMeshKLE(kle_args[0], kle_args[1], mesh)

    if time_scenario is None:
        forc_fun = partial(gauss_forc_fun, amp, scale, loc)
        model = AdvectionDiffusionReactionKLEModel(
            mesh, bndry_conds, kle, vel_fun, react_funs, forc_fun,
            functional, functional_deriv_funs, newton_kwargs)
    else:
        assert (time_scenario["init_sol_fun"] is None or
                callable(time_scenario["init_sol_fun"]))
        init_sol_fun, final_time, deltat, butcher_tableau = (
            time_scenario["init_sol_fun"], time_scenario["final_time"],
            time_scenario["deltat"], time_scenario["butcher_tableau"])
        forc_fun = partial(
            transient_multi_index_forcing,
            [amp, scale, loc], source2_args=time_scenario["sink"])
        forc_fun = TransientFunction(forc_fun)
        model = TransientAdvectionDiffusionReactionKLEModel(
            mesh, bndry_conds, kle, vel_fun, react_funs, forc_fun,
            functional, init_sol_fun, 0, final_time, deltat, butcher_tableau,
            newton_kwargs)

    return model, variable


class InterpolatedMeshKLE(MeshKLE):
    def __init__(self, kle_mesh, kle, mesh):
        self._kle_mesh = kle_mesh
        self._kle = kle
        self._mesh = mesh

        self._basis_mat = self._kle_mesh._get_lagrange_basis_mat(
            self._kle_mesh._canonical_mesh_pts_1d,
            mesh._map_samples_to_canonical_domain(self._mesh.mesh_pts))

    def _fast_interpolate(self, values, xx):
        assert xx.shape[1] == self._mesh.mesh_pts.shape[1]
        assert np.allclose(xx, self._mesh.mesh_pts)
        interp_vals = torch.linalg.multi_dot((self._basis_mat, values))
        # assert np.allclose(
        #     interp_vals, self._kle_mesh.interpolate(values, xx))
        return interp_vals

    def __call__(self, coef):
        use_log = self._kle.use_log
        self._kle.use_log = False
        vals = self._kle(coef)
        interp_vals = self._fast_interpolate(vals, self._mesh.mesh_pts)
        if use_log:
            interp_vals = np.exp(interp_vals)
        self._kle.use_log = use_log
        return interp_vals


def _setup_inverse_advection_diffusion_benchmark(
        amp, scale, loc, nobs, noise_std, length_scale, sigma, nvars, orders,
        obs_indices=None):

    loc = torch.as_tensor(loc)
    ndof = np.prod(np.asarray(orders)+1)
    if obs_indices is None:
        bndry_indices = np.hstack(
            [np.arange(0, orders[0]+1),
             np.arange(ndof-orders[0]-1, ndof)] +
            [jj*(orders[0]+1) for jj in range(1, orders[1])] +
            [jj*(orders[0]+1)+orders[0] for jj in range(1, orders[1])])
        obs_indices = np.random.permutation(
            np.delete(np.arange(ndof), bndry_indices))[:nobs]
    obs_functional = partial(mesh_locations_obs_functional, obs_indices)
    obs_model, variable = _setup_advection_diffusion_benchmark(
        amp, scale, loc, length_scale, sigma, nvars, orders, obs_functional)

    true_kle_params = variable.rvs(1)
    noise = np.random.normal(0, noise_std, (obs_indices.shape[0]))
    noiseless_obs = obs_model(true_kle_params)
    obs = noiseless_obs[0, :] + noise

    inv_functional = partial(
        negloglike_functional,  torch.as_tensor(obs), obs_indices,
        noise_std)
    dqdu = partial(negloglike_functional_dqdu, torch.as_tensor(obs),
                   obs_indices, noise_std)
    dqdp = partial(loglike_functional_dqdp,  torch.as_tensor(obs),
                   obs_indices, noise_std)
    inv_functional_deriv_funs = [dqdu, dqdp]

    newton_kwargs = {"maxiters": 1, "rel_error": True, "verbosity": 0}
    inv_model, variable = _setup_advection_diffusion_benchmark(
        amp, scale, loc, length_scale, sigma, nvars, orders,
        inv_functional, inv_functional_deriv_funs, newton_kwargs=newton_kwargs)

    return (inv_model, variable, true_kle_params, noiseless_obs, obs,
            obs_indices, obs_model)


def _setup_multi_index_advection_diffusion_benchmark(
        length_scale, sigma, nvars, time_scenario=None,
        functional=None, config_values=None):

    amp, scale = 100.0, 0.1
    loc = torch.tensor([0.25, 0.75])[:, None]

    newton_kwargs = {"maxiters": 1, "rel_error": False}
    if config_values is None:
        config_values = [2*np.arange(1, 11)+1, 2*np.arange(1, 11)+1]

    if functional is None:
        subdomain_bounds = np.array([0.75, 1, 0, 0.25])
        functional = partial(subdomain_integral_functional, subdomain_bounds)
        if time_scenario is not None:
            functional = partial(final_time_functional, functional)
    hf_orders = np.array([config_values[0][-1], config_values[1][-1]])
    if time_scenario is None and len(config_values) != 2:
        msg = "Steady state scenario specified so must provide config_values"
        msg += "for each physical dimension"
        raise ValueError(msg)
    if time_scenario is not None and len(config_values) != 3:
        msg = "Transient scenario specified so must provide config_values"
        msg += "for each physical dimension and time-stepping"
        raise ValueError(msg)
    if time_scenario is not None:
        time_scenario["deltat"] = config_values[2][-1]

    hf_model, variable = _setup_advection_diffusion_benchmark(
        amp, scale, loc, length_scale, sigma, nvars, hf_orders, functional,
        newton_kwargs=newton_kwargs, time_scenario=time_scenario)
    kle_args = [hf_model._fwd_solver.residual.mesh, hf_model._kle]

    def setup_model(config_vals):
        orders = config_vals[:2]
        if time_scenario is not None:
            time_scenario["deltat"] = config_vals[2]
        return _setup_advection_diffusion_benchmark(
            amp, scale, loc, length_scale, sigma, nvars, orders, functional,
            kle_args=kle_args, newton_kwargs=newton_kwargs,
            time_scenario=time_scenario)[0]
    model = MultiIndexModel(setup_model, config_values)
    return model, variable
