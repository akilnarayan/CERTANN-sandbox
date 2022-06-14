import torch
from abc import ABC, abstractmethod
import numpy as np
from functools import partial

from pyapprox.util.utilities import cartesian_product, outer_product
from pyapprox.variables.transforms import _map_hypercube_samples
from pyapprox.surrogates.orthopoly.quadrature import gauss_jacobi_pts_wts_1D
from pyapprox.surrogates.interp.barycentric_interpolation import (
    compute_barycentric_weights_1d, barycentric_interpolation_1d,
    multivariate_barycentric_lagrange_interpolation
)
from pyapprox.pde.spectralcollocation.spectral_collocation import (
    chebyshev_derivative_matrix, lagrange_polynomial_derivative_matrix_2d,
    lagrange_polynomial_derivative_matrix_1d, fourier_derivative_matrix,
    fourier_basis
)
from pyapprox.util.visualization import get_meshgrid_function_data, plt
from pyapprox.pde.autopde.util import newton_solve
from pyapprox.pde.autopde.time_integration import ImplicitRungeKutta


class IntervalMeshBoundary():
    def __init__(self, bndry_name, inactive_coord, tol=1e-15):
        self._bndry_index = {"left": 0, "right": 1}[bndry_name]
        self._inactive_coord = inactive_coord
        self._tol = tol

    def bndry_normals(self, samples):
        normals = np.array([[-1], [1]])
        return np.tile(normals[self._bndry_index, (1, samples.shape[1])]).T

    def quadrature_rule(self):
        return np.ones((1, 1)), np.ones((1, 1))

    def samples_on_boundary(self, samples):
        return np.where(
            np.absolute(self._inactive_coord-samples[0, :]) < self._tol)[0]


class RectangularMeshBoundary():
    def __init__(self, bndry_name, inactive_coord, order, active_bounds,
                 tol=1e-15):
        if len(active_bounds) != 2:
            msg = "Bounds must be specfied for the dimension with the "
            msg += "varying coordinates"
            raise ValueError(msg)

        self._bndry_index = {"left": 0, "right": 1, "bottom": 2, "top": 3}[
            bndry_name]
        self._order = order
        self._active_bounds = active_bounds
        self._inactive_coord = inactive_coord
        self._tol = tol

    def bndry_normals(self, samples):
        normals = np.array([[-1, 0], [1, 0], [0, -1], [0, 1]])
        return np.tile(normals[self._bndry_index, (1, samples.shape[1])]).T

    def quadrature_rule(self):
        nsamples = self._order+3
        xquad_1d, wquad_1d = gauss_jacobi_pts_wts_1D(nsamples, 0, 0)
        xlist = [None, None]
        xlist[int(self._bndry_index < 2)] = xquad_1d
        xlist[int(self._bndry_index >= 2)] = self._inactive_coord
        xquad = cartesian_product(xlist)
        wquad = wquad_1d[:, None]*np.prod(self.bounds[1]-self._bounds[0])
        return xquad, wquad

    def samples_on_boundary(self, samples):
        dd = int(self._bndry_index >= 2)
        indices = np.where(
            np.absolute(self._inactive_coord-samples[dd, :]) < self._tol)[0]
        return indices


def laplace(deriv_mats, quantity):
    vals = 0
    for dd in range(len(deriv_mats)):
        Dmat = deriv_mats[dd]
        vals += torch.linalg.multi_dot((Dmat, Dmat, quantity))
    return vals


def partial_deriv(deriv_mats, quantity, dd):
    return torch.linalg.multi_dot((deriv_mats[dd], quantity))


def grad(deriv_mats, quantity):
    vals = torch.empty(
        (quantity.shape[0], len(deriv_mats)), dtype=torch.double)
    for dd in range(len(deriv_mats)):
        vals[:, dd] = torch.linalg.multi_dot(
            (deriv_mats[dd], quantity))
    return vals


def div(deriv_mats, quantities):
    vals = 0
    assert quantities.shape[1] == len(deriv_mats)
    for dd in range(len(deriv_mats)):
        vals += torch.linalg.multi_dot((deriv_mats[dd], quantities[:, dd]))
    return vals


def dot(quantities1, quantities2):
    vals = 0
    assert quantities1.shape[1] == quantities2.shape[1]
    vals = torch.sum(quantities1*quantities2, dim=1)
    return vals


