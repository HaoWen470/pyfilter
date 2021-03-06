import torch
from math import sqrt
from torch.distributions import Normal, MultivariateNormal
from torch.nn import Module
from typing import Tuple
from .utils import construct_diag, ShapeLike, size_getter
from .timeseries import StateSpaceModel, StochasticProcess
from .parameter import ExtendedParameter


def _propagate_sps(
    spx: torch.Tensor, spn: torch.Tensor, process: StochasticProcess, temp_params: Tuple[torch.Tensor, ...]
):
    is_md = process.ndim > 0

    if not is_md:
        spx = spx.squeeze(-1)
        spn = spn.squeeze(-1)

    out = process.propagate_u(spx, u=spn, parameters=temp_params)
    return out if is_md else out.unsqueeze(-1)


def _covariance(a: torch.Tensor, b: torch.Tensor, wc: torch.Tensor):
    """
    Calculates the covariance from a * b^t
    """
    cov = a.unsqueeze(-1) * b.unsqueeze(-2)

    return (wc[:, None, None] * cov).sum(-3)


def _get_meancov(spxy: torch.Tensor, wm: torch.Tensor, wc: torch.Tensor):
    x = (wm.unsqueeze(-1) * spxy).sum(-2)
    centered = spxy - x.unsqueeze(-2)

    return x, _covariance(centered, centered, wc)


class UFTCorrectionResult(Module):
    def __init__(self, mean: torch.Tensor, cov: torch.Tensor, state_slice: slice, ym: torch.Tensor, yc: torch.Tensor):
        super().__init__()
        self.register_buffer("ym", ym)
        self.register_buffer("yc", yc)

        self.register_buffer("mean", mean)
        self.register_buffer("cov", cov)
        self._sslc = state_slice

    @property
    def xm(self):
        return self.mean[..., self._sslc]

    @property
    def xc(self):
        return self.cov[..., self._sslc, self._sslc]

    @staticmethod
    def _helper(m, c):
        if m.shape[-1] > 1:
            return MultivariateNormal(m, c)

        return Normal(m[..., 0], c[..., 0, 0].sqrt())

    def x_dist(self):
        return self._helper(self.xm, self.xc)

    def y_dist(self):
        return self._helper(self.ym, self.yc)


class UFTPredictionResult(Module):
    def __init__(self, spx: torch.Tensor, spy: torch.Tensor):
        super().__init__()
        self.spx = spx
        self.spy = spy


class AggregatedResult(Module):
    def __init__(self, xm, xc, ym, yc):
        super().__init__()
        self.xm = xm
        self.xc = xc
        self.ym = ym
        self.yc = yc


