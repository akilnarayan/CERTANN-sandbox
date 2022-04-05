r"""
Push Forward Based Inference
============================
This tutorial describes push forward based inference (PFI) [BJWSISC2018]_.

PFI solves the inverse problem of inferring parameters :math:`\rv` of a deterministic model :math:`f(\rv)` from stochastic observational data on quantities of interest.  The solution is a posterior probability measure that when propagated through the deterministic model produces a push-forward measure that exactly matches a given observed probability measure on available data.  

The solution to the PFI inverse problem is given by

.. math:: \pi_\text{post}(\rv)=\pi_\text{pr}(\rv)\frac{\pi_\text{obs}(f(\rv))}{\pi_\text{model}(f(\rv))}

where :math:`\pi_\text{pr}(\rv)` is a prior density which captures any initial knowledge, :math:`\pi_\text{obs}(f(\rv))` is the density on the observations, and :math:`\pi_\text{model}(f(\rv))` is the push-forward of the prior density trough the forward model.
"""

import pyapprox as pya
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
from scipy.stats import uniform, norm, gaussian_kde as kde

#%%
#First we must define the forward model. We will use a functional of solution to the following system of non-linear equations
#
#.. math::
#
#  \rv_1 x_1^2+x_2^2&=1\\
#  x_1^2-\rv_2x_2^2&=1
#
#Specifically we choose :math:`f(\rv)=x_2(\rv)`.


class TimsModel(object):
    def __init__(self):
        self.qoi = 1
        self.ranges = np.array(
            [0.79, 0.99, 1-4.5*np.sqrt(0.1), 1+4.5*np.sqrt(0.1)],
            np.double)

    def num_qoi(self):
        if np.isscalar(self.qoi):
            return 1
        else:
            return len(self.qoi)

    def evaluate(self, samples):
        assert samples.ndim == 1
        assert samples.ndim == 1

        sol = np.ones((2), float)

        x1 = samples[0]
        x2 = samples[1]
        u1 = sol[0]
        u2 = sol[1]

        res1 = 1.-(x1*u1*u1+u2*u2)
        res2 = 1.-(u1*u1-x2*u2*u2)

        norm_res = np.sqrt(res1*res1 + res2*res2)

        it = 0
        max_iters = 20
        while (norm_res > 1e-10) and (it < max_iters):
            det = -4*u1*u2*(x1*x2+1.0)
            j11i = -2.0*x2*u2 / det
            j12i = -2.0*u2 / det
            j21i = -2.0*u1 / det
            j22i = 2*x1*u1 / det

            du1 = j11i*res1 + j12i*res2
            du2 = j21i*res1 + j22i*res2

            u1 += du1
            u2 += du2

            res1 = 1.-(x1*u1*u1+u2*u2)
            res2 = 1.-(u1*u1-x2*u2*u2)

            norm_res = np.sqrt(res1*res1 + res2*res2)
            it += 1

        sol[0] = u1
        sol[1] = u2

        if np.isscalar(self.qoi):
            values = np.array([sol[self.qoi]])
        else:
            values = sol[self.qoi]
        return values

    def __call__(self, samples):
        num_samples = samples.shape[1]
        values = np.empty((num_samples, self.num_qoi()), float)
        for i in range(samples.shape[1]):
            values[i, :] = self.evaluate(samples[:, i])
        return values


model = TimsModel()
model.qoi = np.array([1])

#%%
#Define the prior density and the observational density

univariate_variables = [
    uniform(lb, ub-lb)
    for lb, ub in zip(model.ranges[::2], model.ranges[1::2])]
prior_variable = pya.IndependentRandomVariable(
    univariate_variables)


def prior_pdf(x): return np.prod(prior_variable.evaluate('pdf', x), axis=0)


mean = 0.3
variance = 0.025**2
obs_variable = norm(mean, np.sqrt(variance))
def obs_pdf(y): return obs_variable.pdf(y).squeeze()

#%%
#PFI requires the push forward of the prior :math:`\pi_\text{model}(f(\rv))`. Lets approximate this PDF using a Gaussian kernel density estimate built on a large number model outputs evaluated at random samples of the prior.


#Define samples used to evaluate the push forward of the prior
num_prior_samples = 10000
prior_samples = pya.generate_independent_random_samples(
    prior_variable, num_prior_samples)
response_vals_at_prior_samples = model(prior_samples)

#Construct a KDE of the push forward of the prior through the model
push_forward_kde = kde(response_vals_at_prior_samples.T)
def push_forward_pdf(y): return push_forward_kde(y.T).squeeze()

#%%
#We can now simply evaluate
#
#.. math:: \pi_\text{post}(\rv)=\pi_\text{pr}(\rv)\frac{\pi_\text{obs}(f(\rv))}{\hat{\pi}_\text{model}(f(\rv))}
#
#using the approximate push forward PDF :math:`\hat{\pi}_\text{model}(f(\rv))`. Lets use this fact to plot the posterior density.


