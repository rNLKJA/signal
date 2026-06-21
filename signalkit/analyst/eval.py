"""Deterministic faithfulness eval for LLM-written narratives.

Signal phrases its narratives with an LLM from computed aggregates. This module
checks — without any model call — that a narrative only states numbers that
appear in those aggregates, and that it does not contradict the computed trend
direction. A narrative that fails is rejected by the analyst in favour of the
deterministic template, so an unfaithful summary never reaches a user, and the
rejection is recorded in the audit log.

The check is intentionally conservative: it errs towards passing a faithful
narrative rather than risk wrongly rejecting one. The number check (no
fabricated figures) is the headline; the trend-direction check is scoped to the
sentence that actually talks about the trend, to avoid tripping on the separate
month-on-month "up/down" phrasing.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# Numbers as written in prose: 1,234 · 12.5 · 407 (thousands separators allowed).
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Direction vocabulary for the trend-contradiction check.
_RISING = {
    "rising", "rise", "risen", "rose", "increasing", "increase", "increased",
    "growing", "grew", "grown", "upward", "upwards", "climbing", "climbed", "higher",
}
_FALLING = {
    "falling", "fall", "fallen", "fell", "declining", "decline", "declined",
    "decreasing", "decrease", "decreased", "dropping", "dropped", "downward",
    "downwards", "lower", "lowering",
}


class FaithfulnessReport(BaseModel):
    """The verdict on whether a narrative is faithful to its statistics."""

    score: float = Field(ge=0.0, le=1.0, description="Supported figures / total figures, penalised for contradictions")
    passed: bool = Field(description="True if no fabricated figures and no trend contradiction")
    issues: list[str] = Field(default_factory=list, description="Human-readable reasons it failed")
    checked_numbers: int = Field(default=0, description="How many numeric figures were checked")


def _norm(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _is_allowed(value: float, allowed: set[float], tolerance: float) -> bool:
    if value in allowed:
        return True
    # Tolerant match for rounded figures (e.g. a percentage quoted to fewer dp).
    return any(abs(value - a) <= max(tolerance, abs(a) * tolerance) for a in allowed)


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def evaluate(
    narrative: str,
    allowed: set[float],
    *,
    trend_direction: str | None = None,
    tolerance: float = 0.05,
) -> FaithfulnessReport:
    """Score a narrative against the set of figures it is allowed to state.

    ``allowed`` is the set of every number that legitimately appears in (or is
    directly read from) the computed statistics. ``trend_direction`` is the
    deterministic verdict ("rising" | "falling" | "flat"); when it is rising or
    falling, the sentence mentioning the trend must not assert the opposite.
    """
    issues: list[str] = []
    numbers = [n for n in (_norm(m.group()) for m in _NUM_RE.finditer(narrative)) if n is not None]

    unsupported = [n for n in numbers if not _is_allowed(n, allowed, tolerance)]
    for n in unsupported:
        issues.append(f"unsupported figure '{_fmt(n)}' is not in the computed statistics")

    contradiction = False
    if trend_direction in ("rising", "falling"):
        for sentence in re.split(r"(?<=[.!?])\s+", narrative):
            if "trend" not in sentence.lower():
                continue
            words = set(re.findall(r"[a-z]+", sentence.lower()))
            says_rising = bool(words & _RISING)
            says_falling = bool(words & _FALLING)
            opposite = _FALLING if trend_direction == "rising" else _RISING
            same = _RISING if trend_direction == "rising" else _FALLING
            if (words & opposite) and not (words & same):
                contradiction = True
                issues.append(
                    f"trend is computed as {trend_direction} but the narrative describes it as the opposite"
                )
            # only the first trend sentence is judged
            _ = says_rising, says_falling
            break

    total = len(numbers)
    num_score = 1.0 if total == 0 else (total - len(unsupported)) / total
    score = num_score * (0.5 if contradiction else 1.0)
    passed = not unsupported and not contradiction
    return FaithfulnessReport(
        score=round(score, 4), passed=passed, issues=issues, checked_numbers=total
    )


def _year_month_parts(label: str) -> set[float]:
    """Year and month integers from a 'YYYY-MM' label, so dates in prose pass."""
    parts: set[float] = set()
    m = re.match(r"(\d{4})-(\d{2})", str(label))
    if m:
        parts.add(float(int(m.group(1))))
        parts.add(float(int(m.group(2))))
    return parts


def allowed_from_stats(stats) -> set[float]:
    """Every figure a faithful narrative may quote, drawn from a TrendStats."""
    allowed: set[float] = {float(stats.total_offences), float(len(stats.monthly_counts))}
    allowed.update(float(v) for v in stats.monthly_counts.values())
    for pct in (stats.mom_change_pct, stats.yoy_change_pct):
        if pct is not None:
            allowed.add(abs(float(pct)))
            allowed.add(abs(float(round(pct))))  # tolerate a rounded percentage
    allowed.update(float(t.get("count", 0)) for t in stats.top_offenses)
    allowed.update(float(v) for v in stats.by_offense_division.values())
    for label in (stats.window_start, stats.window_end, *stats.monthly_counts.keys()):
        allowed |= _year_month_parts(label)
    return allowed


def allowed_from_totals(totals: list[int], *, extra: set[float] | None = None) -> set[float]:
    """Allowed figures for a comparison narrative: per-series totals plus extras."""
    allowed = {float(t) for t in totals}
    if extra:
        allowed |= extra
    return allowed
