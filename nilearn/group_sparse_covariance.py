"""
Implementation of algorithm for sparse multi-task learning of gaussian
graphical models.
"""
# Authors: Philippe Gervais
# License: simplified BSD

import warnings

import numpy as np
import scipy
import scipy.optimize

from sklearn.utils.extmath import fast_logdet
from sklearn.covariance import empirical_covariance

from .testing import is_spd


def symmetrize(M):
    M[...] = M + M.T
    M[...] /= 2.


def _group_sparse_covariance_cost(n_tasks, n_samples, rho, omega,
                                 emp_covs, display=False):
    """Compute group sparse covariance cost during computation."""
    ll = 0  # log-likelihood
    for k in xrange(n_tasks):
        t = fast_logdet(omega[..., k])
        t -= (omega[..., k] * emp_covs[..., k]).sum()
        ll += n_samples[k] * t

    # L(1,2)-norm
    l2 = np.sqrt((omega ** 2).sum(axis=-1))
    # Do not count diagonal terms
    l12 = l2.sum() - np.diag(l2).sum()
    cost = - (ll - rho * l12)
    if display:
        print("Cost: {cost:.8f}".format(
                cost=cost))
    return cost


# The signature of quad_trust_region and quad_trust_region_deriv are
# complicated, but this allows for some interesting optimizations.
def quad_trust_region(alpha, q, two_ccq, cc, rho2):
    """This value is optimized to zero by the Newton-Raphson step."""
    return rho2 - (cc / ((1. + alpha * q) ** 2)).sum()


def quad_trust_region_deriv(alpha, q, two_ccq, cc, rho2):
    """Derivative of quad_trust_region."""
    return (two_ccq / (1. + alpha * q) ** 3).sum()


def update_vectors(full, n):
    """full is a (N, N) matrix.

    This function is a helper function for updating the submatrix equals to
    "full" with row n + 1 and column n + 1 removed. The initial state of the
    submatrix is supposed to be "full" with row and column n removed.

    This functions returns the new value of row and column n in the submatrix.
    Thus, if h, v are the return values of this function, the submatrix must
    be updated this way: sub[n, :] = h ; sub[:, n] = v
    """
    v = np.ndarray((full.shape[0] - 1,), dtype=full.dtype)
    v[:n + 1] = full[:n + 1, n]
    v[n + 1:] = full[n + 2:, n]

    h = np.ndarray((full.shape[1] - 1,), dtype=full.dtype)
    h[:n + 1] = full[n, :n + 1]
    h[n + 1:] = full[n, n + 2:]

    return h, v


def update_submatrix(full, sub, sub_inv, p):
    """Update submatrix and its inverse.

    sub_inv is the inverse of the submatrix of "full" obtained by removing
    the p-th row and column.

    sub_inv is modified in-place. After execution of this function, it contains
    the inverse of the submatrix of "full" obtained by removing the n+1-th row
    and column.

    This computation is based on Sherman-Woodbury-Morrison identity.
    """

    n = p - 1
    h, v = update_vectors(full, n)

    # change row
    coln = sub_inv[:, n]
    V = h - sub[n, :]
    coln = coln / (1. + np.dot(V, coln))
    sub_inv -= np.outer(coln, np.dot(V, sub_inv))
    sub[n, :] = h

    # change column
    rown = sub_inv[n, :]
    U = v - sub[:, n]
    rown = rown / (1. + np.dot(rown, U))
    sub_inv -= np.outer(np.dot(sub_inv, U), rown)
    sub[:, n] = v   # equivalent to sub[n, :] += U


def assert_submatrix(full, sub, n):
    """Check that "sub" is the matrix obtained by removing the p-th col and row
    in "full". Used only for debugging.
    """
    true_sub = np.ndarray(shape=sub.shape, dtype=sub.dtype)
    true_sub[:n, :n] = full[:n, :n]
    true_sub[n:, n:] = full[n + 1:, n + 1:]
    true_sub[:n, n:] = full[:n, n + 1:]
    true_sub[n:, :n] = full[n + 1:, :n]

    np.testing.assert_almost_equal(true_sub, sub)


