from functools import partial

from warnings import warn

import numpy as np
from numpy.polynomial.legendre import leggauss

from scipy.special import erf, beta as beta_fn, gammaln
from scipy.linalg import solve_triangular
from scipy.linalg import lapack

from pyapprox.pya_numba import njit

from .sys_utilities import hash_array


def sub2ind(sizes, multi_index):
    r"""
    Map a d-dimensional index to the scalar index of the equivalent flat
    1D array

    Examples
    --------

    .. math::

       \begin{bmatrix}
       0,0 & 0,1 & 0,2\\
       1,0 & 1,1 & 1,2\\
       2,0 & 2,1 & 2,2
       \end{bmatrix}
       \rightarrow
       \begin{bmatrix}
       0 & 3 & 6\\
       1 & 4 & 7\\
       2 & 5 & 8
       \end{bmatrix}

    >>> from pyapprox.utilities import sub2ind
    >>> sizes = [3,3]
    >>> ind = sub2ind(sizes,[1,0])
    >>> print(ind)
    1

    Parameters
    ----------
    sizes : integer
        The number of elems in each dimension. For a 2D index
        sizes = [numRows, numCols]

    multi_index : np.ndarray (len(sizes))
       The d-dimensional index

    Returns
    -------
    scalar_index : integer
        The scalar index

    See Also
    --------
    pyapprox.utilities.sub2ind
    """
    num_sets = len(sizes)
    scalar_index = 0
    shift = 1
    for ii in range(num_sets):
        scalar_index += shift * multi_index[ii]
        shift *= sizes[ii]
    return scalar_index


def ind2sub(sizes, scalar_index, num_elems):
    r"""
    Map a scalar index of a flat 1D array to the equivalent d-dimensional index

    Examples
    --------

    .. math::

        \begin{bmatrix}
        0 & 3 & 6\\
        1 & 4 & 7\\
        2 & 5 & 8
        \end{bmatrix}
        \rightarrow
        \begin{bmatrix}
        0,0 & 0,1 & 0,2\\
        1,0 & 1,1 & 1,2\\
        2,0 & 2,1 & 2,2
        \end{bmatrix}

    >>> from pyapprox.utilities import ind2sub
    >>> sizes = [3,3]
    >>> sub = ind2sub(sizes,1,9)
    >>> print(sub)
    [1 0]

    Parameters
    ----------
    sizes : integer
        The number of elems in each dimension. For a 2D index
        sizes = [numRows, numCols]

    scalar_index : integer
        The scalar index

    num_elems : integer
        The total number of elements in the d-dimensional matrix

    Returns
    -------
    multi_index : np.ndarray (len(sizes))
       The d-dimensional index

    See Also
    --------
    pyapprox.utilities.sub2ind
    """
    denom = num_elems
    num_sets = len(sizes)
    multi_index = np.empty((num_sets), dtype=int)
    for ii in range(num_sets-1, -1, -1):
        denom /= sizes[ii]
        multi_index[ii] = scalar_index / denom
        scalar_index = scalar_index % denom
    return multi_index


def cartesian_product(input_sets, elem_size=1):
    r"""
    Compute the cartesian product of an arbitray number of sets.

    The sets can consist of numbers or themselves be lists or vectors. All
    the lists or vectors of a given set must have the same number of entries
    (elem_size). However each set can have a different number of scalars,
    lists, or vectors.

    Parameters
    ----------
    input_sets
        The sets to be used in the cartesian product.

    elem_size : integer
        The size of the vectors within each set.

    Returns
    -------
    result : np.ndarray (num_sets*elem_size, num_elems)
        The cartesian product. num_elems = np.prod(sizes)/elem_size,
        where sizes[ii] = len(input_sets[ii]), ii=0,..,num_sets-1.
        result.dtype will be set to the first entry of the first input_set
    """
    import itertools
    out = []
    # ::-1 reverse order to be backwards compatiable with old
    # function below
    for r in itertools.product(*input_sets[::-1]):
        out.append(r)
    out = np.asarray(out).T[::-1, :]
    return out

    # try:
    #     from pyapprox.cython.utilities import cartesian_product_pyx
    #     # # fused type does not work for np.in32, np.float32, np.int64
    #     # # so envoke cython cast
    #     # if np.issubdtype(input_sets[0][0],np.signedinteger):
    #     #     return cartesian_product_pyx(input_sets,1,elem_size)
    #     # if np.issubdtype(input_sets[0][0],np.floating):
    #     #     return cartesian_product_pyx(input_sets,1.,elem_size)
    #     # else:
    #     #     return cartesian_product_pyx(
    #     #         input_sets,input_sets[0][0],elem_size)
    #     # always convert to float then cast back
    #     cast_input_sets = [np.asarray(s, dtype=float) for s in input_sets]
    #     out = cartesian_product_pyx(cast_input_sets, 1., elem_size)
    #     out = np.asarray(out, dtype=input_sets[0].dtype)
    #     return out
    # except:
    #     print('cartesian_product extension failed')

    # num_elems = 1
    # num_sets = len(input_sets)
    # sizes = np.empty((num_sets), dtype=int)
    # for ii in range(num_sets):
    #     sizes[ii] = input_sets[ii].shape[0]/elem_size
    #     num_elems *= sizes[ii]
    # # try:
    # #    from pyapprox.weave import c_cartesian_product
    # #    # note c_cartesian_product takes_num_elems as last arg and cython
    # #    # takes elem_size
    # #    return c_cartesian_product(input_sets, elem_size, sizes, num_elems)
    # # except:
    # #    print ('cartesian_product extension failed')

    # result = np.empty(
    #     (num_sets*elem_size, num_elems), dtype=type(input_sets[0][0]))
    # for ii in range(num_elems):
    #     multi_index = ind2sub(sizes, ii, num_elems)
    #     for jj in range(num_sets):
    #         for kk in range(elem_size):
    #             result[jj*elem_size+kk, ii] =\
    #                 input_sets[jj][multi_index[jj]*elem_size+kk]
    # return result


def outer_product(input_sets, axis=0):
    r"""
    Construct the outer product of an arbitary number of sets.

    Examples
    --------

    .. math::

        \{1,2\}\times\{3,4\}=\{1\times3, 2\times3, 1\times4, 2\times4\} =
        \{3, 6, 4, 8\}

    Parameters
    ----------
    input_sets
        The sets to be used in the outer product

    Returns
    -------
    result : np.ndarray(np.prod(sizes))
       The outer product of the sets.
       result.dtype will be set to the first entry of the first input_set
    """
    out = cartesian_product(input_sets)
    return np.prod(out, axis=axis)

    # try:
    #     from pyapprox.cython.utilities import outer_product_pyx
    #     # fused type does not work for np.in32, np.float32, np.int64
    #     # so envoke cython cast
    #     if np.issubdtype(input_sets[0][0], np.signedinteger):
    #         return outer_product_pyx(input_sets, 1)
    #     if np.issubdtype(input_sets[0][0], np.floating):
    #         return outer_product_pyx(input_sets, 1.)
    #     else:
    #         return outer_product_pyx(input_sets, input_sets[0][0])
    # except ImportError:
    #     print('outer_product extension failed')

    # num_elems = 1
    # num_sets = len(input_sets)
    # sizes = np.empty((num_sets), dtype=int)
    # for ii in range(num_sets):
    #     sizes[ii] = len(input_sets[ii])
    #     num_elems *= sizes[ii]

    # # try:
    # #     from pyapprox.weave import c_outer_product
    # #     return c_outer_product(input_sets)
    # # except:
    # #     print ('outer_product extension failed')

    # result = np.empty((num_elems), dtype=type(input_sets[0][0]))
    # for ii in range(num_elems):
    #     result[ii] = 1.0
    #     multi_index = ind2sub(sizes, ii, num_elems)
    #     for jj in range(num_sets):
    #         result[ii] *= input_sets[jj][multi_index[jj]]

    # return result


def unique_matrix_rows(matrix):
    unique_rows = []
    unique_rows_set = set()
    for ii in range(matrix.shape[0]):
        key = hash_array(matrix[ii, :])
        if key not in unique_rows_set:
            unique_rows_set.add(key)
            unique_rows.append(matrix[ii, :])
    return np.asarray(unique_rows)


def remove_common_rows(matrices):
    num_cols = matrices[0].shape[1]
    unique_rows_dict = dict()
    for ii in range(len(matrices)):
        matrix = matrices[ii]
        assert matrix.shape[1] == num_cols
        for jj in range(matrix.shape[0]):
            key = hash_array(matrix[jj, :])
            if key not in unique_rows_dict:
                unique_rows_dict[key] = (ii, jj)
            elif unique_rows_dict[key][0] != ii:
                del unique_rows_dict[key]
            # else:
            # entry is a duplicate entry in the current. Allow this to
            # occur but only add one of the duplicates to the unique rows dict

    unique_rows = []
    for key in list(unique_rows_dict.keys()):
        ii, jj = unique_rows_dict[key]
        unique_rows.append(matrices[ii][jj, :])

    return np.asarray(unique_rows)


