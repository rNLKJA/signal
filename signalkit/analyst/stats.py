"""Statistical methods for the analyst layer.

The descriptive figures in ``core.py`` (month-on-month, year-on-year, a naive
z-score) answer *what happened*. This module answers the harder, more defensible
questions a statistician would ask of a monthly crime series:

- **Is the trend real, or noise?** Mann-Kendall, a non-parametric monotonic-trend
  test that makes no assumption of normality or linearity — the standard choice for
  environmental and crime time series.
- **How steep is it, robustly?** The Sen (Theil-Sen) slope, a median-of-pairwise-
  slopes estimator that is not dragged around by one anomalous month, reported with
  a confidence interval.
- **How much is just the calendar?** An additive month-of-year decomposition into
  trend, seasonal and residual components, with an honest "less than two full years"
  flag — crime is seasonal, and saying so is part of reading the data correctly.
- **What comes next, with what uncertainty?** A transparent level-plus-trend forecast
  on the deseasonalised series, with an empirical prediction interval. Explainable on
  purpose: every figure can be traced to a named method, which is what lets the
  forecast itself be governed and faithfulness-checked.

Everything here is a pure function over a monthly series. Wiring into the audit log
and the narrative happens in ``core.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import stats

# A two-sided test at this level decides "significant trend" vs "no trend".
SIGNIFICANCE_ALPHA = 0.05
# Two full seasonal cycles (24 monthly observations) before seasonality is
# treated as established rather than indicative.
FULL_SEASON_MONTHS = 24


@dataclass
class MannKendallResult:
    """Non-parametric monotonic-trend test."""

    trend: str  # "increasing" | "decreasing" | "no trend"
    significant: bool
    p_value: float
    s_statistic: float
    z_score: float
    n: int


@dataclass
class SenSlope:
    """Robust (Theil-Sen) slope, in units of offences per month."""

    slope_per_month: float
    intercept: float
    lo: float
    hi: float


@dataclass
class Seasonality:
    """Additive month-of-year decomposition of a monthly series."""

    seasonal_strength: float  # 0..1 — share of detrended variance explained by season
    peak_month: Optional[int]  # 1..12, calendar month with the largest seasonal lift
    trough_month: Optional[int]
    seasonal_by_month: dict[int, float] = field(default_factory=dict)  # calendar month -> effect
    established: bool = False  # True once >= two full cycles are observed
    months_observed: int = 0


@dataclass
class ForecastPoint:
    month: str  # "YYYY-MM"
    point: float
    lo: float
    hi: float


@dataclass
class Forecast:
    method: str
    horizon: int
    points: list[ForecastPoint] = field(default_factory=list)
    residual_std: float = 0.0


def mann_kendall(values: list[float]) -> Optional[MannKendallResult]:
    """Mann-Kendall trend test with tie-corrected variance.

    Returns ``None`` for series too short to test (n < 4). The S statistic counts
    how many later observations rise vs fall against earlier ones; its tie-corrected
    variance gives a normal approximation and a two-sided p-value.
    """
    x = np.asarray(values, dtype=float)
    n = x.size
    if n < 4:
        return None

    # S = sum over i < j of sign(x_j - x_i), computed pairwise.
    s = 0.0
    for i in range(n - 1):
        s += np.sign(x[i + 1 :] - x[i]).sum()

    # Variance with a correction term for tied groups.
    _, counts = np.unique(x, return_counts=True)
    tie_term = np.sum(counts * (counts - 1) * (2 * counts + 5))
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0

    if var_s <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / np.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / np.sqrt(var_s)
    else:
        z = 0.0

    p = 2 * (1 - stats.norm.cdf(abs(z)))
    significant = bool(p < SIGNIFICANCE_ALPHA)
    if not significant:
        trend = "no trend"
    elif s > 0:
        trend = "increasing"
    else:
        trend = "decreasing"

    return MannKendallResult(
        trend=trend,
        significant=significant,
        p_value=round(float(p), 4),
        s_statistic=float(s),
        z_score=round(float(z), 3),
        n=int(n),
    )


def sen_slope(values: list[float]) -> Optional[SenSlope]:
    """Theil-Sen robust slope (offences/month) with a 95% confidence interval."""
    y = np.asarray(values, dtype=float)
    n = y.size
    if n < 3:
        return None
    x = np.arange(n, dtype=float)
    slope, intercept, lo, hi = stats.theilslopes(y, x, alpha=0.95)
    return SenSlope(
        slope_per_month=round(float(slope), 2),
        intercept=round(float(intercept), 2),
        lo=round(float(lo), 2),
        hi=round(float(hi), 2),
    )


def _month_number(month_label: str) -> int:
    """Calendar month 1..12 from a ``YYYY-MM`` label."""
    return int(month_label.split("-")[1])


def seasonal_decompose(months: list[str], values: list[float]) -> Optional[Seasonality]:
    """Additive decomposition: value = trend + seasonal + residual.

    The trend is the robust Theil-Sen line over the whole window. A short series
    (~21 months) does not support a moving-average trend without bad edge effects,
    so a robust straight line is the honest choice — it isolates the seasonal swing
    without inventing structure the data cannot carry.

    Seasonal strength follows the standard ``max(0, 1 - var(resid)/var(resid+seasonal))``.
    With fewer than two full years the seasonal estimate is returned but flagged
    ``established=False`` — honest about what ~21 months can and cannot support.
    """
    if len(values) != len(months) or len(values) < 12:
        return None
    y = np.asarray(values, dtype=float)
    n = y.size

    sen = sen_slope(values)
    x = np.arange(n, dtype=float)
    trend = sen.intercept + sen.slope_per_month * x if sen else np.full(n, y.mean())
    detrended = y - trend

    # Seasonal effect = mean detrended value per calendar month, centred to sum to zero.
    month_nums = np.array([_month_number(m) for m in months])
    seasonal_by_month: dict[int, float] = {}
    for cm in range(1, 13):
        mask = month_nums == cm
        if mask.any():
            seasonal_by_month[cm] = float(detrended[mask].mean())
    centre = np.mean(list(seasonal_by_month.values()))
    seasonal_by_month = {cm: v - centre for cm, v in seasonal_by_month.items()}

    seasonal = np.array([seasonal_by_month.get(int(cm), 0.0) for cm in month_nums])
    residual = detrended - seasonal

    var_resid = float(np.var(residual))
    var_deseason = float(np.var(residual + seasonal))
    strength = 0.0 if var_deseason == 0 else max(0.0, 1 - var_resid / var_deseason)

    peak = max(seasonal_by_month, key=seasonal_by_month.get) if seasonal_by_month else None
    trough = min(seasonal_by_month, key=seasonal_by_month.get) if seasonal_by_month else None

    return Seasonality(
        seasonal_strength=round(strength, 3),
        peak_month=peak,
        trough_month=trough,
        seasonal_by_month={k: round(v, 1) for k, v in seasonal_by_month.items()},
        established=n >= FULL_SEASON_MONTHS,
        months_observed=n,
    )


def _add_months(month_label: str, k: int) -> str:
    year, month = (int(p) for p in month_label.split("-"))
    idx = (year * 12 + (month - 1)) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def forecast(
    months: list[str], values: list[float], horizon: int = 3
) -> Optional[Forecast]:
    """Level-plus-trend forecast on the deseasonalised series, with an empirical PI.

    Deseasonalise with the month-of-year effects, fit a robust (Sen) trend to the
    deseasonalised level, project ``horizon`` months ahead, then add the seasonal
    effect back for each target calendar month. The prediction interval is
    ``point ± 1.96 * std(in-sample residuals)`` — transparent rather than a black box.
    """
    if len(values) != len(months) or len(values) < 6 or horizon < 1:
        return None
    y = np.asarray(values, dtype=float)
    n = y.size

    season = seasonal_decompose(months, values)
    seasonal_by_month = season.seasonal_by_month if season else {}
    month_nums = np.array([_month_number(m) for m in months])
    seasonal = np.array([seasonal_by_month.get(int(cm), 0.0) for cm in month_nums])

    deseason = y - seasonal
    sen = sen_slope(list(deseason))
    if sen is None:
        return None
    x = np.arange(n, dtype=float)
    fitted_deseason = sen.intercept + sen.slope_per_month * x
    residuals = deseason - fitted_deseason
    resid_std = float(np.std(residuals, ddof=1)) if n > 2 else float(np.std(residuals))

    points: list[ForecastPoint] = []
    for h in range(1, horizon + 1):
        target_month = _add_months(months[-1], h)
        cm = _month_number(target_month)
        level = sen.intercept + sen.slope_per_month * (n - 1 + h)
        point = level + seasonal_by_month.get(cm, 0.0)
        # Widen the interval modestly with horizon (random-walk-style growth in sd).
        band = 1.96 * resid_std * np.sqrt(h)
        points.append(
            ForecastPoint(
                month=target_month,
                point=round(max(0.0, point), 1),
                lo=round(max(0.0, point - band), 1),
                hi=round(point + band, 1),
            )
        )

    return Forecast(
        method="deseasonalised level+trend (Sen slope), empirical PI",
        horizon=horizon,
        points=points,
        residual_std=round(resid_std, 1),
    )