def group_sparse_covariance(signals, rho, n_iter=10,
                            assume_centered=False, verbose=0,
                            dtype=np.float64,
                            return_costs=False, debug=False):
    """Compute sparse precision matrices starting with covariance matrices.

    The precision matrices returned by this function are sparse, and share a
    common sparsity pattern: all have zeros at the same location. This is
    achieved by simultaneous computation of all precision matrices at the
    same time.

    Running time is linear on n_iter, and number of tasks (number of covariance
    matrices), but cubic on covariance size (number of signals).

    Parameters
    ==========
    signals: list of numpy.ndarray
        input tasks. Each task is a 2D array, whose columns contain signals.
        Each array shape must be (sample number, feature number). The sample
        number can vary from task to task, but all tasks must have the same
        number of features (i.e. of columns).

    rho: float
        regularization parameter. With normalized covariances matrices and
        number of samples, sensible values lie in the [0, 1] range(zero is
        no regularization: output is not sparse)

    n_iter: int
        number of iteration. The default value (10) is rather conservative.

    assume_centered: bool
        if True, assume that all input signals are centered. This slightly
        decreases computation time by avoiding useless computation.

    verbose: int
        verbosity level. Zero means "no message".

    dtype: numpy dtype
        type of returned matrices. Defaults to 8-byte floats (double).

    return_costs: bool
        if True, return the value taken by the cost function for each
        iteration in addition to the matrices. Default: False.

    debug: bool
        if True, perform checks during computation. It can help find
        numerical problems, but increases computation time a lot.

    Returns
    =======
    emp_covs: numpy.ndarray
        empirical covariance matrices (output of
        sklearn.covariance.empirical_covariance)

    omega: numpy.ndarray
        estimated precision matrices

    costs: numpy.ndarray
        value of cost function for each iteration. This is output only if
        return_costs is True.

    Notes
    =====
    The present algorithm is based on:

    Jean Honorio and Dimitris Samaras.
    "Simultaneous and Group-Sparse Multi-Task Learning of Gaussian Graphical
    Models". arXiv:1207.4255 (17 July 2012). http://arxiv.org/abs/1207.4255.
    """
    if not isinstance(rho, (int, float)) or rho < 0:
        raise ValueError("Regularization parameter rho must be a "
                         "positive number.\n"
                         "You provided: {0}".format(str(rho)))

    if not hasattr(signals, "__iter__"):
        raise ValueError("'signals' input argument must be an iterable. "
                         "You provided {0}".format(signals.__class__))

    n_tasks = [s.shape[1] for s in signals]
    if len(set(n_tasks)) > 1:
        raise ValueError("All tasks must have the same number of features.\n"
                         "You provided: {0}".format(str(n_tasks)))
    n_tasks = len(signals)
    n_var = signals[0].shape[1]

    n_samples = np.asarray([s.shape[0] for s in signals], dtype=np.double)
    n_samples /= n_samples.max()

    emp_covs = np.empty((n_var, n_var, n_tasks), dtype=dtype)
    for k, s in enumerate(signals):
        emp_covs[..., k] = empirical_covariance(
            s, assume_centered=assume_centered)
        symmetrize(emp_covs[..., k])
        if debug:
            assert(is_spd(emp_covs[..., k]))
    del signals  # reduce memory usage in some cases.

    omega = np.ndarray(shape=emp_covs.shape, dtype=emp_covs.dtype)
    for k in xrange(n_tasks):
        # Values on main diagonals should be far from zero, because they
        # are timeseries energy.
        omega[..., k] = np.diag(1. / np.diag(emp_covs[..., k]))

    # Preallocate arrays
    y = np.ndarray(shape=(n_tasks, n_var - 1), dtype=np.float)
    u = np.ndarray(shape=(n_tasks, n_var - 1))
    y_1 = np.ndarray(shape=(n_tasks, n_var - 2))
    h_12 = np.ndarray(shape=(n_tasks, n_var - 2))
    q = np.ndarray(shape=(n_tasks,))
    c = np.ndarray(shape=(n_tasks,))
    W = np.ndarray(shape=(omega.shape[0] - 1, omega.shape[1] - 1,
                          omega.shape[2]),
                   dtype=np.float)
    Winv = np.ndarray(shape=W.shape, dtype=np.float)

    # Optional.
    costs = []

    # Start optimization loop. Variables are named following the
    # Honorio-Samaras paper notations.
    for n in xrange(n_iter):
        if verbose >= 1:
            print("\n-- Starting {iter_n:d}-th iteration...".format(iter_n=n))

        for p in xrange(n_var):

            if p == 0:
                # Initial state: remove first col/row
                W = omega[1:, 1:, :].copy()   # stack of W(k)
                Winv = np.ndarray(shape=W.shape, dtype=np.float)
                for k in xrange(W.shape[2]):
                    # stack of W^-1(k)
                    Winv[..., k] = np.linalg.inv(W[..., k])
                    if debug:
                        np.testing.assert_almost_equal(
                            np.dot(Winv[..., k], W[..., k]),
                            np.eye(Winv[..., k].shape[0]), decimal=12)
                        assert_submatrix(omega[..., k], W[..., k], p)
            else:
                # Update W and Winv
                if debug:
                    omega_orig = omega.copy()

                for k in xrange(n_tasks):
                    update_submatrix(omega[..., k],
                                     W[..., k], Winv[..., k], p)
                    if debug:
                        assert_submatrix(omega[..., k], W[..., k], p)
                        np.testing.assert_almost_equal(
                            np.dot(Winv[..., k], W[..., k]),
                            np.eye(Winv[..., k].shape[0]), decimal=12)
                        assert(is_spd(W[..., k]))
                        assert(is_spd(Winv[..., k], decimal=14))
                if debug:
                    np.testing.assert_almost_equal(omega_orig, omega)

            # In the following lines, implicit loop on k (tasks)
            # Extract y and u
            y[:, :p] = omega[:p, p, :].T
            y[:, p:] = omega[p + 1:, p, :].T

            u[:, :p] = emp_covs[:p, p, :].T
            u[:, p:] = emp_covs[p + 1:, p, :].T

            if verbose >= 2:
                print("\n-- entering coordinate descent loop (%d)" % p)

            for m in xrange(n_var - 1):
                # Coordinate descent on y

                # T(k) -> n_samples[k]
                # v(k) -> emp_covs[p, p, k]
                # h_22(k) -> Winv[m, m, k]
                # h_12(k) -> Winv[:m, m, k],  Winv[m+1:, m, k]
                # y_1(k) -> y[k, :m], y[k, m+1:]
                # u_2(k) -> u[k, m]
                h_12[:, :m] = Winv[:m, m, :].T
                h_12[:, m:] = Winv[m + 1:, m, :].T
                y_1[:, :m] = y[:, :m]
                y_1[:, m:] = y[:, m + 1:]

                c[:] = - n_samples * (
                    emp_covs[p, p, :] * (h_12 * y_1).sum(axis=1) + u[:, m]
                    )
                c2 = np.sqrt(np.dot(c, c))

                # x -> y[:][m]
                if c2 <= rho:
                    y[:, m] = 0  # x* = 0
                else:
                    # q(k) -> T(k) * v(k) * h_22(k)
                    # \lambda -> alpha   (lambda is a Python keyword)
                    q = n_samples * emp_covs[p, p, :] * Winv[m, m, :]
                    if debug:
                        assert(np.all(q > 0))
                    # x* = \lambda* diag(1 + \lambda q)^{-1} c
                    # Precompute some quantities
                    cc = c * c
                    two_ccq = 2. * cc * q
                    # tolerance does not seem to be important for
                    # numerical stability (tol=1e-2 works)
                    alpha = scipy.optimize.newton(
                        quad_trust_region, 0,
                        fprime=quad_trust_region_deriv,
                        args=(q, two_ccq, cc, rho ** 2),
                        maxiter=50)

                    remainder = quad_trust_region(
                        alpha, q, two_ccq, cc, rho ** 2)

                    if abs(remainder) > 0.1:
                        warnings.warn("Newton-Raphson step did not converge.\n"
                                      "This indicates a badly conditioned "
                                      "system.")

                    if debug:
                        assert alpha >= 0, alpha
                    y[:, m] = (alpha * c) / (1. + alpha * q)  # x*

            # Copy back y in omega (column and row)
            omega[:p, p, :] = y[:, :p].T
            omega[p + 1:, p, :] = y[:, p:].T
            omega[p, :p, :] = y[:, :p].T
            omega[p, p + 1:, :] = y[:, p:].T

            for k in xrange(n_tasks):
                omega[p, p, k] = 1. / emp_covs[p, p, k] + np.dot(
                    np.dot(y[k, :], Winv[..., k]), y[k, :])

                if debug:
                    assert(is_spd(omega[..., k]))

            if return_costs:
                cost = _group_sparse_covariance_cost(n_tasks, n_samples, rho,
                                                     omega, emp_covs,
                                                     display=verbose >= 1)
                costs.append(cost)

    if return_costs:
        return emp_covs, omega, np.asarray(costs)
    else:
        return emp_covs, omega