def allclose_unsorted_matrix_rows(matrix1, matrix2):
    if matrix1.shape != matrix2.shape:
        return False

    matrix1_dict = dict()
    for ii in range(matrix1.shape[0]):
        key = hash_array(matrix1[ii, :])
        # allow duplicates of rows
        if key not in matrix1_dict:
            matrix1_dict[key] = 0
        else:
            matrix1_dict[key] += 1

    matrix2_dict = dict()
    for ii in range(matrix2.shape[0]):
        key = hash_array(matrix2[ii, :])
        # allow duplicates of rows
        if key not in matrix2_dict:
            matrix2_dict[key] = 0
        else:
            matrix2_dict[key] += 1

    if len(list(matrix1_dict.keys())) != len(list(matrix2_dict.keys())):
        return False

    for key in list(matrix1_dict.keys()):
        if key not in matrix2_dict:
            return False
        if matrix2_dict[key] != matrix1_dict[key]:
            return False

    return True


def get_2d_cartesian_grid(num_pts_1d, ranges):
    r"""
    Get a 2d tensor grid with equidistant points.

    Parameters
    ----------
    num_pts_1d : integer
        The number of points in each dimension

    ranges : np.ndarray (4)
        The lower and upper bound of each dimension [lb_1,ub_1,lb_2,ub_2]

    Returns
    -------
    grid : np.ndarray (2,num_pts_1d**2)
        The points in the tensor product grid.
        [x1,x2,...x1,x2...]
        [y1,y1,...y2,y2...]
    """
    # from math_tools_cpp import cartesian_product_double as cartesian_product
    from PyDakota.math_tools import cartesian_product
    x1 = np.linspace(ranges[0], ranges[1], num_pts_1d)
    x2 = np.linspace(ranges[2], ranges[3], num_pts_1d)
    abscissa_1d = []
    abscissa_1d.append(x1)
    abscissa_1d.append(x2)
    grid = cartesian_product(abscissa_1d, 1)
    return grid


def invert_permutation_vector(p, dtype=int):
    r"""
    Returns the "inverse" of a permutation vector. I.e., returns the
    permutation vector that performs the inverse of the original
    permutation operation.

    Parameters
    ----------
    p: np.ndarray
        Permutation vector
    dtype: type
        Data type passed to np.ndarray constructor

    Returns
    -------
    pt: np.ndarray
        Permutation vector that accomplishes the inverse of the
        permutation p.
    """

    N = np.max(p) + 1
    pt = np.zeros(p.size, dtype=dtype)
    pt[p] = np.arange(N, dtype=dtype)
    return pt


def nchoosek(nn, kk):
    try:  # SciPy >= 0.19
        from scipy.special import comb
    except:
        from scipy.misc import comb
    result = np.asarray(np.round(comb(nn, kk)), dtype=int)
    if np.isscalar(result):
        result = np.asscalar(result)
    return result


def total_degree_space_dimension(dimension, degree):
    r"""
    Return the number of basis functions in a total degree polynomial space,
    i.e. the space of all polynomials with degree at most degree.

    Parameters
    ----------
    num_vars : integer
        The number of variables of the polynomials

    degree :
        The degree of the total-degree space

    Returns
    -------
    num_terms : integer
        The number of basis functions in the total degree space

    Notes
    -----
    Note

    .. math:: {n \choose k} = frac{\Gamma(n+k+1)}{\Gamma(k+1)\Gamma{n-k+1}}, \qquad \Gamma(m)=(m-1)!

    So for dimension :math:`d` and degree :math:`p` number of terms in
    subspace is

    .. math:: {d+p \choose p} = frac{\Gamma(d+p+1)}{\Gamma(p+1)\Gamma{d+p-p+1}}, \qquad \Gamma(m)=(m-1)!

    """
    # return nchoosek(dimension+degree, degree)
    # Following more robust for large values
    return int(np.round(
        np.exp(gammaln(degree+dimension+1) - gammaln(degree+1) - gammaln(
            dimension+1))))


def total_degree_subspace_dimension(dimension, degree):
    r"""
    Return the number of basis functions in a total degree polynomial space,
    with degree equal to degree.

    Parameters
    ----------
    num_vars : integer
        The number of variables of the polynomials

    degree :
        The degree of the total-degree space

    Returns
    -------
    num_terms : integer
        The number of basis functions in the total degree space of a given
        degree
    """
    # subspace_dimension = nchoosek(nvars+degree-1, degree)
    # Following more robust for large values
    subspace_dimension = int(
        np.round(np.exp(gammaln(degree+dimension) - gammaln(degree+1) -
                        gammaln(dimension))))
    return subspace_dimension


def total_degree_encompassing_N(dimension, N):
    r"""
    Returns the smallest integer k such that the dimension of the total
    degree-k space is greater than N.
    """

    k = 0
    while total_degree_subspace_dimension(dimension, k) < N:
        k += 1
    return k


def total_degree_barrier_indices(dimension, max_degree):
    r"""
    Returns linear indices that bound total degree spaces

    Parameters
    ----------
    dimension: int
        Parametric dimension
    max_degree: int
        Maximum polynomial degree

    Returns
    -------
    degree_barrier_indices: list
        List of degree barrier indices up to (including) max_degree.
    """
    degree_barrier_indices = [0]

    for degree in range(1, max_degree+1):
        degree_barrier_indices.append(
            total_degree_subspace_dimension(dimension, degree))

    return degree_barrier_indices


def total_degree_orthogonal_transformation(coefficients, d):
    r"""
    Returns an orthogonal matrix transformation that "matches" the input
    coefficients.

    Parameters
    ----------
    coefficients: np.ndarray
        Length-N vector of expansion coefficients
    d: int
        Parametric dimension

    Returns
    -------
    Q: np.ndarray
        A size N x N orthogonal matrix transformation. The first column
        is a unit vector in the direction of coefficients.
    """

    from scipy.linalg import qr

    N = coefficients.size

    degree_barrier_indices = [1]
    max_degree = 0
    while degree_barrier_indices[-1] < N-1:
        max_degree += 1
        degree_barrier_indices.append(
            total_degree_subspace_dimension(d, max_degree))

    q = np.zeros([N, N])

    # Assume degree = 0 is just constant
    q[0, 0] = 1.

    for degree in range(1, max_degree+1):
        i1 = degree_barrier_indices[degree-1]
        i2 = degree_barrier_indices[degree]

        M = i2-i1
        q[i1:i2, i1:i2] = qr(coefficients[i1:i2].reshape([M, 1]))[0]

    return q


def get_low_rank_matrix(num_rows, num_cols, rank):
    r"""
    Construct a matrix of size num_rows x num_cols with a given rank.

    Parameters
    ----------
    num_rows : integer
        The number rows in the matrix

    num_cols : integer
        The number columns in the matrix

    rank : integer
        The rank of the matrix

    Returns
    -------
    Amatrix : np.ndarray (num_rows,num_cols)
        The low-rank matrix generated
    """
    assert rank <= min(num_rows, num_cols)
    # Generate a matrix with normally distributed entries
    N = max(num_rows, num_cols)
    Amatrix = np.random.normal(0, 1, (N, N))
    # Make A symmetric positive definite
    Amatrix = np.dot(Amatrix.T, Amatrix)
    # Construct low rank approximation of A
    eigvals, eigvecs = np.linalg.eigh(Amatrix.copy())
    # Set smallest eigenvalues to zero. Note eigenvals are in
    # ascending order
    eigvals[:(eigvals.shape[0]-rank)] = 0.
    # Construct rank r A matrix
    Amatrix = np.dot(eigvecs, np.dot(np.diag(eigvals), eigvecs.T))
    # Resize matrix to have requested size
    Amatrix = Amatrix[:num_rows, :num_cols]
    return Amatrix


def adjust_sign_svd(U, V, adjust_based_upon_U=True):
    r"""
    Ensure uniquness of svd by ensuring the first entry of each left singular
    singular vector be positive. Only works for np.linalg.svd
    if full_matrices=False

    Parameters
    ----------
    U : (M x M) matrix
        left singular vectors of a singular value decomposition of a (M x N)
        matrix A.

    V : (N x N) matrix
        right singular vectors of a singular value decomposition of a (M x N)
        matrix A.

    adjust_based_upon_U : boolean (default=True)
        True - make the first entry of each column of U positive
        False - make the first entry of each row of V positive

    Returns
    -------
    U : (M x M) matrix
       left singular vectors with first entry of the first
       singular vector always being positive.

    V : (M x M) matrix
        right singular vectors consistent with sign adjustment applied to U.
    """
    if U.shape[1] != V.shape[0]:
        raise ValueError(
            'U.shape[1] must equal V.shape[0]. If using np.linalg.svd set full_matrices=False')

    if adjust_based_upon_U:
        s = np.sign(U[0, :])
    else:
        s = np.sign(V[:, 0])
    U *= s
    V *= s[:, np.newaxis]
    return U, V


def adjust_sign_eig(U):
    r"""
    Ensure uniquness of eigenvalue decompotision by ensuring the first entry
    of the first singular vector of U is positive.

    Parameters
    ----------
    U : (M x M) matrix
        left singular vectors of a singular value decomposition of a (M x M)
        matrix A.

    Returns
    -------
    U : (M x M) matrix
       left singular vectors with first entry of the first
       singular vector always being positive.
    """
    s = np.sign(U[0, :])
    U *= s
    return U


