import sys
import math
from pyapprox.karhunen_loeve_expansion import MeshKLE

if sys.platform == 'win32':
    raise ImportError("Not available on Windows")

import dolfin as dl
try:
    import fenics_adjoint as dla
except:
    import dolfin as dla

import numpy as np


def constrained_newton_energy_solve(F, uh, dirichlet_bcs=None, bc0=None,
                                    linear_solver=None, opts=dict(),
                                    C=None, constraint_vec=None):
    """
    See https://uvilla.github.io/inverse15/UnconstrainedMinimization.html

    F: dl.Expression
        The energy functional.

    uh : dl.Function
        Final solution. The initial state on entry to the function
        will be used as initial guess and then overwritten

    dirichlet_bcs : list
        The Dirichlet boundary conditions on the unknown u.

    bc0 : list
        The Dirichlet boundary conditions for the step (du) in the Newton
        iterations.

    """
    max_iter = opts.get("max_iter", 20)
    # exit when sqrt(g,g)/sqrt(g_0,g_0) <= rel_tolerance"
    rtol = opts.get("rel_tolerance", 1e-8)
    # exit when sqrt(g,g) <= abs_tolerance
    atol = opts.get("abs_tolerance", 1e-9)
    # exit when (g,du) <= gdu_tolerance
    gdu_tol = opts.get("gdu_tolerance", 1e-14)
    # define armijo sufficient decrease
    c_armijo = opts.get("c_armijo", 1e-4)
    # exit if max backtracking steps reached
    max_backtrack = opts.get("max_backtracking_iter", 20)
    # define verbosity
    prt_level = opts.get("print_level", 0)

    termination_reasons = ["Maximum number of Iteration reached",  # 0
                           "Norm of the gradient less than tolerance",  # 1
                           "Maximum number of backtracking reached",  # 2
                           "Norm of (g, du) less than tolerance"  # 3
                           ]
    it = 0
    total_cg_iter = 0
    converged = False
    reason = 0

    L = F
    if C is not None:
        L += C
        if prt_level > 0:
            print("Solving Constrained Nonlinear Problem")
    else:
        if prt_level > 0:
            print("Solving Unconstrained Nonlinear Problem")

    # Compute gradient and hessian
    grad = dla.derivative(L, uh)
    H = dla.derivative(grad, uh)

    # Applying boundary conditions
    if dirichlet_bcs is not None:
        if type(dirichlet_bcs) is dla.DirichletBC:
            bcsl = [dirichlet_bcs]
        else:
            bcsl = dirichlet_bcs
        [bc.apply(uh.vector()) for bc in bcsl]

    if constraint_vec is not None:
        assert C is not None
        dcd_state = dla.assemble(dla.derivative(C, u))
        dcd_lagrangeMult = dcd_state*constraint_vec
        if not dcd_lagrangeMult.norm("l2") < 1.e-14:
            msg = "The initial guess does not satisfy the constraint."
            raise ValueError(msg)

    # Setting variables
    Fn = dla.assemble(F)
    gn = dla.assemble(grad)
    g0_norm = gn.norm("l2")
    gn_norm = g0_norm
    tol = max(g0_norm*rtol, atol)
    du = dla.Function(uh.function_space()).vector()

    # if linear_solver =='PETScLU':
    #    linear_solver = dl.PETScLUSolver(uh.function_space().mesh().mpi_comm())
    # else:
    #    assert linear_solver is None

    if prt_level > 0:
        print("{0:>3}  {1:>6} {2:>15} {3:>15} {4:>15} {5:>15}".format(
            "Nit", "CGit", "Energy", "||g||", "(g,du)", "alpha"))
        print("{0:3d} {1:6d}    {2:15e} {3:15e}     {4:15}   {5:15}".format(
            0, 0, Fn, g0_norm, "    NA    ", "    NA"))

    converged = False
    reason = 0

    for it in range(max_iter):
        if bc0 is not None:
            [Hn, gn] = dla.assemble_system(H, grad, bc0)
        else:
            Hn = dla.assemble(H)
            gn = dla.assemble(grad)

        Hn.init_vector(du, 1)
        if linear_solver is None:
            lin_it = dla.solve(Hn, du, -gn, "cg", "petsc_amg")
        else:
            print('a')
            lin_it = dla.solve(Hn, du, -gn, "lu")
            # linear_solver.set_operator(Hn)
            # lin_it = linear_solver.solve(du, -gn)
        total_cg_iter += lin_it

        du_gn = du.inner(gn)

        alpha = 1.0
        if (np.abs(du_gn) < gdu_tol):
            converged = True
            reason = 3
            uh.vector().axpy(alpha, du)
            Fn = dla.assemble(F)
            gn_norm = gn.norm("l2")
            break

        uh_backtrack = uh.copy(deepcopy=True)
        bk_converged = False

        # Backtrack
        for j in range(max_backtrack):
            uh.assign(uh_backtrack)
            uh.vector().axpy(alpha, du)
            Fnext = dla.assemble(F)
            # print(Fnext,Fn + alpha*c_armijo*du_gn)
            if Fnext < Fn + alpha*c_armijo*du_gn:
                Fn = Fnext
                bk_converged = True
                break
            alpha /= 2.

        if not bk_converged:
            reason = 2
            break

        gn_norm = gn.norm("l2")

        if prt_level > 0:
            print("{0:3d} {1:6d}    {2:15e} {3:15e} {4:15e} {5:15e}".format(
                it+1, lin_it, Fn, gn_norm, du_gn, alpha))

        if gn_norm < tol:
            converged = True
            reason = 1
            break

    if prt_level > 0:
        if reason == 3:
            print("{0:3d} {1:6d}    {2:15e} {3:15e} {4:15e} {5:15e}".format(
                it+1, lin_it, Fn, gn_norm, du_gn, alpha))
        print(termination_reasons[reason])
        if converged:
            print("Newton converged in ", it,
                  "nonlinear iterations and ", total_cg_iter,
                  "linear iterations.")
        else:
            print("Newton did NOT converge in ", it, "iterations.")
        print("Final norm of the gradient: ", gn_norm)
        print("Value of the cost functional: ", Fn)

    if reason in [0, 2]:
        raise Exception(termination_reasons[reason])

    return uh


