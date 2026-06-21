"""Tests for the statistical methods in signalkit.analyst.stats.

Each test drives a synthetic series with a known answer, so a regression in the
maths is caught rather than just "it ran".
"""

import math

from signalkit.analyst import stats


def _months(n: int, start_year: int = 2024, start_month: int = 1) -> list[str]:
    out = []
    idx = start_year * 12 + (start_month - 1)
    for _ in range(n):
        out.append(f"{idx // 12:04d}-{idx % 12 + 1:02d}")
        idx += 1
    return out


# --- Mann-Kendall ---------------------------------------------------------


def test_mann_kendall_detects_monotonic_increase():
    res = stats.mann_kendall([float(i) for i in range(12)])
    assert res is not None
    assert res.trend == "increasing"
    assert res.significant is True
    assert res.p_value < 0.05
    assert res.s_statistic > 0


def test_mann_kendall_detects_decrease():
    res = stats.mann_kendall([float(12 - i) for i in range(12)])
    assert res.trend == "decreasing"
    assert res.significant is True
    assert res.s_statistic < 0


def test_mann_kendall_flat_series_is_no_trend():
    # Alternating values: no monotonic trend.
    res = stats.mann_kendall([5.0, 6.0, 5.0, 6.0, 5.0, 6.0, 5.0, 6.0])
    assert res.trend == "no trend"
    assert res.significant is False


def test_mann_kendall_too_short_returns_none():
    assert stats.mann_kendall([1.0, 2.0, 3.0]) is None


# --- Sen's slope ----------------------------------------------------------


def test_sen_slope_recovers_known_slope():
    # y = 3x + 10, with one outlier the robust slope should ignore.
    values = [10.0 + 3 * i for i in range(10)]
    values[4] = 999.0  # outlier
    sen = stats.sen_slope(values)
    assert sen is not None
    assert abs(sen.slope_per_month - 3.0) < 0.5  # robust to the spike
    assert sen.lo <= sen.slope_per_month <= sen.hi


def test_sen_slope_too_short_returns_none():
    assert stats.sen_slope([1.0, 2.0]) is None


# --- Seasonality ----------------------------------------------------------


def test_seasonal_decompose_finds_peak_and_trough():
    months = _months(24)
    # Flat level + a seasonal bump that peaks in month 7 (July), troughs month 1.
    seasonal = {m: 0.0 for m in range(1, 13)}
    for m in range(1, 13):
        seasonal[m] = 40 * math.sin(2 * math.pi * (m - 4) / 12)  # peak ~ month 7
    values = [100 + seasonal[int(mo.split("-")[1])] for mo in months]
    season = stats.seasonal_decompose(months, values)
    assert season is not None
    assert season.established is True  # 24 months = two full cycles
    assert season.seasonal_strength > 0.8  # almost all variance is seasonal
    assert season.peak_month == 7
    assert season.trough_month == 1


def test_seasonal_decompose_flags_short_series():
    months = _months(18)
    values = [100.0 + i for i in range(18)]
    season = stats.seasonal_decompose(months, values)
    assert season is not None
    assert season.established is False  # < 24 months
    assert season.months_observed == 18


def test_seasonal_decompose_rejects_under_a_year():
    assert stats.seasonal_decompose(_months(6), [1.0] * 6) is None


# --- Forecast -------------------------------------------------------------


def test_forecast_returns_ordered_horizon_with_intervals():
    months = _months(24)
    values = [100.0 + 2 * i for i in range(24)]  # steady rise
    fc = stats.forecast(months, values, horizon=3)
    assert fc is not None
    assert fc.horizon == 3
    assert len(fc.points) == 3
    # Months continue the series.
    assert fc.points[0].month == "2026-01"
    assert fc.points[-1].month == "2026-03"
    for p in fc.points:
        assert p.lo <= p.point <= p.hi
    # Rising trend → forecast above the last observed value.
    assert fc.points[0].point > values[-1] - 5


def test_forecast_interval_widens_with_horizon():
    months = _months(24)
    # Irregular (non-period-12) noise so it survives seasonal removal as residual.
    noise = [3, -2, 5, -4, 1, 2, -3, 4, -1, 0, 2, -5,
             -2, 7, -6, 5, 1, -8, 2, -3, 9, -2, 0, 6]
    values = [100.0 + 2 * i + noise[i] for i in range(24)]
    fc = stats.forecast(months, values, horizon=3)
    assert fc.residual_std > 0
    width_first = fc.points[0].hi - fc.points[0].lo
    width_last = fc.points[-1].hi - fc.points[-1].lo
    assert width_last > width_first  # uncertainty grows with horizon


def test_forecast_too_short_returns_none():
    assert stats.forecast(_months(4), [1.0, 2.0, 3.0, 4.0], horizon=3) is None