def sorted_eigh(C):
    r"""
    Compute the eigenvalue decomposition of a matrix C and sort
    the eigenvalues and corresponding eigenvectors by decreasing
    magnitude.

    Warning. This will prioritize large eigenvalues even if they
    are negative. Do not use if need to distinguish between positive
    and negative eigenvalues

    Input

    B: matrix (NxN)
      matrix to decompose

    Output

    e: vector (N)
      absolute values of the eigenvalues of C sorted by decreasing
      magnitude

    W: eigenvectors sorted so that they respect sorting of e
    """
    e, W = np.linalg.eigh(C)
    e = abs(e)
    ind = np.argsort(e)
    e = e[ind[::-1]]
    W = W[:, ind[::-1]]
    s = np.sign(W[0, :])
    s[s == 0] = 1
    W = W*s
    return e.reshape((e.size, 1)), W


def continue_pivoted_lu_factorization(LU_factor, raw_pivots, current_iter,
                                      max_iters, num_initial_rows=0):
    it = current_iter
    for it in range(current_iter, max_iters):

        # find best pivot
        if np.isscalar(num_initial_rows) and (it < num_initial_rows):
            # pivot=np.argmax(np.absolute(LU_factor[it:num_initial_rows,it]))+it
            pivot = it
        elif (not np.isscalar(num_initial_rows) and
              (it < num_initial_rows.shape[0])):
            pivot = num_initial_rows[it]
        else:
            pivot = np.argmax(np.absolute(LU_factor[it:, it]))+it

        # update pivots vector
        # swap_rows(pivots,it,pivot)
        raw_pivots[it] = pivot

        # apply pivots(swap rows) in L factorization
        swap_rows(LU_factor, it, pivot)

        # check for singularity
        if abs(LU_factor[it, it]) < np.finfo(float).eps:
            msg = "pivot %1.2e" % abs(LU_factor[it, it])
            msg += " is to small. Stopping factorization."
            print(msg)
            break

        # update L_factor
        LU_factor[it+1:, it] /= LU_factor[it, it]

        # udpate U_factor
        col_vector = LU_factor[it+1:, it]
        row_vector = LU_factor[it, it+1:]

        update = np.outer(col_vector, row_vector)
        LU_factor[it+1:, it+1:] -= update
    return LU_factor, raw_pivots, it


def unprecondition_LU_factor(LU_factor, precond_weights, num_pivots=None):
    r"""
    A=LU and WA=XY
    Then WLU=XY
    We also know Y=WU
    So WLU=XWU => WL=XW so L=inv(W)*X*W
    and U = inv(W)Y
    """
    if num_pivots is None:
        num_pivots = np.min(LU_factor.shape)
    assert precond_weights.shape[1] == 1
    assert precond_weights.shape[0] == LU_factor.shape[0]
    # left multiply L an U by inv(W), i.e. compute inv(W).dot(L)
    # and inv(W).dot(U)

    # `np.array` creates a new copy of LU_factor, faster than `.copy()`
    LU_factor = np.array(LU_factor)/precond_weights

    # right multiply L by W, i.e. compute L.dot(W)
    # Do not overwrite columns past num_pivots. If not all pivots have been
    # performed the columns to the right of this point contain U factor
    for ii in range(num_pivots):
        LU_factor[ii+1:, ii] *= precond_weights[ii, 0]

    return LU_factor


def split_lu_factorization_matrix(LU_factor, num_pivots=None):
    r"""
    Return the L and U factors of an inplace LU factorization

    Parameters
    ----------
    num_pivots : integer
        The number of pivots performed. This allows LU in place matrix
        to be split during evolution of LU algorithm
    """
    if num_pivots is None:
        num_pivots = np.min(LU_factor.shape)
    L_factor = np.tril(LU_factor)
    if L_factor.shape[1] < L_factor.shape[0]:
        # if matrix over-determined ensure L is a square matrix
        n0 = L_factor.shape[0]-L_factor.shape[1]
        L_factor = np.hstack([L_factor, np.zeros((L_factor.shape[0], n0))])
    if num_pivots < np.min(L_factor.shape):
        n1 = L_factor.shape[0]-num_pivots
        n2 = L_factor.shape[1]-num_pivots
        L_factor[num_pivots:, num_pivots:] = np.eye(n1, n2)
    np.fill_diagonal(L_factor, 1.)
    U_factor = np.triu(LU_factor)
    U_factor[num_pivots:, num_pivots:] = LU_factor[num_pivots:, num_pivots:]
    return L_factor, U_factor


def truncated_pivoted_lu_factorization(A, max_iters, num_initial_rows=0,
                                       truncate_L_factor=True):
    r"""
    Compute a incomplete pivoted LU decompostion of a matrix.

    Parameters
    ----------
    A np.ndarray (num_rows,num_cols)
        The matrix to be factored

    max_iters : integer
        The maximum number of pivots to perform. Internally max)iters will be
        set such that max_iters = min(max_iters,K), K=min(num_rows,num_cols)

    num_initial_rows: integer or np.ndarray()
        The number of the top rows of A to be chosen as pivots before
        any remaining rows can be chosen.
        If object is an array then entries are raw pivots which
        will be used in order.


    Returns
    -------
    L_factor : np.ndarray (max_iters,K)
        The lower triangular factor with a unit diagonal.
        K=min(num_rows,num_cols)

    U_factor : np.ndarray (K,num_cols)
        The upper triangular factor

    raw_pivots : np.ndarray (num_rows)
        The sequential pivots used to during algorithm to swap rows of A.
        pivots can be obtained from raw_pivots using
        get_final_pivots_from_sequential_pivots(raw_pivots)

    pivots : np.ndarray (max_iters)
        The index of the chosen rows in the original matrix A chosen as pivots
    """
    num_rows, num_cols = A.shape
    min_num_rows_cols = min(num_rows, num_cols)
    max_iters = min(max_iters, min_num_rows_cols)
    if (A.shape[1] < max_iters):
        msg = "truncated_pivoted_lu_factorization: "
        msg += " A is inconsistent with max_iters. Try deceasing max_iters or "
        msg += " increasing the number of columns of A"
        raise Exception(msg)

    # Use L to store both L and U during factoriation then copy out U in post
    # processing
    # `np.array` creates a new copy of A (faster than `.copy()`)
    LU_factor = np.array(A)
    raw_pivots = np.arange(num_rows)
    LU_factor, raw_pivots, it = continue_pivoted_lu_factorization(
        LU_factor, raw_pivots, 0, max_iters, num_initial_rows)

    if not truncate_L_factor:
        return LU_factor, raw_pivots
    else:
        pivots = get_final_pivots_from_sequential_pivots(
            raw_pivots)[:it+1]
        L_factor, U_factor = split_lu_factorization_matrix(LU_factor, it+1)
        L_factor = L_factor[:it+1, :it+1]
        U_factor = U_factor[:it+1, :it+1]
        return L_factor, U_factor, pivots


def add_columns_to_pivoted_lu_factorization(LU_factor, new_cols, raw_pivots):
    r"""
    Given factorization PA=LU add new columns to A in unpermuted order and
    update LU factorization

    Parameters
    ----------
    raw_pivots : np.ndarray (num_pivots)
        The pivots applied at each iteration of pivoted LU factorization.
        If desired one can use get_final_pivots_from_sequential_pivots to
        compute final position of rows after all pivots have been applied.
    """
    assert LU_factor.shape[0] == new_cols.shape[0]
    assert raw_pivots.shape[0] <= new_cols.shape[0]
    num_pivots = raw_pivots.shape[0]
    for it, pivot in enumerate(raw_pivots):
        # inlined swap_rows() for performance
        new_cols[[it, pivot]] = new_cols[[pivot, it]]

        # update LU_factor
        # recover state of col vector from permuted LU factor
        # Let  (jj,kk) represent iteration and pivot pairs
        # then if lu factorization produced sequence of pairs
        # (0,4),(1,2),(2,4) then LU_factor[:,0] here will be col_vector
        # in LU algorithm with the second and third permutations
        # so undo these permutations in reverse order
        next_idx = it+1

        # `col_vector` is a copy of the LU_factor subset
        col_vector = np.array(LU_factor[next_idx:, it])
        for ii in range(num_pivots-it-1):
            # (it+1) necessary in two lines below because only dealing
            # with compressed col vector which starts at row it in LU_factor
            jj = raw_pivots[num_pivots-1-ii]-next_idx
            kk = num_pivots-ii-1-next_idx

            # inlined swap_rows()
            col_vector[jj], col_vector[kk] = col_vector[kk], col_vector[jj]

        new_cols[next_idx:, :] -= np.outer(col_vector, new_cols[it, :])

    LU_factor = np.hstack((LU_factor, new_cols))

    return LU_factor


def add_rows_to_pivoted_lu_factorization(LU_factor, new_rows, num_pivots):
    assert LU_factor.shape[1] == new_rows.shape[1]
    LU_factor_extra = np.array(new_rows)  # take copy of `new_rows`
    for it in range(num_pivots):
        LU_factor_extra[:, it] /= LU_factor[it, it]
        col_vector = LU_factor_extra[:, it]
        row_vector = LU_factor[it, it+1:]
        update = np.outer(col_vector, row_vector)
        LU_factor_extra[:, it+1:] -= update

    return np.vstack([LU_factor, LU_factor_extra])


def swap_rows(matrix, ii, jj):
    matrix[[ii, jj]] = matrix[[jj, ii]]