def unconstrained_newton_solve(F, J, uh, dirichlet_bcs=None, bc0=None,
                               linear_solver=None, opts=dict()):
    """
    F: dl.Expression
        The variational form.

    uh : dl.Function
        Final solution. The initial state on entry to the function
        will be used as initial guess and then overwritten

    dirichlet_bcs : list
        The Dirichlet boundary conditions on the unknown u.

    bc0 : list
        The Dirichlet boundary conditions for the step (du) in the Newton
        iterations.

    """
    max_iter = opts.get("max_iter", 50)
    # exit when sqrt(g,g)/sqrt(g_0,g_0) <= rel_tolerance"
    rtol = opts.get("rel_tolerance", 1e-8)
    # exit when sqrt(g,g) <= abs_tolerance
    atol = opts.get("abs_tolerance", 1e-9)
    # exit when (g,du) <= gdu_tolerance
    gdu_tol = opts.get("gdu_tolerance", 1e-14)
    # define armijo sufficient decrease
    c_armijo = opts.get("c_armijo", 1e-4)
    # exit if max backtracking steps reached
    max_backtrack = opts.get("max_backtracking_iter", 20)
    # define verbosity
    prt_level = opts.get("print_level", 0)

    termination_reasons = ["Maximum number of Iteration reached",  # 0
                           "Norm of the gradient less than tolerance",  # 1
                           "Maximum number of backtracking reached",  # 2
                           "Norm of (g, du) less than tolerance",  # 3
                           "Norm of residual less than tolerance"]  # 4
    it = 0
    total_cg_iter = 0
    converged = False
    reason = 0

    if prt_level > 0:
        print("Solving Nonlinear Problem")

    # Applying boundary conditions
    if dirichlet_bcs is not None:
        if type(dirichlet_bcs) is dla.DirichletBC:
            bcsl = [dirichlet_bcs]
        else:
            bcsl = dirichlet_bcs
        [bc.apply(uh.vector()) for bc in bcsl]

    if type(bc0) is dla.DirichletBC:
        bc0 = [bc0]

    # Setting variables
    gn = dla.assemble(F)
    res_func = dla.Function(uh.function_space())
    res_func.assign(dla.Function(uh.function_space(), gn))
    res = res_func.vector()
    if bc0 is not None:
        for bc in bc0:
            bc.apply(res)
    Fn = res.norm("l2")
    g0_norm = gn.norm("l2")
    gn_norm = g0_norm
    tol = max(g0_norm*rtol, atol)
    res_tol = max(Fn*rtol, atol)
    du = dla.Function(uh.function_space()).vector()

    if linear_solver == 'PETScLU':
        linear_solver = dla.PETScLUSolver(
            uh.function_space().mesh().mpi_comm())
    else:
        assert linear_solver is None

    if prt_level > 0:
        print("{0:>3}  {1:>6} {2:>15} {3:>15} {4:>15} {5:>15} {6:>6}".format(
            "Nit", "CGit", "||r||", "||g||", "(g,du)", "alpha", "Nbt"))
        print("{0:3d} {1:6d}    {2:15e} {3:15e}     {4:15}   {5:10} {6:s}".format(
            0, 0, Fn, g0_norm, "    NA    ", "    NA", "NA"))

    converged = False
    reason = 0
    nbt = 0

    for it in range(max_iter):
        if bc0 is not None:
            [Hn, gn] = dla.assemble_system(J, F, bc0)
        else:
            Hn = dla.assemble(J)
            gn = dla.assemble(F)

        Hn.init_vector(du, 1)
        if linear_solver is None:
            lin_it = dla.solve(Hn, du, -gn, "cg", "petsc_amg")
        else:
            linear_solver.set_operator(Hn)
            lin_it = linear_solver.solve(du, -gn)
        total_cg_iter += lin_it

        du_gn = du.inner(gn)

        alpha = 1.0
        if (np.abs(du_gn) < gdu_tol):
            converged = True
            reason = 3
            uh.vector().axpy(alpha, du)
            gn_norm = gn.norm("l2")
            Fn = gn_norm
            break

        uh_backtrack = uh.copy(deepcopy=True)
        bk_converged = False

        # Backtrack
        for nbt in range(max_backtrack):
            uh.assign(uh_backtrack)
            uh.vector().axpy(alpha, du)
            res = dla.assemble(F)
            if bc0 is not None:
                for bc in bc0:
                    bc.apply(res)
            Fnext = res.norm("l2")
            # print(Fn,Fnext,Fn + alpha*c_armijo*du_gn)
            if Fnext < Fn + alpha*c_armijo*du_gn:
                # if True:
                Fn = Fnext
                bk_converged = True
                break
            alpha /= 2.

        if not bk_converged:
            reason = 2
            break

        gn_norm = gn.norm("l2")

        if prt_level > 0:
            print("{0:3d} {1:6d}    {2:15e} {3:15e} {4:15e} {5:15e} {6:3d}".format(
                it+1, lin_it, Fn, gn_norm, du_gn, alpha, nbt+1))

        if gn_norm < tol:
            converged = True
            reason = 1
            break

        if Fn < res_tol:
            converged = True
            reason = 4
            break

    if prt_level > 0:
        if reason == 3:
            print("{0:3d} {1:6d}    {2:15e} {3:15e} {4:15e} {5:15e} {6:3d}".format(
                it+1, lin_it, Fn, gn_norm, du_gn, alpha, nbt+1))
        print(termination_reasons[reason])
        if converged:
            print("Newton converged in ", it,
                  "nonlinear iterations and ", total_cg_iter,
                  "linear iterations.")
        else:
            print("Newton did NOT converge in ", it, "iterations.")
        print("Final norm of the gradient: ", gn_norm)
        print("Value of the cost functional: ", Fn)

    if reason in [0, 2]:
        raise Exception(termination_reasons[reason])

    return uh