class UnscentedFilterTransform(Module):
    def __init__(self, model: StateSpaceModel, a=1.0, b=2.0, k=0.0):
        """
        Implements the Unscented Transform for a state space model.
        :param model: The model
        :param a: The alpha parameter. Defined on the interval [0, 1]
        :param b: The beta parameter. Optimal value for Gaussian models is 2
        :param k: The kappa parameter. To control the semi-definiteness
        """

        super().__init__()
        if len(model.hidden.increment_dist().event_shape) > 1:
            raise ValueError("Can at most handle vector valued processes!")

        if any(model.hidden.increment_dist.named_parameters()) or any(
            model.observable.increment_dist.named_parameters()
        ):
            raise ValueError("Cannot currently handle case when distribution is parameterized!")

        self._model = model
        self._trans_dim = (
            1 if len(model.hidden.increment_dist().event_shape) == 0 else model.hidden.increment_dist().event_shape[0]
        )

        self._ndim = model.hidden.num_vars + self._trans_dim + model.observable.num_vars

        self._a = a
        self._b = b
        self._lam = a ** 2 * (self._ndim + k) - self._ndim

        self._hidden_views = None
        self._obs_views = None

        self._diaginds = range(model.hidden_ndim)

    def _set_slices(self):
        hidden_dim = self._model.hidden.num_vars

        self._sslc = slice(hidden_dim)
        self._hslc = slice(hidden_dim, hidden_dim + self._trans_dim)
        self._oslc = slice(hidden_dim + self._trans_dim, None)

        return self

    def _set_weights(self):
        self._wm = torch.zeros(1 + 2 * self._ndim)
        self._wc = self._wm.clone()
        self._wm[0] = self._lam / (self._ndim + self._lam)
        self._wc[0] = self._wm[0] + (1 - self._a ** 2 + self._b)
        self._wm[1:] = self._wc[1:] = 1 / 2 / (self._ndim + self._lam)

        return self

    def _set_arrays(self, shape: torch.Size):
        view_shape = (shape[0], *(1 for _ in shape)) if len(shape) > 0 else shape

        self._hidden_views = tuple(
                p.view(view_shape) if isinstance(p, ExtendedParameter) else p
                for p in self._model.hidden.functional_parameters()
        )

        self._obs_views = tuple(
                p.view(view_shape) if isinstance(p, ExtendedParameter) else p
                for p in self._model.observable.functional_parameters()
        )

        return self

    def initialize(self, shape: ShapeLike = None):
        shape = size_getter(shape)
        self._set_weights()._set_slices()._set_arrays(shape)

        mean = torch.zeros((*shape, self._ndim))
        cov = torch.zeros((*shape, self._ndim, self._ndim))

        s_mean = self._model.hidden.i_sample((1000, *shape)).mean(0)
        if self._model.hidden_ndim < 1:
            s_mean.unsqueeze_(-1)

        mean[..., self._sslc] = s_mean

        var = s_cov = self._model.hidden.initial_dist().variance
        if self._model.hidden_ndim > 0:
            s_cov = construct_diag(var)

        cov[..., self._sslc, self._sslc] = s_cov
        cov[..., self._hslc, self._hslc] = construct_diag(self._model.hidden.increment_dist().variance)
        cov[..., self._oslc, self._oslc] = construct_diag(self._model.observable.increment_dist().variance)

        return UFTCorrectionResult(mean, cov, self._sslc, None, None)

    def _get_sps(self, state: UFTCorrectionResult):
        cholcov = sqrt(self._lam + self._ndim) * torch.cholesky(state.cov)

        spx = state.mean.unsqueeze(-2)
        sph = state.mean[..., None, :] + cholcov
        spy = state.mean[..., None, :] - cholcov

        return torch.cat((spx, sph, spy), -2)

    def predict(self, utf_corr: UFTCorrectionResult):
        sps = self._get_sps(utf_corr)

        spx = _propagate_sps(sps[..., self._sslc], sps[..., self._hslc], self._model.hidden, self._hidden_views)
        spy = _propagate_sps(spx, sps[..., self._oslc], self._model.observable, self._obs_views)

        return UFTPredictionResult(spx, spy)

    def calc_mean_cov(self, uft_pred: UFTPredictionResult):
        xmean, xcov = _get_meancov(uft_pred.spx, self._wm, self._wc)
        ymean, ycov = _get_meancov(uft_pred.spy, self._wm, self._wc)

        return AggregatedResult(xmean, xcov, ymean, ycov)

    def update_state(
        self,
        xm: torch.Tensor,
        xc: torch.Tensor,
        state: UFTCorrectionResult,
        ym: torch.Tensor = None,
        yc: torch.Tensor = None,
    ):
        # ===== Overwrite ===== #
        mean = state.mean.clone()
        cov = state.cov.clone()

        mean[..., self._sslc] = xm
        cov[..., self._sslc, self._sslc] = xc

        return UFTCorrectionResult(mean, cov, self._sslc, ym, yc)

    def correct(self, y: torch.Tensor, uft_pred: UFTPredictionResult, prev_corr: UFTCorrectionResult):
        correction = self.calc_mean_cov(uft_pred)
        xmean, xcov, ymean, ycov = correction.xm, correction.xc, correction.ym, correction.yc

        if xmean.dim() > 1:
            tx = uft_pred.spx - xmean.unsqueeze(-2)
        else:
            tx = uft_pred.spx - xmean

        if ymean.dim() > 1:
            ty = uft_pred.spy - ymean.unsqueeze(-2)
        else:
            ty = uft_pred.spy - ymean

        xycov = _covariance(tx, ty, self._wc)

        gain = torch.matmul(xycov, ycov.inverse())

        txmean = xmean + torch.matmul(gain, (y - ymean).unsqueeze(-1))[..., 0]

        temp = torch.matmul(ycov, gain.transpose(-1, -2))
        txcov = xcov - torch.matmul(gain, temp)

        return self.update_state(txmean, txcov, prev_corr, ymean, ycov)