def pivot_rows(pivots, matrix, in_place=True):
    if not in_place:
        matrix = matrix.copy()
    num_pivots = pivots.shape[0]
    assert num_pivots <= matrix.shape[0]
    for ii in range(num_pivots):
        swap_rows(matrix, ii, pivots[ii])
    return matrix


def get_final_pivots_from_sequential_pivots(
        sequential_pivots, num_pivots=None):
    if num_pivots is None:
        num_pivots = sequential_pivots.shape[0]
    assert num_pivots >= sequential_pivots.shape[0]
    pivots = np.arange(num_pivots)
    return pivot_rows(sequential_pivots, pivots, False)


def get_tensor_product_quadrature_rule(
        degrees, num_vars, univariate_quadrature_rules, transform_samples=None,
        density_function=None):
    r"""
    if get error about outer product failing it may be because
    univariate_quadrature rule is returning a weights array for every level,
    i.e. l=0,...level
    """
    degrees = np.atleast_1d(degrees)
    if degrees.shape[0] == 1 and num_vars > 1:
        degrees = np.array([degrees[0]]*num_vars, dtype=int)

    if callable(univariate_quadrature_rules):
        univariate_quadrature_rules = [univariate_quadrature_rules]*num_vars

    x_1d = []
    w_1d = []
    for ii in range(len(univariate_quadrature_rules)):
        x, w = univariate_quadrature_rules[ii](degrees[ii])
        x_1d.append(x)
        w_1d.append(w)
    samples = cartesian_product(x_1d, 1)
    weights = outer_product(w_1d)

    if density_function is not None:
        weights *= density_function(samples)
    if transform_samples is not None:
        samples = transform_samples(samples)
    return samples, weights


def piecewise_quadratic_interpolation(samples, mesh, mesh_vals, ranges):
    assert mesh.shape[0] == mesh_vals.shape[0]
    vals = np.zeros_like(samples)
    samples = (samples-ranges[0])/(ranges[1]-ranges[0])
    for ii in range(0, mesh.shape[0]-2, 2):
        xl = mesh[ii]
        xr = mesh[ii+2]
        x = (samples-xl)/(xr-xl)
        interval_vals = canonical_piecewise_quadratic_interpolation(
            x, mesh_vals[ii:ii+3])
        # to avoid double counting we set left boundary of each interval to
        # zero except for first interval
        if ii == 0:
            interval_vals[(x < 0) | (x > 1)] = 0.
        else:
            interval_vals[(x <= 0) | (x > 1)] = 0.
        vals += interval_vals
    return vals

    # I = np.argsort(samples)
    # sorted_samples = samples[I]
    # idx2=0
    # for ii in range(0,mesh.shape[0]-2,2):
    #     xl=mesh[ii]; xr=mesh[ii+2]
    #     for jj in range(idx2,sorted_samples.shape[0]):
    #         if ii==0:
    #             if sorted_samples[jj]>=xl:
    #                 idx1=jj
    #                 break
    #         else:
    #             if sorted_samples[jj]>xl:
    #                 idx1=jj
    #                 break
    #     for jj in range(idx1,sorted_samples.shape[0]):
    #         if sorted_samples[jj]>xr:
    #             idx2=jj-1
    #             break
    #     if jj==sorted_samples.shape[0]-1:
    #         idx2=jj
    #     x=(sorted_samples[idx1:idx2+1]-xl)/(xr-xl)
    #     interval_vals = canonical_piecewise_quadratic_interpolation(
    #         x,mesh_vals[ii:ii+3])
    #     vals[idx1:idx2+1] += interval_vals
    # return vals[np.argsort(I)]


def canonical_piecewise_quadratic_interpolation(x, nodal_vals):
    r"""
    Piecewise quadratic interpolation of nodes at [0,0.5,1]
    Assumes all values are in [0,1].
    """
    assert x.ndim == 1
    assert nodal_vals.shape[0] == 3
    vals = nodal_vals[0]*(1.0-3.0*x+2.0*x**2)+nodal_vals[1]*(4.0*x-4.0*x**2) +\
        nodal_vals[2]*(-x+2.0*x**2)
    return vals


def discrete_sampling(N, probs, states=None):
    r"""
    discrete_sampling -- samples iid from a discrete probability measure

    x = discrete_sampling(N, prob, states)

    Generates N iid samples from a random variable X whose probability mass
    function is

    prob(X = states[j]) = prob[j],    1 <= j <= length(prob).

    If states is not given, the states are gives by 1 <= state <= length(prob)
    """

    p = probs.squeeze()/np.sum(probs)

    bins = np.digitize(
        np.random.uniform(0., 1., (N, 1)), np.hstack((0, np.cumsum(p))))-1

    if states is None:
        x = bins
    else:
        assert(states.shape[0] == probs.shape[0])
        x = states[bins]

    return x.squeeze()


def lists_of_arrays_equal(list1, list2):
    if len(list1) != len(list2):
        return False
    for ll in range(len(list1)):
        if not np.allclose(list1[ll], list2[ll]):
            return False
    return True


def lists_of_lists_of_arrays_equal(list1, list2):
    if len(list1) != len(list2):
        return False
    for ll in range(len(list1)):
        for kk in range(len(list1[ll])):
            if not np.allclose(list1[ll][kk], list2[ll][kk]):
                return False
    return True


def beta_pdf(alpha_stat, beta_stat, x):
    # scipy implementation is slow
    const = 1./beta_fn(alpha_stat, beta_stat)
    return const*(x**(alpha_stat-1)*(1-x)**(beta_stat-1))


def pdf_under_affine_map(pdf, loc, scale, y):
    return pdf((y-loc)/scale)/scale


def beta_pdf_on_ab(alpha_stat, beta_stat, a, b, x):
    # const = 1./beta_fn(alpha_stat,beta_stat)
    # const /= (b-a)**(alpha_stat+beta_stat-1)
    # return const*((x-a)**(alpha_stat-1)*(b-x)**(beta_stat-1))
    from functools import partial
    pdf = partial(beta_pdf, alpha_stat, beta_stat)
    return pdf_under_affine_map(pdf, a, (b-a), x)


def beta_pdf_derivative(alpha_stat, beta_stat, x):
    r"""
    x in [0,1]
    """
    # beta_const = gamma_fn(alpha_stat+beta_stat)/(
    # gamma_fn(alpha_stat)*gamma_fn(beta_stat))

    beta_const = 1./beta_fn(alpha_stat, beta_stat)
    deriv = 0
    if alpha_stat > 1:
        deriv += (alpha_stat-1)*(x**(alpha_stat-2)*(1-x)**(beta_stat-1))
    if beta_stat > 1:
        deriv -= (beta_stat - 1)*(x**(alpha_stat-1)*(1-x)**(beta_stat-2))
    deriv *= beta_const
    return deriv


def gaussian_cdf(mean, var, x):
    return 0.5*(1+erf((x-mean)/(np.sqrt(var*2))))


def gaussian_pdf(mean, var, x, package=np):
    r"""
    set package=sympy if want to use for symbolic calculations
    """
    return package.exp(-(x-mean)**2/(2*var)) / (2*package.pi*var)**.5


def gaussian_pdf_derivative(mean, var, x):
    return -gaussian_pdf(mean, var, x)*(x-mean)/var


def pdf_derivative_under_affine_map(pdf_deriv, loc, scale, y):
    r"""
    Let y=g(x)=x*scale+loc and x = g^{-1}(y) = v(y) = (y-loc)/scale, scale>0
    p_Y(y)=p_X(v(y))*|dv/dy(y)|=p_X((y-loc)/scale))/scale
    dp_Y(y)/dy = dv/dy(y)*dp_X/dx(v(y))/scale = dp_X/dx(v(y))/scale**2
    """
    return pdf_deriv((y-loc)/scale)/scale**2


def gradient_of_tensor_product_function(univariate_functions,
                                        univariate_derivatives, samples):
    num_samples = samples.shape[1]
    num_vars = len(univariate_functions)
    assert len(univariate_derivatives) == num_vars
    gradient = np.empty((num_vars, num_samples))
    # precompute data which is reused multiple times
    function_values = []
    for ii in range(num_vars):
        function_values.append(univariate_functions[ii](samples[ii, :]))

    for ii in range(num_vars):
        gradient[ii, :] = univariate_derivatives[ii](samples[ii, :])
        for jj in range(ii):
            gradient[ii, :] *= function_values[jj]
        for jj in range(ii+1, num_vars):
            gradient[ii, :] *= function_values[jj]
    return gradient


def evaluate_tensor_product_function(univariate_functions, samples):
    num_samples = samples.shape[1]
    num_vars = len(univariate_functions)
    values = np.ones((num_samples))
    for ii in range(num_vars):
        values *= univariate_functions[ii](samples[ii, :])
    return values


def cholesky_decomposition(Amat):

    nrows = Amat.shape[0]
    assert Amat.shape[1] == nrows

    L = np.zeros((nrows, nrows))
    for ii in range(nrows):
        temp = Amat[ii, ii]-np.sum(L[ii, :ii]**2)
        if temp <= 0:
            raise Exception('matrix is not positive definite')
        L[ii, ii] = np.sqrt(temp)
        L[ii+1:, ii] =\
            (Amat[ii+1:, ii]-np.sum(
                L[ii+1:, :ii]*L[ii, :ii], axis=1))/L[ii, ii]

    return L