def get_2d_rectangular_mesh_boundaries(xl, xr, yb, yt):
    left_bndry = dl.CompiledSubDomain(
        "near(x[0],%e)&&on_boundary" % xl)
    right_bndry = dl.CompiledSubDomain(
        "near(x[0],%e)&&on_boundary" % xr)
    bottom_bndry = dl.CompiledSubDomain(
        "near(x[1],%e)&&on_boundary" % yb)
    top_bndry = dl.CompiledSubDomain(
        "near(x[1],%e)&&on_boundary" % yt)
    return left_bndry, right_bndry, bottom_bndry, top_bndry


def get_2d_unit_square_mesh_boundaries():
    return get_2d_rectangular_mesh_boundaries(0, 1, 0, 1)


def get_2d_rectangular_mesh_boundary_segment(phys_var1, bndry_coord, seg_left,
                                             seg_right):
    phys_var2 = 1-phys_var1
    string = "(std::abs(x[%d]-%f)<DOLFIN_EPS)&&(x[%d]-%f)>-DOLFIN_EPS&&(x[%d]-%f)<DOLFIN_EPS&&on_boundary" % (
        phys_var1, bndry_coord, phys_var2, seg_left, phys_var2, seg_right)
    bndry = dl.CompiledSubDomain(string)
    return bndry


