import numpy as np
from functools import partial

# from pyapprox.polynomial_sampling import christoffel_weights
from pyapprox.univariate_polynomials.quadrature import leja_growth_rule
from pyapprox.univariate_polynomials.orthonormal_polynomials import \
    evaluate_orthonormal_polynomial_deriv_1d
from pyapprox.univariate_polynomials.leja_sequences import \
    get_candidate_based_christoffel_leja_sequence_1d, \
    get_leja_sequence_quadrature_weights
from pyapprox.univariate_polynomials.recursion_factory import \
    get_recursion_coefficients_from_variable

from pyapprox.univariate_polynomials.leja_sequences import \
    get_christoffel_leja_sequence_1d, \
    get_christoffel_leja_quadrature_weights_1d, \
    get_pdf_weighted_leja_sequence_1d, \
    get_pdf_weighted_leja_quadrature_weights_1d
from pyapprox.utilities import beta_pdf, beta_pdf_derivative, \
    gaussian_pdf, gaussian_pdf_derivative

# TODO remove dependence on these packages if possible
from pyapprox.variables import is_bounded_continuous_variable, \
    is_continuous_variable, get_distribution_info, transform_scale_parameters


def candidate_based_christoffel_leja_rule_1d(
        recursion_coeffs, generate_candidate_samples, num_candidate_samples,
        level, initial_points=None, growth_rule=leja_growth_rule,
        samples_filename=None, return_weights_for_all_levels=True):

    num_leja_samples = growth_rule(level)

    leja_sequence = get_candidate_based_christoffel_leja_sequence_1d(
        num_leja_samples, recursion_coeffs, generate_candidate_samples,
        num_candidate_samples, initial_points, samples_filename)

    def generate_basis_matrix(x):
        return evaluate_orthonormal_polynomial_deriv_1d(
            x[0, :], num_leja_samples, recursion_coeffs, deriv_order=0)

    def weight_function(x):
        return 1./np.sum(generate_basis_matrix(x)**2, axis=1)

    ordered_weights_1d = get_leja_sequence_quadrature_weights(
        leja_sequence, growth_rule, generate_basis_matrix, weight_function,
        level, return_weights_for_all_levels)

    return leja_sequence[0, :], ordered_weights_1d


def transform_initial_samples(variable, initial_points):
    loc, scale = transform_scale_parameters(variable)
    if is_bounded_continuous_variable(variable):
        bounds = [-1, 1]
        if initial_points is None:
            initial_points = np.asarray(
                [[variable.ppf(0.5)]]).T
            initial_points = (initial_points-loc)/scale
        # initial samples must be in canonical space
        assert np.all((initial_points >= bounds[0]) &
                      (initial_points <= bounds[1]))
        return initial_points, bounds

    bounds = list(variable.interval(1))
    if variable.dist.name == 'continuous_rv_sample':
        bounds = [-np.inf, np.inf]
    if initial_points is None:
        # creating a leja sequence with initial points == 0
        # e.g. norm(0, 1).ppf(0.5) will cause leja sequence to
        # try to add point at infinity. So use different initial point
        initial_points = np.asarray(
            [[variable.ppf(0.75)]]).T
        initial_points = (initial_points-loc)/scale
    if initial_points.shape[1] == 1:
        assert initial_points[0, 0] != 0

    return initial_points, bounds


def univariate_christoffel_leja_quadrature_rule(
        variable, growth_rule, level, return_weights_for_all_levels=True,
        initial_points=None,
        orthonormality_tol=1e-12):
    """
    Return the samples and weights of the Leja quadrature rule for any
    continuous variable using the inverse Christoffel weight function

    By construction these rules have polynomial ordering.

    Parameters
    ----------
    variable : scipy.stats.dist
        The variable used to construct an orthogonormal polynomial

    growth_rule : callable
        Function which returns the number of samples in the quadrature rule
        With signature

        `growth_rule(level) -> integer`

        where level is an integer

    level : integer
        The level of the univariate rule.

    return_weights_for_all_levels : boolean
        True  - return weights [w(0),w(1),...,w(level)]
        False - return w(level)

    initial_points : np.ndarray (1, ninit_samples)
        Any points that must be included in the Leja sequence. This argument
        is typically used to pass in previously computed sequence which
        is updated efficiently here. MUST be in the canonical domain

    Return
    ------
    ordered_samples_1d : np.ndarray (num_samples_1d)
        The reordered samples.

    ordered_weights_1d : np.ndarray (num_samples_1d)
        The reordered weights.
    """
    if not is_continuous_variable(variable):
        raise Exception('Only supports continuous variables')

    name, scales, shapes = get_distribution_info(variable)
    max_nsamples = growth_rule(level)
    opts = {"orthonormality_tol":
            orthonormality_tol}
    ab = get_recursion_coefficients_from_variable(
        variable, max_nsamples+1, opts)
    basis_fun = partial(
        evaluate_orthonormal_polynomial_deriv_1d, ab=ab)

    initial_points, bounds = transform_initial_samples(
        variable, initial_points)

    leja_sequence = get_christoffel_leja_sequence_1d(
        max_nsamples, initial_points, bounds, basis_fun,
        {'gtol': 1e-8, 'verbose': False}, callback=None)

    __basis_fun = partial(basis_fun, nmax=max_nsamples-1, deriv_order=0)
    ordered_weights_1d = get_christoffel_leja_quadrature_weights_1d(
        leja_sequence, growth_rule, __basis_fun, level, True)
    if return_weights_for_all_levels:
        return leja_sequence[0, :], ordered_weights_1d
    return leja_sequence[0, :], ordered_weights_1d[-1]