def pivoted_cholesky_decomposition(A, npivots, init_pivots=None, tol=0.,
                                   error_on_small_tol=False,
                                   pivot_weights=None,
                                   return_full=False,
                                   econ=True):
    r"""
    Return a low-rank pivoted Cholesky decomposition of matrix A.

    If A is positive definite and npivots is equal to the number of rows of A
    then L.dot(L.T)==A

    To obtain the pivoted form of L set
    L = L[pivots,:]

    Then P.T.dot(A).P == L.dot(L.T)

    where P is the standard pivot matrix which can be obtained from the
    pivot vector using the function
    """
    Amat = A.copy()
    nrows = Amat.shape[0]
    assert Amat.shape[1] == nrows
    assert npivots <= nrows

    # L = np.zeros(((nrows,npivots)))
    L = np.zeros(((nrows, nrows)))
    # diag1 = np.diag(Amat).copy() # returns a copy of diag
    diag = Amat.ravel()[::Amat.shape[0]+1]  # returns a view of diag
    # assert np.allclose(diag,diag1)
    pivots = np.arange(nrows)
    init_error = np.absolute(diag).sum()
    L, pivots, diag, chol_flag, ncompleted_pivots, error = \
        continue_pivoted_cholesky_decomposition(
            Amat, L, npivots, init_pivots, tol,
            error_on_small_tol,
            pivot_weights, pivots, diag,
            0, init_error, econ)

    if not return_full:
        return L[:, :ncompleted_pivots], pivots[:ncompleted_pivots], error,\
            chol_flag
    else:
        return L, pivots, error, chol_flag, diag.copy(), init_error, \
            ncompleted_pivots


def continue_pivoted_cholesky_decomposition(Amat, L, npivots, init_pivots, tol,
                                            error_on_small_tol,
                                            pivot_weights, pivots, diag,
                                            ncompleted_pivots, init_error,
                                            econ):
    Amat = Amat.copy()  # Do not overwrite incoming Amat
    if econ is False and pivot_weights is not None:
        msg = 'pivot weights not used when econ is False'
        raise Exception(msg)
    chol_flag = 0
    assert ncompleted_pivots < npivots
    for ii in range(ncompleted_pivots, npivots):
        if init_pivots is None or ii >= len(init_pivots):
            if econ:
                if pivot_weights is None:
                    pivot = np.argmax(diag[pivots[ii:]])+ii
                else:
                    pivot = np.argmax(
                        pivot_weights[pivots[ii:]]*diag[pivots[ii:]])+ii
            else:
                schur_complement = (
                    Amat[np.ix_(pivots[ii:], pivots[ii:])] -
                    L[pivots[ii:], :ii].dot(L[pivots[ii:], :ii].T))
                schur_diag = np.diagonal(schur_complement)
                pivot = np.argmax(
                    np.linalg.norm(schur_complement, axis=0)**2/schur_diag)
                pivot += ii
        else:
            pivot = np.where(pivots == init_pivots[ii])[0][0]
            assert pivot >= ii

        swap_rows(pivots, ii, pivot)
        if diag[pivots[ii]] <= 0:
            msg = 'matrix is not positive definite'
            if error_on_small_tol:
                raise Exception(msg)
            else:
                print(msg)
                chol_flag = 1
                break

        L[pivots[ii], ii] = np.sqrt(diag[pivots[ii]])

        L[pivots[ii+1:], ii] = (
            Amat[pivots[ii+1:], pivots[ii]] -
            L[pivots[ii+1:], :ii].dot(L[pivots[ii], :ii]))/L[pivots[ii], ii]
        diag[pivots[ii+1:]] -= L[pivots[ii+1:], ii]**2

        # for jj in range(ii+1,nrows):
        #     L[pivots[jj],ii]=(Amat[pivots[ii],pivots[jj]]-
        #         L[pivots[ii],:ii].dot(L[pivots[jj],:ii]))/L[pivots[ii],ii]
        #     diag[pivots[jj]] -= L[pivots[jj],ii]**2
        error = diag[pivots[ii+1:]].sum()/init_error
        # print(ii,'error',error)
        if error < tol:
            msg = 'Tolerance reached. '
            msg += f'Iteration:{ii}. Tol={tol}. Error={error}'
            # If matrix is rank r then then error will be machine precision
            # In such a case exiting without an error is the right thing to do
            if error_on_small_tol:
                raise Exception(msg)
            else:
                chol_flag = 1
                print(msg)
                break

    return L, pivots, diag, chol_flag, ii+1, error


def get_pivot_matrix_from_vector(pivots, nrows):
    P = np.eye(nrows)
    P = P[pivots, :]
    return P


def determinant_triangular_matrix(matrix):
    return np.prod(np.diag(matrix))


def get_all_primes_less_than_or_equal_to_n(n):
    primes = list()
    primes.append(2)
    for num in range(3, n+1, 2):
        if all(num % i != 0 for i in range(2, int(num**.5) + 1)):
            primes.append(num)
    return np.asarray(primes)


@njit(cache=True)
def get_first_n_primes(n):
    primes = list()
    primes.append(2)
    num = 3
    while len(primes) < n:
        # np.all does not work with numba
        # if np.all([num % i != 0 for i in range(2, int(num**.5) + 1)]):
        flag = True
        for i in range(2, int(num**.5) + 1):
            if (num % i == 0):
                flag = False
                break
        if flag is True:
            primes.append(num)
        num += 2
    return np.asarray(primes)


def approx_fprime(x, func, eps=np.sqrt(np.finfo(float).eps)):
    r"""Approx the gradient of a vector valued function at a single
    sample using finite_difference
    """
    assert x.shape[1] == 1
    nvars = x.shape[0]
    fprime = []
    func_at_x = func(x).squeeze()
    assert func_at_x.ndim == 1
    for ii in range(nvars):
        x_plus_eps = x.copy()
        x_plus_eps[ii] += eps
        fprime.append((func(x_plus_eps).squeeze()-func_at_x)/eps)
    return np.array(fprime)


def partial_functions_equal(func1, func2):
    if not (isinstance(func1, partial) and isinstance(func2, partial)):
        return False
    are_equal = all([getattr(func1, attr) == getattr(func2, attr)
                     for attr in ['func', 'args', 'keywords']])
    return are_equal


def get_all_sample_combinations(samples1, samples2):
    r"""
    For two sample sets of different random variables
    loop over all combinations

    samples1 vary slowest and samples2 vary fastest

    Let samples1 = [[1,2],[2,3]]
        samples2 = [[0, 0, 0],[0, 1, 2]]

    Then samples will be

    ([1, 2, 0, 0, 0])
    ([1, 2, 0, 1, 2])
    ([3, 4, 0, 0, 0])
    ([3, 4, 0, 1, 2])

    """
    import itertools
    samples = []
    for r in itertools.product(*[samples1.T, samples2.T]):
        samples.append(np.concatenate(r))
    return np.asarray(samples).T


def get_correlation_from_covariance(cov):
    r"""
    Compute the correlation matrix from a covariance matrix

    Parameters
    ----------
    cov : np.ndarray (nrows,nrows)
        The symetric covariance matrix

    Returns
    -------
    cor : np.ndarray (nrows,nrows)
        The symetric correlation matrix

    Examples
    --------
    >>> cov = np.asarray([[2,-1],[-1,2]])
    >>> get_correlation_from_covariance(cov)
    array([[ 1. , -0.5],
           [-0.5,  1. ]])
    """
    stdev_inv = 1/np.sqrt(np.diag(cov))
    cor = stdev_inv[np.newaxis, :]*cov*stdev_inv[:, np.newaxis]
    return cor


def compute_f_divergence(density1, density2, quad_rule, div_type,
                         normalize=False):
    r"""
    Compute f divergence between two densities

    .. math:: \int_\Gamma f\left(\frac{p(z)}{q(z)}\right)q(x)\,dx

    Parameters
    ----------
    density1 : callable
        The density p(z)

    density2 : callable
        The density q(z)

    normalize : boolean
        True  - normalize the densities
        False - Check that densities are normalized, i.e. integrate to 1

    quad_rule : tuple
        x,w - quadrature points and weights
        x : np.ndarray (num_vars,num_samples)
        w : np.ndarray (num_samples)

    div_type : string
        The type of f divergence (KL,TV,hellinger).
        KL - Kullback-Leibler :math:`f(t)=t\log t`
        TV - total variation  :math:`f(t)=\frac{1}{2}\lvert t-1\rvert`
        hellinger - squared Hellinger :math:`f(t)=(\sqrt(t)-1)^2`
    """
    x, w = quad_rule
    assert w.ndim == 1

    density1_vals = density1(x).squeeze()
    const1 = density1_vals.dot(w)
    density2_vals = density2(x).squeeze()
    const2 = density2_vals.dot(w)
    if normalize:
        density1_vals /= const1
        density2_vals /= const2
    else:
        tol = 1e-14
        # print(const1)
        # print(const2)
        assert np.allclose(const1, 1.0, atol=tol)
        assert np.allclose(const2, 1.0, atol=tol)
        const1, const2 = 1.0, 1.0

    # normalize densities. May be needed if density is
    # Unnormalized Bayesian Posterior
    def d1(x): return density1(x)/const1
    def d2(x): return density2(x)/const2

    if div_type == 'KL':
        # Kullback-Leibler
        def f(t): return t*np.log(t)
    elif div_type == 'TV':
        # Total variation
        def f(t): return 0.5*np.absolute(t-1)
    elif div_type == 'hellinger':
        # Squared hellinger int (p(z)**0.5-q(z)**0.5)**2 dz
        # Note some formulations use 0.5 times above integral. We do not
        # do that here
        def f(t): return (np.sqrt(t)-1)**2
    else:
        raise Exception(f'Divergence type {div_type} not supported')

    d1_vals, d2_vals = d1(x), d2(x)
    II = np.where(d2_vals > 1e-15)[0]
    ratios = np.zeros_like(d2_vals)+1e-15
    ratios[II] = d1_vals[II]/d2_vals[II]
    if not np.all(np.isfinite(ratios)):
        print(d1_vals[II], d2_vals[II])
        msg = 'Densities are not absolutely continuous. '
        msg += 'Ensure that density2(z)=0 implies density1(z)=0'
        raise Exception(msg)

    divergence_integrand = f(ratios)*d2_vals

    return divergence_integrand.dot(w)