def on_any_boundary(x, on_boundary):
    return on_boundary


def get_all_boundaries():
    bndry = dl.CompiledSubDomain("on_boundary")
    return bndry


def get_1d_dirichlet_boundary_conditions_from_expression(expression, xl, xr):
    left_bndry = dl.CompiledSubDomain(
        "near(x[0],%e)&&on_boundary" % xl)
    right_bndry = dl.CompiledSubDomain(
        "near(x[0],%e)&&on_boundary" % xr)
    bndry_obj = [left_bndry, right_bndry]
    boundary_conditions = [
        ['dirichlet', bndry_obj[ii], expression]
        for ii in range(len(bndry_obj))]
    return boundary_conditions


def get_dirichlet_boundary_conditions_from_expression(expression, xl, xr, yb,
                                                      yt):
    bndry_obj = get_2d_rectangular_mesh_boundaries(xl, xr, yb, yt)
    boundary_conditions = [
        ['dirichlet', bndry_obj[ii], expression]
        for ii in range(len(bndry_obj))]
    return boundary_conditions


def get_robin_boundary_conditions_from_expression(expression, alpha):
    bndry_obj = get_2d_unit_square_mesh_boundaries()
    boundary_conditions = []
    ii = 0
    for phys_var in [0, 1]:
        for normal in [1, -1]:
            boundary_conditions.append(
                ['robin', bndry_obj[ii], expression(phys_var, normal), alpha])
            ii += 1
    return boundary_conditions


def copy_expression(expr):
    if hasattr(expr, 'cppcode'):
        # old fenics versions
        new_expr = dla.Expression(expr.cppcode, **expr.user_parameters,
                                  degree=expr.ufl_element().degree())
    else:
        # fenics 2019
        new_expr = dla.Expression(expr._cppcode, **expr._user_parameters,
                                  degree=expr.ufl_element().degree())
    return new_expr


def mark_boundaries(mesh, boundary_conditions):
    num_bndrys = len(boundary_conditions)
    boundaries = dl.MeshFunction("size_t", mesh, mesh.topology().dim()-1)
    assert num_bndrys < 9999
    boundaries.set_all(9999)
    num_bndrys = len(boundary_conditions)
    for ii in range(num_bndrys):
        boundary_conditions[ii][1].mark(boundaries, ii)
    return boundaries


def collect_dirichlet_boundaries(function_space, boundary_conditions,
                                 boundaries):
    num_bndrys = len(boundary_conditions)
    dirichlet_bcs = []
    for ii in range(num_bndrys):
        if boundary_conditions[ii][0] == 'dirichlet':
            bc_expr = boundary_conditions[ii][2]
            # ii must be same marker number as used in mark_boundaries()
            dirichlet_bcs.append(
                dla.DirichletBC(function_space, bc_expr, boundaries, ii))
    return dirichlet_bcs


def get_boundary_indices(function_space):
    bc_map = dla.Function(function_space)
    bc = dla.DirichletBC(function_space, dla.Constant(1.0), 'on_boundary')
    bc.apply(bc_map.vector())
    indices = np.arange(bc_map.vector().size())[
        bc_map.vector().get_local() == 1.0]
    return indices