class CartesianProductCollocationMesh():
    def __init__(self, domain_bounds, orders, bndry_conds, basis_types=None):

        if len(orders) != len(domain_bounds)//2:
            raise ValueError("Order must be specified for each dimension")
        if len(orders) > 2:
            raise ValueError("Only 1D and 2D meshes supported")

        if basis_types is None:
            basis_types = ["C"]*(len(domain_bounds)//2)
        if len(basis_types) != len(domain_bounds)//2:
            raise ValueError("Basis type must be specified for each dimension")
        self._basis_types = basis_types

        super().__init__()
        self._domain_bounds = np.asarray(domain_bounds)
        self._orders = orders
        self.nphys_vars = len(self._domain_bounds)//2

        self._canonical_domain_bounds = np.ones(2*self.nphys_vars)
        self._canonical_domain_bounds[::2] = -1.
        for ii in range(self.nphys_vars):
            if self._basis_types [ii]== "F":
                self._canonical_domain_bounds[2*ii:2*ii+2] = [0, 2*np.pi]

        (self._canonical_mesh_pts_1d, self._canonical_deriv_mats_1d,
         self._canonical_mesh_pts_1d_baryc_weights, self.mesh_pts,
         self._deriv_mats) = (
             self._form_derivative_matrices())

        self._bndrys = self._form_boundaries()
        self._bndry_indices = self._determine_boundary_indices()
        if len(self._bndrys) != len(bndry_conds):
            raise ValueError(
                "Incorrect number of boundary conditions provided")
        for bndry_cond in bndry_conds:
            if bndry_cond[1] not in ["D", "R", "P", None]:
                raise ValueError(
                    "Boundary condition {bndry_cond[1} not supported")
            if (bndry_cond[1] not in [None, "P"] and
                not callable(bndry_cond[0])):
                raise ValueError("Boundary condition must be callable")
        self._bndry_conds = bndry_conds
        self.nunknowns = self.mesh_pts.shape[1]

    def _form_boundaries(self):
        if self.nphys_vars == 1:
            return [IntervalMeshBoundary(name, inactive_coord)
                    for name, inactive_coord in zip(
                            ["left", "right"], self._domain_bounds)]
        return [
            RectangularMeshBoundary(
                name, self._domain_bounds[ii], self._orders[int(ii < 2)],
                self._domain_bounds[2*int(ii < 2): 2*int(ii < 2)+2])
            for ii, name in enumerate(["left", "right", "bottom", "top"])]

    def _determine_boundary_indices(self):
        bndry_indices = [[] for ii in range(2*self.nphys_vars)]
        for ii in range(2*self.nphys_vars):
            bndry_indices[ii] = self._bndrys[ii].samples_on_boundary(
                self.mesh_pts)
        return bndry_indices

    def _map_samples_from_canonical_domain(self, canonical_samples):
        return _map_hypercube_samples(
            canonical_samples, self._canonical_domain_bounds,
            self._domain_bounds)

    def _map_samples_to_canonical_domain(self, samples):
        return _map_hypercube_samples(
            samples, self._domain_bounds, self._canonical_domain_bounds)

    @staticmethod
    def _form_1d_derivative_matrices(order, basis_type):
        if basis_type == "C":
            return chebyshev_derivative_matrix(order)
        elif basis_type == "F":
            return fourier_derivative_matrix(order)
        raise Exception(f"Basis type {basis_type} provided not supported")

    def _form_derivative_matrices(self):
        canonical_mesh_pts_1d, canonical_deriv_mats_1d = [], []
        for ii in range(self.nphys_vars):
            mpts, der_mat = self._form_1d_derivative_matrices(
                self._orders[ii], self._basis_types[ii])
            canonical_mesh_pts_1d.append(mpts)
            canonical_deriv_mats_1d.append(der_mat)

        canonical_mesh_pts_1d_baryc_weights = [
            compute_barycentric_weights_1d(xx) for xx in canonical_mesh_pts_1d]

        mesh_pts = self._map_samples_from_canonical_domain(
            cartesian_product(canonical_mesh_pts_1d))

        if self.nphys_vars == 1:
            canonical_len = (self._canonical_domain_bounds[1]-
                             self._canonical_domain_bounds[0])
            deriv_mats = [
                canonical_deriv_mats_1d[0]*(canonical_len)/(
                    self._domain_bounds[1]-self._domain_bounds[0])]
        else:
            ident_mats = [np.eye(o+1) for o in self._orders]
            # assumes that 2d-mesh_pts varies in x1 faster than x2,
            # e.g. points are
            # [[x11,x21],[x12,x21],[x13,x12],[x11,x22],[x12,x22],...]
            canonical_len = [(self._canonical_domain_bounds[1] -
                              self._canonical_domain_bounds[0]),
                             (self._canonical_domain_bounds[3] -
                              self._canonical_domain_bounds[2])]
            deriv_mats = [
                np.kron(np.eye(self._orders[1]+1),
                        canonical_deriv_mats_1d[0]*canonical_len[0]/(
                            self._domain_bounds[1]-self._domain_bounds[0])),
                np.kron(canonical_deriv_mats_1d[1]*canonical_len[1]/(
                    self._domain_bounds[3]-self._domain_bounds[2]),
                        np.eye(self._orders[0]+1))]
        deriv_mats = [torch.tensor(mat, dtype=torch.double)
                      for mat in deriv_mats]

        return (canonical_mesh_pts_1d, canonical_deriv_mats_1d,
                canonical_mesh_pts_1d_baryc_weights,
                mesh_pts, deriv_mats)

    def _interpolate(self, canonical_abscissa_1d,
                     canonical_barycentric_weights_1d,
                     values, eval_samples):
        if eval_samples.ndim == 1:
            eval_samples = eval_samples[None, :]
            assert eval_samples.shape[0] == self.mesh_pts.shape[0]
        if values.ndim == 1:
            values = values[:, None]
            assert values.ndim == 2
        canonical_eval_samples = self._map_samples_to_canonical_domain(
            eval_samples)
        if np.all([t == "C" for t in self._basis_types]):
            return self._cheby_interpolate(
                canonical_eval_samples, canonical_abscissa_1d,
                canonical_barycentric_weights_1d, values)
        elif np.all([t == "F" for t in self._basis_types]):
            return self._fourier_interpolate(canonical_eval_samples, values)
        raise ValueError("Mixed basis not currently supported")

    def _cheby_interpolate(self, canonical_eval_samples, canonical_abscissa_1d,
                           canonical_barycentric_weights_1d, values):
        interp_vals = multivariate_barycentric_lagrange_interpolation(
            canonical_eval_samples, canonical_abscissa_1d,
            canonical_barycentric_weights_1d, values,
            np.arange(self.nphys_vars))
        return interp_vals

    def _fourier_interpolate(self, canonical_eval_samples, values):
        basis_vals = [
            fourier_basis(o, s)
            for o, s in zip(self._orders, canonical_eval_samples)]
        if self.nphys_vars == 1:
            return basis_vals[0].dot(values)
        return (basis_vals[0]*basis_vals[1]).dot(values)


    def interpolate(self, mesh_values, eval_samples):
        canonical_abscissa_1d = self._canonical_mesh_pts_1d
        return self._interpolate(
            canonical_abscissa_1d, self._canonical_mesh_pts_1d_baryc_weights,
            mesh_values, eval_samples)

    def _plot_2d(self, mesh_values, num_pts_1d=100, ncontour_levels=20,
                 ax=None):
        if ax is None:
            ax = plt.subplots(1, 1, figsize=(8, 6))[1]
        # interpolate values onto plot points

        def fun(x):
            return self.interpolate(mesh_values, x)

        X, Y, Z = get_meshgrid_function_data(
            fun, self._domain_bounds, num_pts_1d, qoi=0)
        return ax.contourf(
            X, Y, Z, levels=np.linspace(Z.min(), Z.max(), ncontour_levels))

    def _plot_1d(self, mesh_values, nplot_pts_1d=None, ax=None,
                 **kwargs):
        if ax is None:
            ax = plt.subplots(1, 1, figsize=(8, 6))[1]

        if nplot_pts_1d is not None:
            # interpolate values onto plot points
            plot_mesh = np.linspace(
                self._domain_bounds[0], self._domain_bounds[1], nplot_pts_1d)
            interp_vals = self.interpolate(mesh_values, plot_mesh)
            return ax.plot(plot_mesh, interp_vals, **kwargs)
        # just plot values on mesh points
        return ax.plot(self.mesh_pts[0, :], mesh_values, **kwargs)

    def plot(self, mesh_values, nplot_pts_1d=None, ax=None,
             **kwargs):
        if self.nphys_vars == 1:
            return self._plot_1d(
                mesh_values, nplot_pts_1d, ax, **kwargs)
        return self._plot_2d(
            mesh_values, nplot_pts_1d, 30, ax=ax)

    def _apply_dirichlet_boundary_conditions_to_residual(self, residual, sol):
        for ii, bndry_cond in enumerate(self._bndry_conds):
            if bndry_cond[1] == "D":
                if self._basis_types[ii//2] == "F":
                    msg = "Cannot enforce non-periodic boundary conditions "
                    msg += "when using a Fourier basis"
                    raise ValueError(msg)
                idx = self._bndry_indices[ii]
                bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
                residual[idx] = sol[idx]-bndry_vals
        return residual

    def _apply_periodic_boundary_conditions_to_residual(self, residual, sol):
        for ii in range(len(self._bndry_conds)//2):
            if (self._basis_types[ii] == "C" and
                self._bndry_conds[2*ii][1] == "P"):
                idx1 = self._bndry_indices[2*ii]
                idx2 = self._bndry_indices[2*ii+1]
                residual[idx1] = sol[idx1]-sol[idx2]
                residual[idx2] = torch.linalg.multi_dot(
                    (self._deriv_mats[ii//2][idx1, :], sol))-(
                        torch.linalg.multi_dot(
                            (self._deriv_mats[ii//2][idx2, :], sol)))
        return residual

    def _apply_neumann_and_robin_boundary_conditions_to_residual(
            self, residual, sol):
        for ii, bndry_cond in enumerate(self._bndry_conds):
            if bndry_cond[1] == "N" or bndry_cond[1] == "R":
                if self._basis_types[ii//2] == "F":
                    msg = "Cannot enforce non-periodic boundary conditions "
                    msg += "when using a Fourier basis"
                    raise ValueError(msg)
                idx = self._bndry_indices[ii]
                bndry_vals = bndry_cond[0](self.mesh_pts[:, idx])[:, 0]
                normal = (-1)**(ii+1)
                if ii < 2:
                    # warning flux is not dependent on diffusivity (
                    # diffusion equation not a standard boundary formulation)
                    flux = torch.linalg.multi_dot(
                        (self._deriv_mats[0][idx, :], sol))
                else:
                    flux = torch.linalg.multi_dot(
                        (self._deriv_mats[1][idx, :], sol))
                residual[idx] = normal*flux-bndry_vals
                if bndry_cond[1] == "R":
                    residual[idx] += bndry_cond[2]*sol[idx]
        return residual

    def _apply_boundary_conditions_to_residual(self, residual, sol):
        residual = self._apply_dirichlet_boundary_conditions_to_residual(
            residual, sol)
        residual = (
            self._apply_neumann_and_robin_boundary_conditions_to_residual(
                residual, sol))
        residual = (self._apply_periodic_boundary_conditions_to_residual(
            residual, sol))
        return residual

    def integrate(self, mesh_values):
        quad_rules = [
            gauss_jacobi_pts_wts_1D(o+2, 0, 0) for o in self._orders]
        canonical_xquad = cartesian_product([q[0] for q in quad_rules])
        canonical_wquad = outer_product([q[1] for q in quad_rules])
        xquad = self._map_samples_from_canonical_domain(
            canonical_xquad)
        wquad = canonical_wquad/np.prod(
            self._domain_bounds[1::2]-self._domain_bounds[::2])
        return self.interpolate(mesh_values, xquad)[:, 0].dot(wquad)

    def laplace(self, quantity):
        return laplace(self._deriv_mats, quantity)

    def partial_deriv(self, quantity, dd):
        return partial_deriv(self._deriv_mats, quantity, dd)

    def grad(self, quantity):
        return grad(self._deriv_mats, quantity)

    def div(self, quantities):
        return div(self._deriv_mats, quantities)

    def dot(self, quantities1, quantities2):
        return dot(quantities1, quantities2)


class AbstractFunction(ABC):
    def __init__(self, name, requires_grad=False):
        self._name = name
        self._requires_grad = requires_grad

    @abstractmethod
    def _eval(self, samples):
        raise NotImplementedError()

    def __call__(self, samples):
        vals = self._eval(samples)
        if vals.ndim != 2:
            raise ValueError("Function must return a 2D np.ndarray")
        if type(vals) == np.ndarray:
            vals = torch.tensor(
                vals, requires_grad=self._requires_grad, dtype=torch.double)
            return vals
        return vals.clone().detach().requires_grad_(self._requires_grad)


class AbstractTransientFunction(AbstractFunction):
    @abstractmethod
    def set_time(self, time):
        raise NotImplementedError()


class Function(AbstractFunction):
    def __init__(self, fun, name='fun', requires_grad=False):
        super().__init__(name, requires_grad)
        self._fun = fun

    def _eval(self, samples):
        return self._fun(samples)


class TransientFunction(AbstractFunction):
    def __init__(self, fun, name='fun', requires_grad=False):
        super().__init__(name, requires_grad)
        self._fun = fun
        self._partial_fun = None
        self._time = None

    def _eval(self, samples):
        return self._partial_fun(samples)

    def set_time(self, time):
        self._partial_fun = partial(self._fun, time=time)


class AbstractSpectralCollocationResidual(ABC):
    def __init__(self, mesh):
        self.mesh = mesh
        self._funs = None

    @abstractmethod
    def _raw_residual(self, sol):
        raise NotImplementedError()

    def _residual(self, sol):
        # correct equations for boundary conditions
        raw_residual = self._raw_residual(sol)
        return self.mesh._apply_boundary_conditions_to_residual(
            raw_residual, sol)

    def _transient_residual(self, sol, time):
        # correct equations for boundary conditions
        for fun in self._funs:
            if hasattr(fun, "set_time"):
                fun.set_time(time)
        for bndry_cond in self.mesh._bndry_conds:
            if hasattr(bndry_cond[0], "set_time"):
                bndry_cond[0].set_time(time)
        return self._raw_residual(sol)


class SteadyStatePDE():
    def __init__(self, residual):
        self.residual = residual

    def solve(self, init_guess=None, **newton_kwargs):
        if init_guess is None:
            init_guess = torch.ones(
                (self.residual.mesh.nunknowns, 1), dtype=torch.double)
        init_guess = init_guess.squeeze()
        if type(init_guess) == np.ndarray:
            sol = torch.tensor(
                init_guess.clone(), requires_grad=True, dtype=torch.double)
        else:
            sol = init_guess.clone().detach().requires_grad_(True)
        sol = newton_solve(
            self.residual._residual, sol, **newton_kwargs)
        return sol.detach().numpy()[:, None]


class TransientPDE():
    def __init__(self, residual, deltat, tableau_name):
        self.residual = residual
        self.time_integrator = ImplicitRungeKutta(
            deltat, self.residual._transient_residual, tableau_name,
            constraints_fun=self._apply_boundary_conditions_to_residual)

    def _apply_boundary_conditions_to_residual(self, raw_residual, sol, time):
        for bndry_cond in self.residual.mesh._bndry_conds:
            if hasattr(bndry_cond[0], "set_time"):
                bndry_cond[0].set_time(time)
        return self.residual.mesh._apply_boundary_conditions_to_residual(
            raw_residual, sol)

    def solve(self, init_sol, init_time, final_time, verbosity=0,
              newton_opts={}):
        sols = self.time_integrator.integrate(
            init_sol, init_time, final_time, verbosity, newton_opts)
        return sols


class AdvectionDiffusionReaction(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, diff_fun, vel_fun, react_fun, forc_fun):
        super().__init__(mesh)

        self._diff_fun = diff_fun
        self._vel_fun = vel_fun
        self._react_fun = react_fun
        self._forc_fun = forc_fun

        self._funs = [
            self._diff_fun, self._vel_fun, self._react_fun, self._forc_fun]

    @staticmethod
    def _check_shape(vals, ncols, name=None):
        if vals.ndim != 2 or vals.shape[1] != ncols:
            if name is not None:
                msg = name
            else:
                msg = "The ndarray"
            msg += f' has the wrong shape {vals.shape}'
            raise ValueError(msg)

    def _raw_residual(self, sol):
        # torch requires 1d sol to be a 1D tensor so Jacobian can be
        # computed correctly. But each other quantity must be a 2D tensor
        # with 1 column
        # To make sure sol is applied to both velocity components use
        # sol[:, None]
        diff_vals = self._diff_fun(self.mesh.mesh_pts)
        vel_vals = self._vel_fun(self.mesh.mesh_pts)
        forc_vals = self._forc_fun(self.mesh.mesh_pts)
        react_vals = self._react_fun(sol[:, None])
        self._check_shape(diff_vals, 1, "diff_vals")
        self._check_shape(forc_vals, 1, "forc_vals")
        self._check_shape(vel_vals, self.mesh.nphys_vars, "vel_vals")
        self._check_shape(react_vals, 1, "react_vals")
        residual = (self.mesh.div(diff_vals*self.mesh.grad(sol)) -
                    self.mesh.div(vel_vals*sol[:, None]) -
                    react_vals[:, 0]+forc_vals[:, 0])
        return residual


class EulerBernoulliBeam(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, emod_fun, smom_fun, forc_fun):
        if mesh.nphys_vars > 1:
            raise ValueError("Only 1D meshes supported")

        super().__init__(mesh)

        self._emod_fun = emod_fun
        self._smom_fun = smom_fun
        self._forc_fun = forc_fun

        self._emod_vals, self._smom_vals, self._forc_vals = (
            self._precompute_data())

    def _precompute_data(self):
        return (self._emod_fun(self.mesh.mesh_pts),
                self._smom_fun(self.mesh.mesh_pts),
                self._forc_fun(self.mesh.mesh_pts))

    def _raw_residual(self, sol):
        emod_vals = self._emod_fun(self.mesh.mesh_pts)
        smom_vals = self._smom_fun(self.mesh.mesh_pts)
        forc_vals = self._forc_fun(self.mesh.mesh_pts)

        residual = 0
        dmat = self.mesh._deriv_mats[0]
        residual = torch.linalg.multi_dot(
            (dmat, dmat, emod_vals*smom_vals*dmat, dmat, sol))
        residual -= forc_vals[:, 0]
        return residual

    def _residual(self, sol):
        # correct equations for boundary conditions
        raw_residual = self._raw_residual(sol)
        dmat = self.mesh._deriv_mats[0]
        raw_residual[0] = sol[0]-0
        raw_residual[1] = torch.linalg.multi_dot((dmat[0, :], sol))-0
        raw_residual[-1] = torch.linalg.multi_dot((torch.linalg.multi_dot(
            (dmat, dmat))[-1, :], sol))-0
        raw_residual[-2] = torch.linalg.multi_dot((torch.linalg.multi_dot(
            (dmat, dmat, dmat))[-1, :], sol))-0
        return raw_residual


class Helmholtz(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, wnum_fun, forc_fun):
        super().__init__(mesh)

        self._wnum_fun = wnum_fun
        self._forc_fun = forc_fun

    def _raw_residual(self, sol):
        wnum_vals = self._wnum_fun(self.mesh.mesh_pts)
        forc_vals = self._forc_fun(self.mesh.mesh_pts)
        residual = (self.mesh.laplace(sol) + wnum_vals[:, 0]*sol -
                    forc_vals[:, 0])
        return residual


class VectorMesh():
    def __init__(self, meshes):
        self._meshes = meshes
        self.nunknowns = sum([m.mesh_pts.shape[1] for m in self._meshes])
        self.nphys_vars = self._meshes[0].nphys_vars
        self._bndry_conds = []
        for mesh in self._meshes:
            self._bndry_conds += mesh._bndry_conds

    def split_quantities(self, vector):
        cnt = 0
        split_vector = []
        for ii in range(len(self._meshes)):
            ndof = self._meshes[ii].mesh_pts.shape[1]
            split_vector.append(vector[cnt:cnt+ndof])
            cnt += ndof
        return split_vector

    def _apply_boundary_conditions_to_residual(self, residual, sol):
        split_sols = self.split_quantities(sol)
        split_residual = self.split_quantities(residual)
        for ii, mesh in enumerate(self._meshes):
            split_residual[ii] = mesh._apply_boundary_conditions_to_residual(
                split_residual[ii], split_sols[ii])
        return torch.cat(split_residual)

    def interpolate(self, sol_vals, xx):
        Z = []
        for ii in range(len(self._meshes)):
            Z.append(self._meshes[ii].interpolate(sol_vals[ii], xx))
        return Z

    def integrate(self, sol_vals):
        Z = []
        for ii in range(len(self._meshes)):
            Z.append(self._meshes[ii].integrate(sol_vals[ii]))
        return Z

    def plot(self, sol_vals, nplot_pts_1d=50, axs=None, **kwargs):
        if axs is None:
            fig, axs = plt.subplots(
                1, self.nphys_vars+1, figsize=(8*(self.nphys_vars+1), 6))
        if self._meshes[0].nphys_vars == 1:
            xx = np.linspace(
                *self._meshes[0]._domain_bounds, nplot_pts_1d)[None, :]
            Z =  self.interpolate(sol_vals, xx)
            objs = []
            for ii in range(2):
                obj = axs[ii].plot(xx[0, :], Z[ii], **kwargs)
                objs.append(obj)
            return objs
        from pyapprox.util.visualization import get_meshgrid_samples
        X, Y, pts = get_meshgrid_samples(
            self._meshes[0]._domain_bounds, nplot_pts_1d)
        Z = self.interpolate(sol_vals, pts)
        objs = []
        for ii in range(3):
            obj = axs[ii].contourf(
                X, Y, Z[ii].reshape(X.shape),
                levels=np.linspace(Z[ii].min(), Z[ii].max(), 20))
            objs.append(obj)
        return objs


class InteriorCartesianProductCollocationMesh(CartesianProductCollocationMesh):
    def __init__(self, domain_bounds, orders):
        super().__init__(domain_bounds, orders,
                         [[None ,None]]*len(domain_bounds))

        self._deriv_mats_alt = self._form_derivative_matrices_alt()

    def _apply_boundary_conditions_to_residual(self, residual, sol):
        return residual

    def _form_canonical_deriv_matrices(self, canonical_mesh_pts_1d):
        eval_samples = cartesian_product(
            [-np.cos(np.linspace(0, np.pi, o+1)) for o in self._orders])
        if self.nphys_vars == 2:
            canonical_deriv_mats, __, canonical_mesh_pts = (
                lagrange_polynomial_derivative_matrix_2d(
                    eval_samples, canonical_mesh_pts_1d))
            return canonical_deriv_mats, canonical_mesh_pts

        return [lagrange_polynomial_derivative_matrix_1d(
            eval_samples[0], canonical_mesh_pts_1d[0])[0]], np.atleast_1d(
                canonical_mesh_pts_1d)

    def _form_derivative_matrices(self):
        # will work but divergence condition is only satisfied on interior
        # so if want to drive flow with only boundary conditions on velocity
        # it will now work
        canonical_mesh_pts_1d = [
            -np.cos(np.linspace(0, np.pi, o+1))[1:-1] for o in self._orders]
        # self.mesh_pts_1d = [
        #     -np.cos(np.linspace(0, np.pi, self.order[0]+1))[1:-1],
        #     -np.cos(np.linspace(0, np.pi, self.order[0]-1))]
        canonical_mesh_pts_1d_baryc_weights = [
            compute_barycentric_weights_1d(xx) for xx in canonical_mesh_pts_1d]

        canonical_deriv_mats, canonical_mesh_pts = (
            self._form_canonical_deriv_matrices(canonical_mesh_pts_1d))

        mesh_pts = self._map_samples_from_canonical_domain(
            canonical_mesh_pts.copy())

        deriv_mats = []
        for dd in range(self.nphys_vars):
            deriv_mats.append(canonical_deriv_mats[dd]*2./(
                self._domain_bounds[2*dd+1]-self._domain_bounds[2*dd]))
        deriv_mats = [torch.tensor(mat, dtype=torch.double)
                      for mat in deriv_mats]
        return (canonical_mesh_pts_1d, None,
                canonical_mesh_pts_1d_baryc_weights,
                mesh_pts, deriv_mats)

    def _form_derivative_matrices_alt(self):
        canonical_mesh_pts_1d = [
            -np.cos(np.linspace(0, np.pi, o+1))[1:-1] for o in self._orders]
        if self.nphys_vars == 2:
            canonical_deriv_mats_alt = (
                lagrange_polynomial_derivative_matrix_2d(
                    cartesian_product(canonical_mesh_pts_1d),
                    [-np.cos(np.linspace(0, np.pi, o+1))
                     for o in self._orders])[0])
        else:
            canonical_deriv_mats_alt = [
                lagrange_polynomial_derivative_matrix_1d(
                    canonical_mesh_pts_1d[0],
                    -np.cos(np.linspace(0, np.pi, self._orders[0]+1)))[0]]

        deriv_mats_alt = []
        for dd in range(self.nphys_vars):
            deriv_mats_alt.append(canonical_deriv_mats_alt[dd]*2./(
                self._domain_bounds[2*dd+1]-self._domain_bounds[2*dd]))
        return [torch.tensor(mat, dtype=torch.double)
                for mat in deriv_mats_alt]

    def _get_deriv_mats(self, quantity):
        if quantity.shape[0] == self.nunknowns:
            return self._deriv_mats
        return self._deriv_mats_alt

    def _determine_boundary_indices(self):
        self._boundary_indices = None

    def laplace(self, quantity):
        return laplace(self._get_deriv_mats(quantity), quantity)

    def partial_deriv(self, quantity, dd):
        return partial_deriv(self._get_deriv_mats(quantity), quantity, dd)

    def grad(self, quantity):
        return grad(self._get_deriv_mats(quantity), quantity)

    def div(self, quantities):
        return div(self._get_deriv_mats(quantities), quantities)


class NavierStokes(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, vel_forc_fun, pres_forc_fun,
                 unique_pres_data=(0, 1)):
        super().__init__(mesh)

        self._navier_stokes = True
        self._vel_forc_fun = vel_forc_fun
        self._pres_forc_fun = pres_forc_fun
        self._unique_pres_data = unique_pres_data

    def _raw_residual(self, sol):
        split_sols = self.mesh.split_quantities(sol)
        vel_sols = torch.hstack([s[:, None] for s in split_sols[:-1]])
        vel_forc_vals = self._vel_forc_fun(self.mesh._meshes[0].mesh_pts)
        residual = [None for ii in range(len(split_sols))]
        for dd in range(self.mesh.nphys_vars):
            residual[dd] = (
                -self.mesh._meshes[dd].laplace(split_sols[dd]) +
                self.mesh._meshes[-1].partial_deriv(split_sols[-1], dd))
            residual[dd] -= vel_forc_vals[:, dd]
            if self._navier_stokes:
                residual[dd] += self.mesh._meshes[0].dot(
                    vel_sols, self.mesh._meshes[dd].grad(split_sols[dd]))
        nvel_unknowns = self.mesh._meshes[0].nunknowns
        residual[-1] = (
            self.mesh._meshes[-1].div(vel_sols) -
            self._pres_forc_fun(self.mesh._meshes[-1].mesh_pts)[:, 0])
        residual[-1][self._unique_pres_data[0]] = (
            split_sols[-1][self._unique_pres_data[0]]-self._unique_pres_data[1])
        return torch.cat(residual)


class LinearStokes(NavierStokes):
    def __init__(self, mesh, vel_forc_fun, pres_forc_fun,
                 unique_pres_data=(0, 1)):
        super().__init__(mesh, vel_forc_fun, pres_forc_fun,
                         unique_pres_data)
        self._navier_stokes = False


class ShallowWater(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, depth_forc_fun, vel_forc_fun, bed_fun):
        super().__init__(mesh)

        self._depth_forc_fun = depth_forc_fun
        self._vel_forc_fun = vel_forc_fun
        self._g = 9.81

        self._funs = [self._depth_forc_fun, self._vel_forc_fun]
        self._bed_vals = bed_fun(self.mesh._meshes[0].mesh_pts)

    def _raw_residual_1d(self, depth, vels, depth_forc_vals, vel_forc_vals):
        pderiv = self.mesh._meshes[0].partial_deriv
        residual = [0, 0]
        residual[0] = pderiv(depth*vels[:, 0], 0)-depth_forc_vals[:, 0]
        residual[1] = pderiv(depth*vels[:, 0]**2+self._g*depth**2/2, 0)
        residual[1] += self._g*depth*pderiv(self._bed_vals[:, 0], 0)
        residual[1] -= vel_forc_vals[:, 0]
        return torch.cat(residual)

    def _raw_residual_2d(self, depth, vels, depth_forc_vals, vel_forc_vals):
        pderiv = self.mesh._meshes[0].partial_deriv
        residual = [0, 0, 0]
         # depth equation (mass balance)
        for dd in range(self.mesh.nphys_vars):
            residual[0] += self.mesh._meshes[0].partial_deriv(
                depth*vels[:, dd], dd)
        residual[0] -= depth_forc_vals[:, 0]
        # velocity equations (momentum equations)
        residual[1] = pderiv(depth*vels[:, 0]**2+self._g*depth**2/2, 0)
        residual[1] += pderiv(depth*torch.prod(vels, dim=1), 1)
        residual[1] += self._g*depth*pderiv(self._bed_vals[:, 0], 0)
        residual[1] -= vel_forc_vals[:, 0]
        residual[2] = pderiv(depth*torch.prod(vels, dim=1), 0)
        residual[2] += pderiv(depth*vels[:, 1]**2+self._g*depth**2/2, 1)
        residual[2] += self._g*depth*pderiv(self._bed_vals[:, 0], 1)
        residual[2] -= vel_forc_vals[:, 1]
        return torch.cat(residual)

    def _raw_residual(self, sol):
        split_sols = self.mesh.split_quantities(sol)
        depth = split_sols[0]
        vels = torch.hstack([s[:, None] for s in split_sols[1:]])
        depth_forc_vals = self._depth_forc_fun(self.mesh._meshes[0].mesh_pts)
        vel_forc_vals = self._vel_forc_fun(self.mesh._meshes[1].mesh_pts)

        if self.mesh.nphys_vars == 1:
            return self._raw_residual_1d(
                depth, vels, depth_forc_vals, vel_forc_vals)

        return self._raw_residual_2d(
                depth, vels, depth_forc_vals, vel_forc_vals)

        residual = [0 for ii in range(len(split_sols))]
        # depth equation (mass balance)
        for dd in range(self.mesh.nphys_vars):
            # split_sols = [q1, q2] = [h, u, v]
            residual[0] += self.mesh._meshes[0].partial_deriv(
                depth*vels[:, dd], dd)
        residual[0] -= depth_forc_vals[:, 0]
        # velocity equations (momentum equations)
        for dd in range(self.mesh.nphys_vars):
            # split_sols = [q1, q2] = [h, u, v]
            residual[dd+1] += self.mesh._meshes[dd].partial_deriv(
                depth*vels[:, dd]**2+self._g*depth**2/2, dd)
            # split_sols = [q1, q2] = [h, uh, vh]
            # residual[dd+1] += self.mesh._meshes[dd].partial_deriv(
            #     vels[:, dd]**2/depth+self._g*depth**2/2, dd)
            residual[dd+1] += self._g*depth*self.mesh._meshes[dd].partial_deriv(
                self._bed_vals[:, 0], dd)
            residual[dd+1] -= vel_forc_vals[:, dd]
        if self.mesh.nphys_vars > 1:
            residual[1] += self.mesh._meshes[1].partial_deriv(
                depth*torch.prod(vels, dim=1), 1)
            residual[2] += self.mesh._meshes[2].partial_deriv(
                depth*torch.prod(vels, dim=1), 0)
        return torch.cat(residual)


class ShallowShelfVelocities(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, forc_fun, bed_fun, beta_fun,
                 depth_fun, A, rho, homotopy_val=0):
        super().__init__(mesh)

        self._forc_fun = forc_fun
        self._A = A
        self._rho = rho
        self._homotopy_val = homotopy_val
        self._g = 9.81
        self._n = 3

        self._depth_fun = depth_fun
        self._funs = [self._forc_fun]
        self._bed_vals = bed_fun(self.mesh._meshes[0].mesh_pts)
        self._beta_vals = beta_fun(self.mesh._meshes[0].mesh_pts)
        self._forc_vals = self._forc_fun(self.mesh._meshes[0].mesh_pts)

        self._depth_vals = None

    def _strains(self, split_sols):
        pderiv = self.mesh._meshes[0].partial_deriv
        e_ij = []
        for ii in range(len(split_sols)):
            e_ij.append([])
            for jj in range(self.mesh.nphys_vars):
                e_ij[-1].append(pderiv(split_sols[ii], jj))
        return e_ij

    def _effective_strain_rate_1d(self, e_ij):
        return (e_ij[0][0]**2+self._homotopy_val)**(1/2)

    def _effective_strain_rate_2d(self, e_ij):
        return (e_ij[0][0]**2 + e_ij[1][1]**2+e_ij[0][0]*e_ij[1][1]+
                e_ij[0][1]**2/4+self._homotopy_val)**(1/2)

    def _effective_strain_rate(self, e_ij):
        if self.mesh.nphys_vars == 2:
            return self._effective_strain_rate_2d(e_ij)
        elif self.mesh.nphys_vars == 1:
            return self._effective_strain_rate_1d(e_ij)
        raise NotImplementedError()

    def _viscosity(self, e_ij):
        return (1/2*self._A**(-1/self._n)*
                self._effective_strain_rate(e_ij)**((self._n-1)/(self._n)))

    def _vector_components(self, e_ij):
        if self.mesh.nphys_vars == 2:
            vec1 = torch.hstack([(2*e_ij[0][0] + e_ij[1][1])[:, None],
                                 ((e_ij[0][1] + e_ij[1][0])/2)[:, None]])
            vec2 = torch.hstack([((e_ij[0][1] + e_ij[1][0])/2)[:, None],
                                 (e_ij[0][0] + 2*e_ij[1][1])[:, None]])
            return vec1, vec2
        return (2*e_ij[0][0][:, None],)

    def _raw_residual_nD(self, split_sols, depth_vals):
        pderiv = self.mesh._meshes[0].partial_deriv
        div = self.mesh._meshes[0].div
        e_ij = self._strains(split_sols)
        visc = self._viscosity(e_ij)
        C = 2*visc*depth_vals[:, 0]
        vecs = self._vector_components(e_ij)
        residual = [0 for ii in range(self.mesh.nphys_vars)]
        for ii in range(self.mesh.nphys_vars):
            residual[ii] = -div(C[:, None]*vecs[ii])
            residual[ii] += self._beta_vals[:, 0]*split_sols[ii]
            residual[ii] += self._rho*self._g*depth_vals[:, 0]*pderiv(
                self._bed_vals[:, 0]+depth_vals[:, 0], ii)
            residual[ii] -= self._forc_vals[:, ii]
        return torch.cat(residual)
    

    # def _raw_residual_1d(self, split_sols, depth_vals):
    #     pderiv = self.mesh._meshes[0].partial_deriv
    #     dudx = pderiv(split_sols[0], 0)
    #     if self._n == 1:
    #         visc = 1/2*self._A**(-1)
    #     else:
    #         De = (dudx**2+self._homotopy_val)**(1/2)
    #         visc = 1/2*self._A**(-1/self._n)*De**((self._n-1)/(self._n))
    #     C = 2*depth_vals[:, 0]*visc
    #     residual = -pderiv(C*2*dudx, 0)
    #     residual += self._beta_vals[:, 0]*split_sols[0]
    #     residual += self._rho*self._g*depth_vals[:, 0]*pderiv(
    #         self._bed_vals[:, 0]+depth_vals[:, 0], 0)
    #     residual -= self._forc_vals[:, 0]
    #     return residual

    # def _raw_residual_2d(self, split_sols, depth_vals):
    #     pderiv = self.mesh._meshes[0].partial_deriv
    #     dudx = pderiv(split_sols[0], 0)
    #     dvdy = pderiv(split_sols[1], 1)
    #     dudy = pderiv(split_sols[0], 1)
    #     dvdx = pderiv(split_sols[1], 0)
    #     De = (dudx**2+dvdy**2+dudx*dvdy+(dudy+dvdx)**2/4 +
    #           self._homotopy_val)**(1/2)
    #     visc = 1/2*self._A**(-1/self._n)*De**((self._n-1)/(self._n))
    #     C = 2*depth_vals[:, 0]*visc
    #     residual = [0, 0]
    #     residual[0] = -pderiv(C*(2*dudx+dvdy), 0)-pderiv(C*(dudy+dvdx)/2, 1)
    #     residual[1] = -pderiv(C*(dudy+dvdx)/2, 0)-pderiv(C*(dudx+2*dvdy), 1)
    #     for ii in range(2):
    #         residual[ii] += self._beta_vals[:, 0]*split_sols[ii]
    #         residual[ii] += self._rho*self._g*depth_vals[:, 0]*pderiv(
    #             self._bed_vals[:, 0]+depth_vals[:, 0], ii)
    #         residual[ii] -= self._forc_vals[:, ii]
    #     return torch.cat(residual)

    # def _raw_residual_nD(self, split_sols, depth_vals):
    #     # if self.mesh.nphys_vars == 1:
    #     #     return self._raw_residual_1d(split_sols, depth_vals)
    #     # return self._raw_residual_2d(split_sols, depth_vals)

    def _raw_residual(self, sol):
        depth_vals = self._depth_fun(self.mesh._meshes[0].mesh_pts)
        split_sols = self.mesh.split_quantities(sol)
        return self._raw_residual_nD(split_sols, depth_vals)


class ShallowShelf(ShallowShelfVelocities):
    def __init__(self, mesh, forc_fun, bed_fun, beta_fun,
                 depth_forc_fun, A, rho, homotopy_val=0):
        if len(mesh._meshes) != mesh._meshes[0].nphys_vars+1:
            raise ValueError("Incorrect number of meshes provided")

        super().__init__(mesh, forc_fun, bed_fun, beta_fun,
                         None, A, rho, homotopy_val)
        self._depth_forc_fun = depth_forc_fun

    def _raw_residual(self, sol):
         # depth is 3rd mesh
        split_sols = self.mesh.split_quantities(sol)
        depth_vals = split_sols[-1]
        residual = super()._raw_residual_nD(
            split_sols[:-1], depth_vals[:, None])
        vel_vals = torch.hstack(
            [s[:, None] for s in split_sols[:self.mesh.nphys_vars]])
        depth_residual = -self.mesh._meshes[self.mesh.nphys_vars].div(
            depth_vals[:, None]*vel_vals)
        depth_residual += self._depth_forc_fun(
            self.mesh._meshes[self.mesh.nphys_vars].mesh_pts)[:, 0]
        return torch.cat((residual, depth_residual))


class NaviersLinearElasticity(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, forc_fun, lambda_fun, mu_fun, rho):
        super().__init__(mesh)

        self._rho = rho
        self._forc_fun = forc_fun
        self._lambda_fun = lambda_fun
        self._mu_fun = mu_fun

        # only needs to be time dependent funs
        self._funs = [self._forc_fun]

        # assumed to be time independent
        self._lambda_vals = self._lambda_fun(self.mesh._meshes[0].mesh_pts)
        self._mu_vals = self._mu_fun(self.mesh._meshes[0].mesh_pts)

        # sol is the displacement field
        # beam length L box cross section with width W
        # lambda = Lamae elasticity parameter
        # mu = Lamae elasticity parameter
        # rho density of beam
        # g acceleartion due to gravity

    def _raw_residual_1d(self, sol_vals, forc_vals):
        pderiv = self.mesh.meshes[0].partial_deriv
        residual = -pderiv(
            (self._lambda_vals[:, 0]+2*self._mu_vals[:, 0]) *
            pderiv(sol_vals[:, 0], 0), 0) - self._rho*forc_vals[:, 0]
        return residual

    def _raw_residual_2d(self, sol_vals, forc_vals):
        pderiv = self.mesh.meshes[0].partial_deriv
        residual = [0, 0]
        mu = self._mu_vals[:, 0]
        lam = self._lambda_vals[:, 0]
        lp2mu = lam+2*mu
        # strains
        exx = pderiv(sol_vals[:, 0], 0)
        eyy = pderiv(sol_vals[:, 1], 1)
        exy = 0.5*(pderiv(sol_vals[:, 0], 1)+pderiv(sol_vals[:, 1], 0))
        # stresses
        tauxy = 2*mu*exy
        tauxx = lp2mu*exx+lam*eyy
        tauyy = lam*exx+lp2mu*eyy
        residual[0] = pderiv(tauxx, 0)+pderiv(tauxy, 1)
        residual[0] += self._rho*forc_vals[:, 0]
        residual[1] = pderiv(tauxy, 0)+pderiv(tauyy, 1)
        residual[1] += self._rho*forc_vals[:, 1]
        return torch.cat(residual)

    def _raw_residual(self, sol):
        split_sols = self.mesh.split_quantities(sol)
        sol_vals = torch.hstack(
            [s[:, None] for s in split_sols[:self.mesh.nphys_vars]])
        forc_vals = self._forc_fun(self.mesh._meshes[0].mesh_pts)
        if self.mesh.nphys_vars == 1:
            return self._raw_residual_1d(sol_vals, forc_vals)
        return self._raw_residual_2d(sol_vals, forc_vals)


class FirstOrderStokesIce(AbstractSpectralCollocationResidual):
    def __init__(self, mesh, forc_fun, bed_fun, beta_fun,
                 depth_fun, A, rho, homotopy_val=0):
        super().__init__(mesh)

        self._forc_fun = forc_fun
        self._A = A
        self._rho = rho
        self._homotopy_val = homotopy_val
        self._g = 9.81
        self._n = 3

    def _strains(split_sols):
        pderiv = self.mesh._meshes[0].partial_deriv
        e_ij = []
        for ii in range(len(split_sols)):
            e_ij.append([])
            for jj in range(self.mesh.nphys_vars):
                e_ij[-1].append(pderiv(split_sols[ii], jj))
        return e_ij

    def _effective_strain_rate_2d(self, e_ij):
        rate = e_ij[0, 0]**2 + e_ij[0, 1]**2/4

    def _effective_strain_rate(self, e_ij):
        if self.mesh.nphys_vars == 2:
            return self._effective_strain_rate_2d(self, e_ij)
        raise NotImplementedError()

    def _viscosity(self, e_ij):
        return 1/2*self._A**(-1/self._n)*self._effective_strain_rate(e_ij)

    def _vector_components_2d(self, e_ij):
        vec1 = torch.hstack([(2*e_ij[0, 0] + e_ij[1, 1])[:, None],
                             (e_ij[0, 1] + e_ij[1, 0]/2)[:, None]])
        vec2 = torch.hstack([(e_ij[0, 1] + e_ij[1, 0]/2)[:, None],
                             (e_ij[0, 0] + 2*e_ij[1, 1])[:, None]])
        return vec1, vec2

    
    def _raw_residual(self, split_sols, depth_vals):
        e_ij = self._strains(split_sols)
        visc = self._viscosity(e_ij)
        C = 2*visc*depth_vals[:, 0]
        vecs = self._vector_components_2d(e_ij)
        residual = [0 for ii in range(self.mesh.nphys_vars)]
        for ii in range(self.mesh.nphys_vars):
            residual[ii] = self.mesh.div(C[:, None]*vecs[ii]) 
            residual[ii] += self._beta_vals[:, 0]*split_sols[ii]
            residual[ii] += self._rho*self._g*depth_vals[:, 0]*pderiv(
                self._bed_vals[:, 0]+depth_vals[:, 0], ii)
            residual[ii] -= self._forc_vals[:, ii]
        return torch.cat(residual)