def cholesky_solve_linear_system(L, rhs):
    r"""
    Solve LL'x = b using forwards and backwards substitution
    """
    # Use forward subsitution to solve Ly = b
    y = solve_triangular(L, rhs, lower=True)
    # Use backwards subsitution to solve L'x = y
    x = solve_triangular(L.T, y, lower=False)
    return x


def update_cholesky_factorization(L_11, A_12, A_22):
    r"""
    Update a Cholesky factorization.

    Specifically compute the Cholesky factorization of

    .. math:: A=\begin{bmatrix} A_{11} & A_{12}\\ A_{12}^T & A_{22}\end{bmatrix}

    where :math:`L_{11}` is the Cholesky factorization of :math:`A_{11}`.
    Noting that

    .. math::

      \begin{bmatrix} A_{11} & A_{12}\\ A_{12}^T & A_{22}\end{bmatrix} =
      \begin{bmatrix} L_{11} & 0\\ L_{12}^T & L_{22}\end{bmatrix}
      \begin{bmatrix} L_{11}^T & L_{12}\\ 0 & L_{22}^T\end{bmatrix}

    we can equate terms to find

    .. math::

        L_{12} = L_{11}^{-1}A_{12}, \quad
        L_{22}L_{22}^T = A_{22}-L_{12}^TL_{12}
    """
    if L_11.shape[0] == 0:
        return np.linalg.cholesky(A_22)

    nrows, ncols = A_12.shape
    assert A_22.shape == (ncols, ncols)
    assert L_11.shape == (nrows, nrows)
    L_12 = solve_triangular(L_11, A_12, lower=True)
    print(A_22 - L_12.T.dot(L_12))
    L_22 = np.linalg.cholesky(A_22 - L_12.T.dot(L_12))
    L = np.block([[L_11, np.zeros((nrows, ncols))], [L_12.T, L_22]])
    return L


def update_cholesky_factorization_inverse(L_11_inv, L_12, L_22):
    nrows, ncols = L_12.shape
    L_22_inv = np.linalg.inv(L_22)
    L_inv = np.block(
        [[L_11_inv, np.zeros((nrows, ncols))],
         [-L_22_inv.dot(L_12.T.dot(L_11_inv)), L_22_inv]])
    return L_inv


def update_trace_involving_cholesky_inverse(L_11_inv, L_12, L_22_inv, B,
                                            prev_trace):
    r"""
    Update the trace of matrix matrix product involving the inverse of a
    matrix with a cholesky factorization.

    That is compute

    .. math:: \mathrm{Trace}\leftA^{inv}B\right}

    where :math:`A=LL^T`
    """
    nrows, ncols = L_12.shape
    assert B.shape == (nrows+ncols, nrows+ncols)
    B_11 = B[:nrows, :nrows]
    B_12 = B[:nrows, nrows:]
    B_21 = B[nrows:, :nrows]
    B_22 = B[nrows:, nrows:]
    # assert np.allclose(B, np.block([[B_11, B_12],[B_21, B_22]]))

    C = -np.dot(L_22_inv.dot(L_12.T), L_11_inv)
    C_T_L_22_inv = C.T.dot(L_22_inv)
    trace = prev_trace + np.sum(C.T.dot(C)*B_11) + \
        np.sum(C_T_L_22_inv*B_12) + np.sum(C_T_L_22_inv.T*B_21) +  \
        np.sum(L_22_inv.T.dot(L_22_inv)*B_22)
    return trace


def num_entries_square_triangular_matrix(N, include_diagonal=True):
    r"""Num entries in upper (or lower) NxN traingular matrix"""
    if include_diagonal:
        return int(N*(N+1)/2)
    else:
        return int(N*(N-1)/2)


def num_entries_rectangular_triangular_matrix(M, N, upper=True):
    r"""Num entries in upper (or lower) MxN traingular matrix.
    This is useful for nested for loops like

    (upper=True)

    for ii in range(M):
        for jj in range(ii+1):

    (upper=False)

    for jj in range(N):
        for ii in range(jj+1):

    """
    assert M >= N
    if upper:
        return num_entries_square_triangular_matrix(N)
    else:
        return num_entries_square_triangular_matrix(M) -\
            num_entries_square_triangular_matrix(M-N)


def flattened_rectangular_lower_triangular_matrix_index(ii, jj, M, N):
    r"""
    Get flattened index kk from row and column indices (ii,jj) of a
    lower triangular part of MxN matrix
    """
    assert M >= N
    assert ii >= jj
    if ii == 0:
        return 0
    T = num_entries_rectangular_triangular_matrix(ii, min(ii, N), upper=False)
    kk = T+jj
    return kk


def evaluate_quadratic_form(matrix, samples):
    r"""
    Evaluate x.T.dot(A).dot(x) for several vectors x

    Parameters
    ----------
    num_samples : np.ndarray (nvars,nsamples)
        The vectors x

    matrix : np.ndarray(nvars,nvars)
        The matrix A

    Returns
    -------
    vals : np.ndarray (nsamples)
        Evaluations of the quadratic form for each vector x
    """
    return (samples.T.dot(matrix)*samples.T).sum(axis=1)


def split_dataset(samples, values, ndata1):
    """
    Split a data set into two sets.

    Parameters
    ----------
    samples : np.ndarray (nvars,nsamples)
        The samples to be split

    values : np.ndarray (nsamples,nqoi)
        Values of the data at ``samples``

    ndata1 : integer
        The number of samples allocated to the first split. All remaining
        samples will be added to the second split.

    Returns
    -------
    samples1 : np.ndarray (nvars,ndata1)
        The samples of the first split data set

    values1 : np.ndarray (nvars,ndata1)
        The values of the first split data set

    samples2 : np.ndarray (nvars,ndata1)
        The samples of the first split data set

    values2 : np.ndarray (nvars,ndata1)
        The values of the first split data set
    """
    assert ndata1 <= samples.shape[1]
    assert values.shape[0] == samples.shape[1]
    II = np.random.permutation(samples.shape[1])
    samples1 = samples[:, II[:ndata1]]
    samples2 = samples[:, II[ndata1:]]
    values1 = values[II[:ndata1], :]
    values2 = values[II[ndata1:], :]
    return samples1, samples2, values1, values2


def leave_one_out_lsq_cross_validation(basis_mat, values, alpha=0, coef=None):
    """
    let :math:`x_i` be the ith row of :math:`X` and let
    :math:`\beta=(X^\top X)^{-1}X^\top y` such that the residuals
    at the training samples satisfy

    .. math:: r_i = X\beta-y

    then the leave one out cross validation errors are given by

    .. math:: e_i = \frac{r_i}{1-h_i}

    where

    :math:`h_i = x_i^\top(X^\top X)^{-1}x_i`
    """
    assert values.ndim == 2
    assert basis_mat.shape[0] > basis_mat.shape[1]+2
    gram_mat = basis_mat.T.dot(basis_mat)
    gram_mat += alpha*np.eye(gram_mat.shape[0])
    H_mat = basis_mat.dot(np.linalg.inv(gram_mat).dot(basis_mat.T))
    H_diag = np.diag(H_mat)
    if coef is None:
        coef = np.linalg.lstsq(
            gram_mat, basis_mat.T.dot(values), rcond=None)[0]
    assert coef.ndim == 2
    residuals = basis_mat.dot(coef) - values
    cv_errors = residuals / (1-H_diag[:, None])
    cv_score = np.sqrt(np.sum(cv_errors**2, axis=0)/basis_mat.shape[0])
    return cv_errors, cv_score, coef