def save_fenics_function(function, filename):
    function_space = function.function_space()
    fFile = dl.HDF5File(function_space.mesh().mpi_comm(), filename, "w")
    fFile.write(function, "/f")
    fFile.close()


def load_fenics_function(function_space, filename):
    function = dla.Function(function_space)
    fFile = dl.HDF5File(function_space.mesh().mpi_comm(), filename, "r")
    fFile.read(function, "/f")
    fFile.close()
    return function


def get_num_subdomain_dofs(Vh, subdomain):
    """
    Get the number of dofs on a subdomain
    """
    temp = dla.Function(Vh)
    bc = dla.DirichletBC(Vh, dla.Constant(1.0), subdomain)
    # warning applying bc does not just apply subdomain.inside to all
    # coordinates it does some boundary points more than once and other
    # inside points not at all.
    bc.apply(temp.vector())
    vec = temp.vector().get_local()
    dl.plot(temp)
    import matplotlib.pyplot as plt
    plt.show()
    return np.where(vec > 0)[0].shape[0]


def get_surface_of_3d_function(Vh_2d, z, function):
    for V in Vh_2d.split():
        assert V.ufl_element().degree() == 1
    mesh_coords = Vh_2d.mesh().coordinates().reshape((-1, 2))
    v_2d = dl.vertex_to_dof_map(Vh_2d)
    # v_2d = Vh_2d.dofmap().dofs()
    values = np.zeros(Vh_2d.dim(), dtype=float)
    for ii in range(mesh_coords.shape[0]):
        y = function(mesh_coords[ii, 0], mesh_coords[ii, 1], z)
        if np.isscalar(y):
            stride = 1
        else:
            stride = len(y)
        dofs = [v_2d[stride*ii+jj] for jj in range(stride)]
        # print(dofs,y)
        values[dofs] = y

    function_2d = dla.Function(Vh_2d)
    function_2d.vector()[:] = values
    return function_2d


def split_function_recursively(function):
    """
    Example
    -------
    P1 = dl.VectorElement("Lagrange", mesh_2d.ufl_cell(), degree=1)
    P2 = dl.FiniteElement("Lagrange", mesh_2d.ufl_cell(), degree=1)
    V = FunctionSpace(V1*V2*V2).

    P1*P2*P2 creates mixed spaces recursively so we must splir recursively
    For f=Funcion(V)
    f.split() will create 2 functions f1 and f2 associated with the
    function spaces P1 and P2 * P2 then we can call
    f1.split again to decompose functions associated with P1.
    """
    result = []
    sub_functions = function.split(True)
    nsub_functions = len(sub_functions)
    if nsub_functions == 0:
        return [function]
    for ii in range(len(sub_functions)):
        result += split_function_recursively(sub_functions[ii])
    return result


def plot_functions(functions, nrows=1):
    import matplotlib.pyplot as plt
    nfunctions = len(functions)
    if nrows == 1:
        ncols = nfunctions
    ncols = int(np.ceil(nfunctions/nrows))
    for ii in range(nfunctions):
        ax = plt.subplot(nrows, ncols, ii+1)
        pp = dl.plot(functions[ii])
        plt.colorbar(pp)


def homogenize_boundaries(bcs):
    if isinstance(bcs, dla.DirichletBC):
        bcs = [bcs]
    hbcs = [dla.DirichletBC(bc) for bc in bcs]
    for hbc in hbcs:
        hbc.homogenize()
    return hbcs


def info_red(msg):
    print('\033[91m'+msg+'\033[0m')


def info_blue(msg):
    print('\033[94m'+msg+'\033[0m')


def info_green(msg):
    print('\033[92m'+msg+'\033[0m')


