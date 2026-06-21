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

    # Inferential figures (added v1.13) — let a narrative quote the p-value, the
    # robust slope and its CI, the seasonal strength/months, and the forecast.
    def _add(*values):
        for v in values:
            if v is None:
                continue
            allowed.add(abs(float(v)))
            allowed.add(abs(float(round(v))))

    _add(getattr(stats, "trend_p_value", None))
    _add(getattr(stats, "sen_slope_per_month", None))
    _add(getattr(stats, "seasonal_strength", None))
    _add(getattr(stats, "seasonal_peak_month", None))
    _add(getattr(stats, "seasonal_trough_month", None))
    strength = getattr(stats, "seasonal_strength", None)
    if strength is not None:
        _add(round(strength * 100))  # tolerate "75%" phrasing of strength 0.75
    for bound in getattr(stats, "sen_slope_ci", None) or []:
        _add(bound)
    for point in getattr(stats, "forecast", None) or []:
        _add(point.get("point"), point.get("lo"), point.get("hi"))
    return allowed


def allowed_from_totals(totals: list[int], *, extra: set[float] | None = None) -> set[float]:
    """Allowed figures for a comparison narrative: per-series totals plus extras."""
    allowed = {float(t) for t in totals}
    if extra:
        allowed |= extra
    return allowed


# ---------------------------------------------------------------------------
# Measuring the faithfulness check itself
# ---------------------------------------------------------------------------
#
# A check you cannot measure is a check you cannot trust. This labelled set lets
# us report how well the deterministic check actually performs, and where it is
# blind. The honest finding: it never wrongly rejects a faithful narrative
# (precision is high by design — it is conservative), but it cannot catch errors
# that are not about the numbers themselves, such as attaching a correct figure
# to the wrong subject, an unsupported editorial claim, or a wrong count that
# slips under the relative tolerance. That gap is exactly why an LLM judge is
# added as an independent second signal.

# Figures that legitimately appear in the reference statistics for these cases.
_BASE_ALLOWED = [4031, 6.5, 12.4, 300, 350, 407, 322, 2025, 2026, 3, 4]

# Each case: a narrative, the figures it is allowed to state, the computed trend,
# the ground-truth label, and the kind of (un)faithfulness it represents.
EVAL_SET: list[dict] = [
    # --- faithful (the check should pass these) ---
    {"id": "clean-1", "label": "faithful", "category": "clean", "trend": "falling",
     "narrative": "Between 2025-04 and 2026-03, SA Police recorded 4,031 offences. The trend "
                  "over the window is falling. The latest month is up 6.5% on the month before. "
                  "Year on year, the latest month is down 12.4%."},
    {"id": "clean-2", "label": "faithful", "category": "clean", "trend": "falling",
     "narrative": "The series is seasonal and tends to peak in March. The latest month recorded "
                  "407 offences."},
    {"id": "clean-3", "label": "faithful", "category": "clean-no-figures", "trend": "falling",
     "narrative": "The trend over the window is falling."},
    {"id": "round-1", "label": "faithful", "category": "legitimate-rounding", "trend": "falling",
     "narrative": "Year on year, the latest month is down about 12%."},

    # --- unfaithful the deterministic check SHOULD catch ---
    {"id": "fab-1", "label": "unfaithful", "category": "fabricated-number", "trend": "falling",
     "narrative": "Between 2025-04 and 2026-03, SA Police recorded 9,210 offences."},
    {"id": "fab-2", "label": "unfaithful", "category": "fabricated-number", "trend": "falling",
     "narrative": "The latest month is up 41.0% on the month before."},
    {"id": "contra-1", "label": "unfaithful", "category": "trend-contradiction", "trend": "falling",
     "narrative": "The trend over the window is rising."},
    {"id": "contra-2", "label": "unfaithful", "category": "trend-contradiction", "trend": "rising",
     "narrative": "Overall the trend over the window is falling."},

    # --- unfaithful the deterministic check CANNOT catch (the honest gap) ---
    {"id": "sem-1", "label": "unfaithful", "category": "semantic-mislabel", "trend": "falling",
     "narrative": "Robbery accounts for 4,031 offences in Adelaide."},  # 4031 is the theft total
    {"id": "sem-2", "label": "unfaithful", "category": "semantic-mislabel", "trend": "falling",
     "narrative": "Theft fell by 12.4% in Whyalla."},  # 12.4% is Adelaide's, not Whyalla's
    {"id": "claim-1", "label": "unfaithful", "category": "unsupported-claim", "trend": "falling",
     "narrative": "The trend over the window is falling, which reflects successful policing operations."},
    {"id": "claim-2", "label": "unfaithful", "category": "unsupported-claim", "trend": "falling",
     "narrative": "These numbers show the area is now safe."},
    {"id": "tol-1", "label": "unfaithful", "category": "tolerance-leak", "trend": "falling",
     "narrative": "Between 2025-04 and 2026-03, SA Police recorded 3,900 offences."},  # off by 131
]