def leave_many_out_lsq_cross_validation(basis_mat, values, fold_sample_indices,
                                        alpha=0, coef=None):
    nfolds = len(fold_sample_indices)
    nsamples = basis_mat.shape[0]
    cv_errors = []
    cv_score = 0
    gram_mat = basis_mat.T.dot(basis_mat)
    gram_mat += alpha*np.eye(gram_mat.shape[0])
    if coef is None:
        coef = np.linalg.lstsq(
            gram_mat, basis_mat.T.dot(values), rcond=None)[0]
    residuals = basis_mat.dot(coef) - values
    gram_mat_inv = np.linalg.inv(gram_mat)
    for kk in range(nfolds):
        indices_kk = fold_sample_indices[kk]
        nvalidation_samples_kk = indices_kk.shape[0]
        assert nsamples - nvalidation_samples_kk >= basis_mat.shape[1]
        basis_mat_kk = basis_mat[indices_kk, :]
        residuals_kk = residuals[indices_kk, :]

        H_mat = np.eye(nvalidation_samples_kk) - basis_mat_kk.dot(
            gram_mat_inv.dot(basis_mat_kk.T))
        # print('gram_mat cond number', np.linalg.cond(gram_mat))
        # print('H_mat cond number', np.linalg.cond(H_mat))
        H_mat_inv = np.linalg.inv(H_mat)
        cv_errors.append(H_mat_inv.dot(residuals_kk))
        cv_score += np.sum(cv_errors[-1]**2, axis=0)
    return np.asarray(cv_errors), np.sqrt(cv_score/basis_mat.shape[0]), coef


def get_random_k_fold_sample_indices(nsamples, nfolds, random=True):
    sample_indices = np.arange(nsamples)
    if random is True:
        sample_indices = np.random.permutation(sample_indices)
    fold_sample_indices = [np.empty(0, dtype=int) for kk in range(nfolds)]
    nn = 0
    while nn < nsamples:
        for jj in range(nfolds):
            fold_sample_indices[jj] = np.append(
                fold_sample_indices[jj], sample_indices[nn])
            nn += 1
            if nn >= nsamples:
                break
    assert np.unique(np.hstack(fold_sample_indices)).shape[0] == nsamples
    return fold_sample_indices


def get_cross_validation_rsquared_coefficient_of_variation(
        cv_score, train_vals):
    r"""
    cv_score = :math:`N^{-1/2}\left(\sum_{n=1}^N e_n\right^{1/2}` where
    :math:`e_n` are the cross  validation residues at each test point and
    :math:`N` is the number of traing vals

    We define r_sq as

    .. math:: 1-\frac{N^{-1}\left(\sum_{n=1}^N e_n\right)}/mathbb{V}\left[Y\right] where Y is the vector of training vals
    """
    # total sum of squares (proportional to variance)
    denom = np.std(train_vals)
    # the factors of 1/N in numerator and denominator cancel out
    rsq = 1-(cv_score/denom)**2
    return rsq


def __integrate_using_univariate_gauss_legendre_quadrature_bounded(
        integrand, lb, ub, nquad_samples, rtol=1e-8, atol=1e-8,
        verbose=0, adaptive=True, tabulated_quad_rules=None):
    """
    tabulated_quad_rules : dictionary
        each entry is a tuple (x,w) of gauss legendre with weight
        function p(x)=1 defined on [-1,1]. The number of points in x is
        defined by the key.
        User must ensure that the dictionary contains any nquad_samples
        that may be requested
    """
    # Adaptive
    # nquad_samples = 10
    prev_res = np.inf
    it = 0
    while True:
        if (tabulated_quad_rules is None or
                nquad_samples not in tabulated_quad_rules):
            xx_canonical, ww_canonical = leggauss(nquad_samples)
        else:
            xx_canonical, ww_canonical = tabulated_quad_rules[nquad_samples]
        xx = (xx_canonical+1)/2*(ub-lb)+lb
        ww = ww_canonical*(ub-lb)/2
        res = integrand(xx).T.dot(ww).T
        diff = np.absolute(prev_res-res)
        if verbose > 1:
            print(it, nquad_samples, diff)
        if (np.all(np.absolute(prev_res-res) < rtol*np.absolute(res)+atol) or
                adaptive is False):
            break
        prev_res = res
        nquad_samples *= 2
        it += 1
    if verbose > 0:
        print(f'adaptive quadrature converged in {it} iterations')
    return res


def integrate_using_univariate_gauss_legendre_quadrature_unbounded(
        integrand, lb, ub, nquad_samples, atol=1e-8, rtol=1e-8,
        interval_size=2, max_steps=1000, verbose=0, adaptive=True,
        soft_error=False, tabulated_quad_rules=None):
    """
    Compute unbounded integrals by moving left and right from origin.
    Assume that integral decays towards +/- infinity. And that once integral
    over a sub interval drops below tolerance it will not increase again if
    we keep moving in same direction.
    """
    if interval_size <= 0:
        raise ValueError("Interval size must be positive")

    if np.isfinite(lb) and np.isfinite(ub):
        partial_lb, partial_ub = lb, ub
    elif np.isfinite(lb) and not np.isfinite(ub):
        partial_lb, partial_ub = lb, lb+interval_size
    elif not np.isfinite(lb) and np.isfinite(ub):
        partial_lb, partial_ub = ub-interval_size, ub
    else:
        partial_lb, partial_ub = -interval_size/2, interval_size/2

    result = __integrate_using_univariate_gauss_legendre_quadrature_bounded(
        integrand, partial_lb, partial_ub, nquad_samples, rtol,
        atol, verbose-1, adaptive, tabulated_quad_rules)

    step = 0
    partial_result = np.inf
    plb, pub = partial_lb-interval_size, partial_lb
    while (np.any(np.absolute(partial_result) >= rtol*np.absolute(result)+atol)
           and (plb >= lb) and step < max_steps):
        partial_result = \
            __integrate_using_univariate_gauss_legendre_quadrature_bounded(
                integrand, plb, pub, nquad_samples, rtol, atol,
                verbose-1, adaptive, tabulated_quad_rules)
        result += partial_result
        pub = plb
        plb -= interval_size
        step += 1
        if verbose > 1:
            print('Left', step, result, partial_result, plb, pub,
                  interval_size)
        if verbose > 0:
            if step >= max_steps:
                msg = "Early termination when computing left integral"
                msg += f"max_steps {max_steps} reached"
                if soft_error is True:
                    warn(msg, UserWarning)
                else:
                    raise RuntimeError(msg)
            if np.all(np.abs(partial_result) < rtol*np.absolute(result)+atol):
                msg = f'Tolerance {atol} {rtol} for left integral reached in '
                msg += f'{step} iterations'
                print(msg)

    step = 0
    partial_result = np.inf
    plb, pub = partial_ub, partial_ub+interval_size
    while (np.any(np.absolute(partial_result) >= rtol*np.absolute(result)+atol)
           and (pub <= ub) and step < max_steps):
        partial_result = \
            __integrate_using_univariate_gauss_legendre_quadrature_bounded(
                integrand, plb, pub, nquad_samples, rtol, atol,
                verbose-1, adaptive, tabulated_quad_rules)
        result += partial_result
        plb = pub
        pub += interval_size
        step += 1
        if verbose > 1:
            print('Right', step, result, partial_result, plb, pub,
                  interval_size)
        if verbose > 0:
            if step >= max_steps:
                msg = "Early termination when computing right integral. "
                msg += f"max_steps {max_steps} reached"
                if soft_error is True:
                    warn(msg, UserWarning)
                else:
                    raise RuntimeError(msg)
            if np.all(np.abs(partial_result) < rtol*np.absolute(result)+atol):
                msg = f'Tolerance {atol} {rtol} for right integral reached in '
                msg += f'{step} iterations'
                print(msg)
        # print(partial_result, plb, pub)

    return result


def qr_solve(Q, R, rhs):
    """
    Find the least squares solution Ax = rhs given a QR factorization of the
    matrix A

    Parameters
    ----------
    Q : np.ndarray (nrows, nrows)
        The unitary/upper triangular Q factor

    R : np.ndarray (nrows, ncols)
        The upper triangular R matrix

    rhs : np.ndarray (nrows, nqoi)
        The right hand side vectors

    Returns
    -------
    x : np.ndarray (nrows, nqoi)
        The solution
    """
    tmp = np.dot(Q.T, rhs)
    return solve_triangular(R, tmp, lower=False)


def equality_constrained_linear_least_squares(A, B, y, z):
    """
    Solve equality constrained least squares regression

    minimize || y - A*x ||_2   subject to   B*x = z

    It is assumed that

    Parameters
    ----------
    A : np.ndarray (M, N)
        P <= N <= M+P, and

    B : np.ndarray (N, P)
        P <= N <= M+P, and

    y : np.ndarray (M, 1)
        P <= N <= M+P, and

    z : np.ndarray (P, 1)
        P <= N <= M+P, and

    Returns
    -------
    x : np.ndarray (N, 1)
        The solution
    """
    return lapack.dgglse(A, B, y, z)[3]


def piecewise_univariate_linear_quad_rule(range_1d, npoints):
    """
    Compute the points and weights of a piecewise-linear quadrature
    rule that can be used to compute a definite integral

    Parameters
    ----------
    range_1d : iterable (2)
       The lower and upper bound of the definite integral

    Returns
    -------
    xx : np.ndarray (npoints)
        The points of the quadrature rule

    ww : np.ndarray (npoints)
        The weights of the quadrature rule
    """
    xx = np.linspace(range_1d[0], range_1d[1], npoints)
    ww = np.ones((npoints))/(npoints-1)*(range_1d[1]-range_1d[0])
    ww[0] *= 0.5
    ww[-1] *= 0.5
    return xx, ww