def compute_errors(u_e, u):
    """Compute various measures of the error u - u_e, where
    u is a finite element Function and u_e is an Expression.

    Adapted from https://fenicsproject.org/pub/tutorial/html/._ftut1020.html
    """
    print('u_e', u_e.ufl_element().degree())
    # Get function space
    V = u.function_space()

    # Explicit computation of L2 norm
    error = (u - u_e)**2*dl.dx
    E1 = np.sqrt(abs(dla.assemble(error)))

    # Explicit interpolation of u_e onto the same space as u
    u_e_ = dla.interpolate(u_e, V)
    error = (u - u_e_)**2*dl.dx
    E2 = np.sqrt(abs(dla.assemble(error)))

    # Explicit interpolation of u_e to higher-order elements.
    # u will also be interpolated to the space Ve before integration
    Ve = dl.FunctionSpace(V.mesh(), 'P', 5)
    u_e_ = dla.interpolate(u_e, Ve)
    error = (u - u_e)**2*dl.dx
    E3 = np.sqrt(abs(dla.assemble(error)))

    # Infinity norm based on nodal values
    u_e_ = dla.interpolate(u_e, V)
    E4 = abs(u_e_.vector().get_local() - u.vector().get_local()).max()

    # L2 norm
    E5 = dl.errornorm(u_e, u, norm_type='L2', degree_rise=3)

    # H1 seminorm
    E6 = dl.errornorm(u_e, u, norm_type='H10', degree_rise=3)

    # Collect error measures in a dictionary with self-explanatory keys
    errors = {'u - u_e': E1,
              'u - interpolate(u_e, V)': E2,
              'interpolate(u, Ve) - interpolate(u_e, Ve)': E3,
              'infinity norm (of dofs)': E4,
              'L2 norm': E5,
              'H10 seminorm': E6}

    return errors


def compute_convergence_rates(run_model, u_e, max_degree=1, num_levels=5,
                              min_n=8, min_degree=1):
    """Compute convergences rates for various error norms
    Adapted from https://fenicsproject.org/pub/tutorial/html/._ftut1020.html
    """

    h = {}  # discretization parameter: h[degree][level]
    E = {}  # error measure(s): E[degree][level][error_type]

    # Iterate over degrees and mesh refinement levels
    degrees = range(min_degree, max_degree + 1)
    for degree in degrees:
        n = min_n  # coarsest mesh division
        h[degree] = []
        E[degree] = []
        for ii in range(num_levels):
            h[degree].append(1.0 / n)
            u = run_model(n, degree)
            if (hasattr(u_e, 'function_space') and
                    u.function_space() != u_e.function_space()):
                V = dl.FunctionSpace(
                    u.function_space().mesh(), u_e.ufl_element().family(),
                    u_e.ufl_element().degree())
                u_e_interp = dla.Function(V)
                u_e_interp.interpolate(u_e)
                errors = compute_errors(u_e_interp, u)
            else:
                # if not hasattr(u_e,'function_space') the u_e is an expression
                errors = compute_errors(u_e, u)
            E[degree].append(errors)
            print('2 x (%d x %d) P%d mesh, %d unknowns, E1 = %g' %
                  (n, n, degree, u.function_space().dim(), errors['u - u_e']))
            n *= 2

    # Compute convergence rates
    from math import log as ln  # log is a fenics name too
    etypes = list(E[min_degree][0].keys())
    rates = {}
    for degree in degrees:
        rates[degree] = {}
        for error_type in sorted(etypes):
            rates[degree][error_type] = []
            for i in range(1, num_levels):
                Ei = E[degree][i][error_type]
                Eim1 = E[degree][i - 1][error_type]
                r = ln(Ei / Eim1) / ln(h[degree][i] / h[degree][i - 1])
                rates[degree][error_type].append(round(r, 2))

    return etypes, degrees, rates, E


def convergence_order(errors, base=2):
    orders = [0.0] * (len(errors) - 1)
    for i in range(len(errors) - 1):
        if errors[i+1] == 0:
            errors[i] = 0.0
        else:
            ratio = errors[i]/errors[i+1]
            if ratio == 0:
                orders[i] = 0
            else:
                orders[i] = math.log(ratio, base)
    return orders