def _binary_metrics(y_true_unfaithful: list[bool], y_pred_unfaithful: list[bool]) -> dict:
    """Precision/recall/F1/accuracy with 'unfaithful' as the positive class."""
    tp = sum(1 for t, p in zip(y_true_unfaithful, y_pred_unfaithful) if t and p)
    fp = sum(1 for t, p in zip(y_true_unfaithful, y_pred_unfaithful) if (not t) and p)
    tn = sum(1 for t, p in zip(y_true_unfaithful, y_pred_unfaithful) if (not t) and (not p))
    fn = sum(1 for t, p in zip(y_true_unfaithful, y_pred_unfaithful) if t and (not p))
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else None)
    total = tp + fp + tn + fn
    return {
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "accuracy": round((tp + tn) / total, 3) if total else None,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def measure_check(judge_fn=None) -> dict:
    """Measure the faithfulness check against the labelled set.

    ``judge_fn(narrative, allowed, trend_direction) -> bool | None`` is an optional
    independent second opinion (the LLM judge); ``True`` means it judged the
    narrative faithful. When provided, the result also reports the judge's own
    precision/recall and how often it agrees with the deterministic check.
    """
    y_true = [c["label"] == "unfaithful" for c in EVAL_SET]
    det_pred, judge_pred = [], []
    det_missed, by_category = [], {}

    for c in EVAL_SET:
        allowed = {float(x) for x in _BASE_ALLOWED}
        report = evaluate(c["narrative"], allowed, trend_direction=c["trend"])
        det_unfaithful = not report.passed
        det_pred.append(det_unfaithful)

        true_unfaithful = c["label"] == "unfaithful"
        if true_unfaithful and not det_unfaithful:
            det_missed.append(c["id"])
        cat = by_category.setdefault(c["category"], {"n": 0, "det_correct": 0})
        cat["n"] += 1
        if det_unfaithful == true_unfaithful:
            cat["det_correct"] += 1

        if judge_fn is not None:
            verdict = judge_fn(c["narrative"], allowed, c["trend"])
            judge_pred.append(None if verdict is None else (not verdict))

    result = {
        "labelled_cases": len(EVAL_SET),
        "positive_class": "unfaithful",
        "deterministic_check": {
            **_binary_metrics(y_true, det_pred),
            "missed_case_ids": det_missed,
            "by_category": by_category,
            "note": "Conservative by design: it never rejects a faithful narrative "
                    "(precision 1.0), but it only checks figures and trend direction, so "
                    "it cannot catch a correct figure attached to the wrong subject, an "
                    "unsupported claim, or a wrong count inside the relative tolerance.",
        },
    }
    if judge_fn is not None and all(p is not None for p in judge_pred) and judge_pred:
        result["llm_judge"] = {
            **_binary_metrics(y_true, judge_pred),
            "agreement_with_deterministic": round(
                sum(1 for d, j in zip(det_pred, judge_pred) if d == j) / len(EVAL_SET), 3
            ),
            "note": "An independent LLM judge over the same labelled set, intended to catch "
                    "the semantic errors the deterministic check is blind to.",
        }
    else:
        result["llm_judge"] = {
            "available": False,
            "note": "Set SIGNAL_LLM_API_KEY to measure the LLM judge as a second signal.",
        }
    return result