def piecewise_univariate_quadratic_quad_rule(range_1d, npoints):
    """
    Compute the points and weights of a piecewise-quadratic quadrature
    rule that can be used to compute a definite integral

    Parameters
    ----------
    range_1d : iterable (2)
       The lower and upper bound of the definite integral

    Returns
    -------
    xx : np.ndarray (npoints)
        The points of the quadrature rule

    ww : np.ndarray (npoints)
        The weights of the quadrature rule
    """
    xx = np.linspace(range_1d[0], range_1d[1], npoints)
    dx = 4/(3*(npoints-1))
    ww = dx*np.ones((npoints))*(range_1d[1]-range_1d[0])
    ww[0::2] *= 0.5
    ww[0] *= 0.5
    ww[-1] *= 0.5
    return xx, ww


def get_tensor_product_piecewise_polynomial_quadrature_rule(
        nsamples_1d, ranges, degree=1):
    """
    Compute the nodes and weights needed to integrate a 2D function using
    piecewise linear interpolation
    """
    nrandom_vars = len(ranges)//2
    if isinstance(nsamples_1d, int):
        nsamples_1d = np.array([nsamples_1d]*nrandom_vars)
    assert nrandom_vars == len(nsamples_1d)

    if degree == 1:
        piecewise_univariate_quad_rule = piecewise_univariate_linear_quad_rule
    elif degree == 2:
        piecewise_univariate_quad_rule = \
            piecewise_univariate_quadratic_quad_rule
    else:
        raise ValueError("degree must be 1 or 2")

    univariate_quad_rules = [
        partial(piecewise_univariate_quad_rule, ranges[2*ii:2*ii+2])
        for ii in range(nrandom_vars)]
    x_quad, w_quad = get_tensor_product_quadrature_rule(
        nsamples_1d, nrandom_vars,
        univariate_quad_rules)

    return x_quad, w_quad


def extract_sub_list(mylist, indices):
    """
    Extract a subset of items from a list

    Parameters
    ----------
    mylist : list(nitems)
        The list containing all items

    indices : iterable (nindices)
        The indices of the desired items

    Returns
    -------
    subset :  list (nindices)
        The extracted items
    """
    return [mylist[ii] for ii in indices]


def unique_elements_from_2D_list(list_2d):
    """
    Extract the unique elements from a list of lists

    Parameters
    ----------
    list_2d : list(list)
        The list of lists

    Returns
    -------
    unique_items :  list (nunique_items)
        The unique items
    """
    return list(set(flatten_2D_list(list_2d)))


def flatten_2D_list(list_2d):
    """
    Flatten a list of lists into a single list

    Parameters
    ----------
    list_2d : list(list)
        The list of lists

    Returns
    -------
    flattened_list :  list (nitems)
        The unique items
    """
    return [item for sub in list_2d for item in sub]


@njit(cache=True)
def piecewise_quadratic_basis(level, xx):
    """
    Evaluate each piecewise quadratic basis on a dydatic grid of a specified
    level.

    Parameters
    ----------
    level : integer
        The level of the dydadic grid. The number of points in the grid is
        nbasis=2**level+1, except at level 0 when nbasis=1

    xx : np.ndarary (nsamples)
        The samples at which to evaluate the basis functions

    Returns
    -------
    vals : np.ndarary (nsamples, nbasis)
        Evaluations of each basis function
    """
    assert level > 0
    h = 1/float(1 << level)
    N = (1 << level)+1
    vals = np.zeros((xx.shape[0], N))

    for ii in range(N):
        xl = (ii-1.0)*h
        xr = xl+2.0*h
        if ii % 2 == 1:
            vals[:, ii] = np.maximum(-(xx-xl)*(xx-xr)/(h*h), 0.0)
            continue
        II = np.where((xx > xl-h) & (xx < xl+h))[0]
        xx_II = xx[II]
        vals[II, ii] = (xx_II**2-h*xx_II*(2*ii-3)+h*h*(ii-1)*(ii-2))/(2.*h*h)
        JJ = np.where((xx >= xl+h) & (xx < xr+h))[0]
        xx_JJ = xx[JJ]
        vals[JJ, ii] = (xx_JJ**2-h*xx_JJ*(2*ii+3)+h*h*(ii+1)*(ii+2))/(2.*h*h)
    return vals


def piecewise_linear_basis(level, xx):
    """
    Evaluate each piecewise linear basis on a dydatic grid of a specified
    level.

    Parameters
    ----------
    level : integer
        The level of the dydadic grid. The number of points in the grid is
        nbasis=2**level+1, except at level 0 when nbasis=1

    xx : np.ndarary (nsamples)
        The samples at which to evaluate the basis functions

    Returns
    -------
    vals : np.ndarary (nsamples, nbasis)
        Evaluations of each basis function
    """
    assert level > 0
    N = (1 << level)+1
    vals = np.maximum(
        0, 1-np.absolute((1 << level)*xx[:, None]-np.arange(N)[None, :]))
    return vals


def nsamples_dydactic_grid_1d(level):
    """
    The number of points in a dydactic grid.

    Parameters
    ----------
    level : integer
        The level of the dydadic grid.

    Returns
    -------
    nsamples : integer
        The number of points in the grid
    """
    if level == 0:
        return 1
    return (1 << level)+1


def dydactic_grid_1d(level):
    """
    The points in a dydactic grid.

    Parameters
    ----------
    level : integer
        The level of the dydadic grid.

    Returns
    -------
    samples : np.ndarray(nbasis)
        The points in the grid
    """
    if level == 0:
        return np.array([0.5])
    return np.linspace(0, 1, nsamples_dydactic_grid_1d(level))


def tensor_product_piecewise_polynomial_basis(
        levels, samples, basis_type="linear"):
    """
    Evaluate each piecewise polynomial basis on a tensor product dydactic grid

    Parameters
    ----------
    levels : array_like (nvars)
        The levels of each 1D dydadic grid.

    samples : np.ndarray (nvars, nsamples)
        The samples at which to evaluate the basis functions

    basis_type : string
        The type of piecewise polynomial basis, i.e. 'linear' or 'quadratic'

    Returns
    -------
    basis_vals : np.ndarray(nsamples, nbasis)
        Evaluations of each basis function
    """
    nvars = samples.shape[0]
    levels = np.asarray(levels)
    if len(levels) != nvars:
        raise ValueError("levels and samples are inconsistent")

    basis_fun = {"linear": piecewise_linear_basis,
                 "quadratic": piecewise_quadratic_basis}[basis_type]

    active_vars = np.arange(nvars)[levels > 0]
    nactive_vars = active_vars.shape[0]
    nsamples = samples.shape[1]
    N_active = [nsamples_dydactic_grid_1d(ll) for ll in levels[active_vars]]
    N_max = np.max(N_active)
    basis_vals_1d = np.empty((nactive_vars, N_max, nsamples),
                             dtype=np.float64)
    for dd in range(nactive_vars):
        idx = active_vars[dd]
        basis_vals_1d[dd, :N_active[dd], :] = basis_fun(
            levels[idx], samples[idx, :]).T
    temp1 = basis_vals_1d.reshape(
        (nactive_vars*basis_vals_1d.shape[1], nsamples))
    indices = cartesian_product([np.arange(N) for N in N_active])
    nindices = indices.shape[1]
    temp2 = temp1[indices.ravel()+np.repeat(
        np.arange(nactive_vars)*basis_vals_1d.shape[1], nindices), :].reshape(
            nactive_vars, nindices, nsamples)
    basis_vals = np.prod(temp2, axis=0).T
    return basis_vals


def tensor_product_piecewise_polynomial_interpolation_with_values(
        samples, fn_vals, levels, basis_type="linear"):
    """
    Use a piecewise polynomial basis to interpolate a function from values
    defined on a tensor product dydactic grid.

    Parameters
    ----------
    samples : np.ndarray (nvars, nsamples)
        The samples at which to evaluate the basis functions

    fn_vals : np.ndarary (nbasis, nqoi)
        The values of the function on the dydactic grid

    levels : array_like (nvars)
        The levels of each 1D dydadic grid.

    basis_type : string
        The type of piecewise polynomial basis, i.e. 'linear' or 'quadratic'

    Returns
    -------
    basis_vals : np.ndarray(nsamples, nqoi)
        Evaluations of the interpolant at the samples
    """
    basis_vals = tensor_product_piecewise_polynomial_basis(
        levels, samples, basis_type)
    if fn_vals.shape[0] != basis_vals.shape[1]:
        raise ValueError("The size of fn_vals is inconsistent with levels")
    return basis_vals.dot(fn_vals)


def tensor_product_piecewise_polynomial_interpolation(
        samples, levels, fun, basis_type="linear"):
    """
    Use tensor-product piecewise polynomial basis to interpolate a function.

    Parameters
    ----------
    samples : np.ndarray (nvars, nsamples)
        The samples at which to evaluate the basis functions

    levels : array_like (nvars)
        The levels of each 1D dydadic grid.

    fun : callable
        Function with the signature

        `fun(samples) -> np.ndarray (nx, nqoi)`

        where samples is np.ndarray (nvars, nx)

    basis_type : string
        The type of piecewise polynomial basis, i.e. 'linear' or 'quadratic'

    Returns
    -------
    basis_vals : np.ndarray(nsamples, nqoi)
        Evaluations of the interpolant at the samples
    """
    samples_1d = [dydactic_grid_1d(ll) for ll in levels]
    grid_samples = cartesian_product(samples_1d)
    fn_vals = fun(grid_samples)
    return tensor_product_piecewise_polynomial_interpolation_with_values(
        samples, fn_vals, levels, basis_type)