class RectangularMeshPeriodicBoundary(dl.SubDomain):
    """
    domain [0,Lx],[0,Ly]
    y-boundary is periodic, i.e. top and bottom boundaries
    """

    def __init__(self, Ly, **kwargs):
        self.Ly = Ly
        dl.SubDomain.__init__(self, **kwargs)

    # Left boundary is "target domain" G
    def inside(self, x, on_boundary):
        return dl.near(x[0], 0) and on_boundary

    # Map right boundary (H) to left boundary (G)
    def map(self, x, y):
        y[0] = x[0]
        y[1] = x[1]-self.Ly


def get_vertices_of_polygon(ampothem, nedges):
    assert np.issubdtype(type(nedges), np.integer)
    circumradius = ampothem / np.cos(np.pi/nedges)
    vertices = []
    for t in np.linspace(0, 2*np.pi, nedges+1)[:-1]+np.pi/nedges:
        vertex = [circumradius*np.cos(t), circumradius*np.sin(t)]
        vertices.append(vertex)
    vertices = np.array(vertices).T
    # print(vertices.T)
    # plt.plot(vertices[0,:],vertices[1,:],'o-')
    # plt.show()
    return vertices


def generate_polygonal_mesh(resolution, ampothem, nedges, radius,
                            plot_mesh=False):
    """
    Sometimes segault is thrown when mshr.generate_mesh() is
    called. This is because resolution is to low to resolve
    smaller inner-most circle.
    """
    import mshr
    vertices = get_vertices_of_polygon(ampothem, nedges)

    domain_vertices = []
    for vertex in vertices.T:
        domain_vertices.append(dl.Point(vertex[0], vertex[1]))

    domain = mshr.Polygon(domain_vertices)

    cx1, cy1 = 0.0, 0.0
    circle1 = mshr.Circle(dl.Point(cx1, cy1), radius)
    domain.set_subdomain(1, circle1)
    cx2, cy2 = cx1-radius/np.sqrt(8), cy1-radius/np.sqrt(8)
    circle2 = mshr.Circle(dl.Point(cx2, cy2), radius/2)
    domain.set_subdomain(2, circle2)
    mesh = mshr.generate_mesh(domain, resolution)

    if plot_mesh:
        subdomains = dl.MeshFunction('size_t', mesh, mesh.topology().dim(), 2)
        subdomains.set_all(0)
        subdomain1 = dl.AutoSubDomain(
            lambda x: np.sqrt((x[0]-cx1)**2+(x[1]-cy1)**2) < radius+1e-8)
        subdomain1.mark(subdomains, 1)
        subdomain2 = dl.AutoSubDomain(
            lambda x: np.sqrt((x[0]-cx2)**2+(x[1]-cy2)**2) < radius/2+1e-8)
        subdomain2.mark(subdomains, 2)
        import matplotlib.pyplot as plt
        dl.plot(mesh)
        dl.plot(subdomains)
        plt.show()

    return mesh


def get_polygon_boundary_segments(ampothem, nedges, nsegments_per_edge=None,
                                  cumulative_segment_sizes=None):
    bndry_obj = []
    vertices = get_vertices_of_polygon(ampothem, nedges)
    if cumulative_segment_sizes is None:
        assert nsegments_per_edge is not None
        cumulative_segment_sizes = np.arange(
            1, nsegments_per_edge+1)/nsegments_per_edge
    else:
        assert (nsegments_per_edge is None or
                nsegments_per_edge == len(cumulative_segment_sizes))
        nsegments_per_edge = len(cumulative_segment_sizes)
    x1, y1 = vertices[:, -1]
    for ii in range(vertices.shape[1]):
        x2, y2 = vertices[:, ii]
        pt_begin, pt_end = np.array([x1, y1]), np.array([x2, y2])
        pt_diff = pt_end-pt_begin
        p1 = pt_begin
        for jj in range(nsegments_per_edge):
            # p2 = pt_begin+pt_diff*(jj+1)/nsegments_per_edge
            p2 = pt_begin+pt_diff*cumulative_segment_sizes[jj]
            bndry_seg = get_2d_bndry_segment(p1[0], p1[1], p2[0], p2[1])
            bndry_obj.append(bndry_seg)
            p1 = p2.copy()
        x1, y1 = x2, y2
    return bndry_obj