def get_pdf_weight_functions(variable):
    name, scales, shapes = get_distribution_info(variable)
    if name == 'uniform' or name == 'beta':
        if name == 'uniform':
            alpha_stat, beta_stat = 1, 1
        else:
            alpha_stat, beta_stat = shapes['a'], shapes['b']

        def pdf(x):
            return beta_pdf(alpha_stat, beta_stat, (x+1)/2)/2

        def pdf_jac(x):
            return beta_pdf_derivative(alpha_stat, beta_stat, (x+1)/2)/4
        return pdf, pdf_jac

    if name == 'norm':
        return partial(gaussian_pdf, 0, 1), \
            partial(gaussian_pdf_derivative, 0, 1)

    raise ValueError(f'var_type {name} not supported')


def univariate_pdf_weighted_leja_quadrature_rule(
        variable, growth_rule, level, return_weights_for_all_levels=True,
        initial_points=None,
        orthonormality_tol=1e-12):
    """
    Return the samples and weights of the Leja quadrature rule for any
    continuous variable using the PDF of the random variable as the
    weight function

    By construction these rules have polynomial ordering.

    Parameters
    ----------
    variable : scipy.stats.dist
        The variable used to construct an orthogonormal polynomial

    growth_rule : callable
        Function which returns the number of samples in the quadrature rule
        With signature

        `growth_rule(level) -> integer`

        where level is an integer

    level : integer
        The level of the univariate rule.

    return_weights_for_all_levels : boolean
        True  - return weights [w(0),w(1),...,w(level)]
        False - return w(level)

    initial_points : np.ndarray (1, ninit_samples)
        Any points that must be included in the Leja sequence. This argument
        is typically used to pass in previously computed sequence which
        is updated efficiently here.  MUST be in the canonical domain

    Return
    ------
    ordered_samples_1d : np.ndarray (num_samples_1d)
        The reordered samples.

    ordered_weights_1d : np.ndarray (num_samples_1d)
        The reordered weights.
    """
    if not is_continuous_variable(variable):
        raise Exception('Only supports continuous variables')

    name, scales, shapes = get_distribution_info(variable)
    max_nsamples = growth_rule(level)
    opts = {"orthonormality_tol": orthonormality_tol}
    ab = get_recursion_coefficients_from_variable(
        variable, max_nsamples+1, opts)
    basis_fun = partial(evaluate_orthonormal_polynomial_deriv_1d, ab=ab)

    pdf, pdf_jac = get_pdf_weight_functions(variable)

    initial_points, bounds = transform_initial_samples(
        variable, initial_points)

    leja_sequence = get_pdf_weighted_leja_sequence_1d(
        max_nsamples, initial_points, bounds, basis_fun, pdf, pdf_jac,
        {'gtol': 1e-8, 'verbose': False}, callback=None)

    __basis_fun = partial(basis_fun, nmax=max_nsamples-1, deriv_order=0)
    ordered_weights_1d = get_pdf_weighted_leja_quadrature_weights_1d(
        leja_sequence, growth_rule, pdf, __basis_fun, level, True)

    if return_weights_for_all_levels:
        return leja_sequence[0, :], ordered_weights_1d
    return leja_sequence[0, :], ordered_weights_1d[-1]


def get_discrete_univariate_leja_quadrature_rule(
        variable, growth_rule, initial_points=None,
        orthonormality_tol=1e-12, return_weights_for_all_levels=True):
    from pyapprox.variables import get_probability_masses, \
        is_bounded_discrete_variable
    var_name = get_distribution_info(variable)[0]
    if is_bounded_discrete_variable(variable):
        xk, pk = get_probability_masses(variable)
        loc, scale = transform_scale_parameters(variable)
        xk = (xk-loc)/scale

        if initial_points is None:
            initial_points = (np.atleast_2d([variable.ppf(0.5)])-loc)/scale
        # initial samples must be in canonical space
        assert np.all((initial_points >= -1) & (initial_points <= 1))
        assert np.all((xk >= -1) & (xk <= 1))

        def generate_candidate_samples(num_samples):
            return xk[None, :]

        opts = {"orthonormality_tol": orthonormality_tol}
        ab = get_recursion_coefficients_from_variable(
            variable, xk.shape[0], opts)
        quad_rule = partial(
            candidate_based_christoffel_leja_rule_1d, ab,
            generate_candidate_samples, xk.shape[0], growth_rule=growth_rule,
            initial_points=initial_points,
            return_weights_for_all_levels=return_weights_for_all_levels)
    else:
        raise ValueError('var_name %s not implemented' % var_name)
    return quad_rule


def get_univariate_leja_quadrature_rule(
        variable,
        growth_rule,
        method='pdf',
        orthonormality_tol=1e-11,
        initial_points=None,
        return_weights_for_all_levels=True):

    if not is_continuous_variable(variable):
        return get_discrete_univariate_leja_quadrature_rule(
            variable, growth_rule,
            orthonormality_tol=orthonormality_tol,
            initial_points=initial_points,
            return_weights_for_all_levels=return_weights_for_all_levels)

    if method == 'christoffel':
        return partial(
            univariate_christoffel_leja_quadrature_rule, variable, growth_rule,
            orthonormality_tol=orthonormality_tol,
            initial_points=initial_points,
            return_weights_for_all_levels=return_weights_for_all_levels)

    if method == 'pdf':
        return partial(
            univariate_pdf_weighted_leja_quadrature_rule,
            variable, growth_rule,
            orthonormality_tol=orthonormality_tol,
            initial_points=initial_points,
            return_weights_for_all_levels=return_weights_for_all_levels)

    raise ValueError(f"Method {method} not supported")