#Define the samples at which to evaluate the posterior density
num_pts_1d = 50
X, Y, samples_for_posterior_eval = pya.get_meshgrid_samples(model.ranges, 30)

#Evaluate the density of the prior at the samples used to evaluate
#the posterior
prior_prob_at_samples_for_posterior_eval = prior_pdf(
    samples_for_posterior_eval)

#Evaluate the model at the samples used to evaluate
#the posterior
response_vals_at_samples_for_posterior_eval = model(samples_for_posterior_eval)

#Evaluate the distribution on the observable data
obs_prob_at_samples_for_posterior_eval = obs_pdf(
    response_vals_at_samples_for_posterior_eval)

#Evaluate the probability of the responses at the desired points
response_prob_at_samples_for_posterior_eval = push_forward_pdf(
    response_vals_at_samples_for_posterior_eval)

#Evaluate the posterior probability
posterior_prob = prior_prob_at_samples_for_posterior_eval*(
    obs_prob_at_samples_for_posterior_eval/response_prob_at_samples_for_posterior_eval)

#Plot the posterior density
p = plt.contourf(
    X, Y, np.reshape(posterior_prob, X.shape),
    levels=np.linspace(posterior_prob.min(), posterior_prob.max(), 30),
    cmap=mpl.cm.coolwarm)

#%%
#Note that to plot the posterior we had to evaluate the model at each plot points. This can be expensive. To avoid this surrogate models can be used to replace the expensive model [BJWSISC2018b]_. But ignoring such approaches we can still obtain useful information without additional model evaluations as is done above. For example, we can easily and with no additional model evaluations evaluate the posterior density at the prior samples
prior_prob_at_prior_samples = prior_pdf(prior_samples)
obs_prob_at_prior_samples = obs_pdf(response_vals_at_prior_samples)
response_prob_at_prior_samples = push_forward_pdf(
    response_vals_at_prior_samples)
posterior_prob_at_prior_samples = prior_prob_at_prior_samples * (
    obs_prob_at_prior_samples / response_prob_at_prior_samples)

#%%
#We can also use rejection sampling to draw samples from the posterior
#Given a random number :math:`u\sim U[0,1]`, and the prior samples :math:`x`. We accept a prior sample as a draw from the posterior if
#
#.. math:: u\le\frac{\pi_\text{post}(\rv)}{M\pi_\text{prop}(\rv)}
#
#for some proposal density :math:`\pi_\text{prop}(\rv)` such that :math:`M\pi_\text{prop}(\rv)` is an upper bound on the density of the posterior. Here we set :math:`M=1.1\,\max_{\rv\in\rvdom}\pi_\text{post}(\rv)` to be a constant sligthly bigger than the maximum of the posterior over all the prior samples.

max_posterior_prob = posterior_prob_at_prior_samples.max()
accepted_samples_idx = np.where(
    posterior_prob_at_prior_samples/(1.1*max_posterior_prob) >
    np.random.uniform(0., 1., num_prior_samples))[0]
posterior_samples = prior_samples[:, accepted_samples_idx]
acceptance_ratio = float(posterior_samples.shape[1])/num_prior_samples
print(acceptance_ratio)

#plot the accepted samples
plt.plot(posterior_samples[0, :], posterior_samples[1, :], 'ok')
plt.xlim(model.ranges[:2])
_ = plt.ylim(model.ranges[2:])

#%%
#The goal of inverse problem was to define the posterior density that when push forward through the forward model exactly matches the observed PDF. Lets check that by pushing forward our samples from the posterior.

#compute the posterior push forward
posterior_push_forward_kde = kde(
    response_vals_at_prior_samples[accepted_samples_idx].T)


def posterior_push_forward_pdf(
    y): return posterior_push_forward_kde(y.T).squeeze()


plt.figure()
lb, ub = obs_variable.interval(1-1e-10)
y = np.linspace(lb, ub, 101)
plt.plot(y, obs_pdf(y), 'r-')
plt.plot(y, posterior_push_forward_pdf(y), 'k--')
plt.plot(y, push_forward_pdf(y), 'b:')
plt.show()

#%%
#Explore the number effect of changing the number of prior samples on the posterior and its push-forward.

#%%
#References
#^^^^^^^^^^
#.. [BJWSISC2018] `T Butler, J Jakeman, T Wildey. Combining push-forward measures and Bayes' rule to construct consistent solutions to stochastic inverse problems. SIAM Journal on Scientific Computing, 40 (2), A984-A1011 (2018). <https://doi.org/10.1137/16M1087229>`_
#
#.. [BJWSISC2018b] `T Butler, J Jakeman, T Wildey. Convergence of probability densities using approximate models for forward and inverse problems in uncertainty quantification. SIAM Journal on Scientific Computing, 40 (5), A3523-A3548 (2018). <https://doi.org/10.1137/18M1181675>`_