def get_2d_bndry_segment(x1, y1, x2, y2):
    """
    Define boundary segment along the line between (x1,y1) and (x2,y2)
    Assumes x1,y1 x2,y2 come in clockwise order
    """
    # print(dl.DOLFIN_EPS)
    tol = 1e-12
    if abs(x2-x1) > tol and abs(y2-y1) > tol:
        m = (y2-y1)/(x2-x1)
        bndry = dl.CompiledSubDomain(
            "near(x[1]-m*(x[0]-x1)-y1,0,tol)&&((m*(x[0]-x1)+y1)<std::max(y1,y2)+DOLFIN_EPS)&&((m*(x[0]-x1)+y1)>std::min(y1,y2)-DOLFIN_EPS)&&on_boundary", m=m, x1=x1, y1=y1, x2=x2, y2=y2, tol=tol)
    elif abs(x2-x1) < tol:
        I = np.argsort([y1, y2])
        x1, x2 = np.array([x1, x2])[I]
        y1, y2 = np.array([y1, y2])[I]
        bndry = dl.CompiledSubDomain(
            "near(x[0],x1,tol)&&((x[1]-y1)>-DOLFIN_EPS)&&((x[1]-y2)<DOLFIN_EPS)&&on_boundary", x1=x1, y1=y1, y2=y2, tol=tol)
    else:
        I = np.argsort([x1, x2])
        x1, x2 = np.array([x1, x2])[I]
        y1, y2 = np.array([y1, y2])[I]
        bndry = dl.CompiledSubDomain(
            "near(x[1],y1,tol)&&((x[0]-x1)>-DOLFIN_EPS)&&((x[0]-x2)<DOLFIN_EPS)&&on_boundary", y1=y1, x1=x1, x2=x2, tol=tol)

    return bndry


class FenicsMeshKLE(MeshKLE):
    def __init__(self, function_space, mean_field=0):
        self.function_space = function_space
        mesh_coords = self.function_space.tabulate_dof_coordinates().T
        if type(mean_field) == dl.Function or type(mean_field) == dla.Function:
            super().__init__(
                mesh_coords, mean_field.vector().get_local().copy())
        elif np.isscalar(mean_field):
            super().__init__(mesh_coords, mean_field)
        else:
            raise Exception()

    def __call__(self, coef):
        assert coef.shape[1] == 1
        np_field = super().__call__(coef)[:, 0]
        field = dla.Function(self.function_space)
        field.vector()[:] = np_field
        return field


"""
NOTES
If we want to set Dirichlet conditions for individual components of the
system, this can be done as usual by the class DirichletBC, but we must
specify for which subsystem we set the boundary condition. For example,
if u=u1,u2,u3 and we want to specify that u2 should be equal to xy on
the boundary defined by boundary, we do
u_D = Expression('x[0]*x[1]', degree=1)
bc = DirichletBC(V.sub(1), u_D, boundary)

The ds variable implies a boundary integral, while dx implies an
integral over the domain
The special symbol ds(0) implies integration over subdomain (part) 0,
ds(1) denotes integration over subdomain (part) 1, and so on.
The idea of multiple ds-type objects generalizes to volume integrals too:
dx(0), dx(1), etc., are used to integrate over subdomain 0, 1, etc.

Consider a mixed function space

P2 = dl.VectorElement("Lagrange", mesh.ufl_cell(), 2)
P1 = dl.FiniteElement("Lagrange", mesh.ufl_cell(), 1)
TH = P2 * P1
Vh = dl.FunctionSpace(mesh, TH)

The following will cause the error:
 'Cannot access a non-const vector from a subfunction.'
temp = dl.Function(Vh)
temp1 = temp.split()[0].split()[0]
vec = temp1.vector().get_local()
To avoid this set deep copy to True in split(), i.e.
temp1 = temp.split(True)[0].split(True)[0]

Example of setting linear solver parameters
dl.info(dl.parameters,verbose=True)
dl.list_linear_solver_methods()
dl.list_krylov_solver_preconditioners()
linsolver_opts={'linear_solver':'cg', 'preconditioner':'gmres',
                 'krylov_solver':{'monitor_convergence':True,
                                  'relative_tolerance':1e-8}}
dl.solve(a==L,solver_parameters=linsolver_opts)

"""
