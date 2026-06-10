"""
signalkit/analyst/core.py
=========================
The analyst layer: turns a filtered slice of the complaint data into trend
statistics and a plain-language narrative, and writes a governance
DecisionEntry for every answer it produces.

Governance design points:

  - The statistics are deterministic and computed from aggregate data only.
  - The optional LLM (set ANTHROPIC_API_KEY and install the ``llm`` extra)
    phrases the narrative from the computed statistics. It never sees raw
    incident data, only the aggregates in TrendStats.
  - Every answer is logged before it is returned, and the API response
    carries the ``decision_id`` so any output can be traced back to its
    audit entry: what model ran, what data informed it, what was decided,
    and whether human review is required.
  - Answers containing anomalous months are flagged
    ``human_review_required=True`` — a spike should be checked by a person
    before anyone acts on it.
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from signalkit.data.nypd import MonthlyRecord, get_records
from signalkit.governance.decision_log import (
    DecisionCategory,
    DecisionEntry,
    DecisionLogger,
    RiskCategory,
)

DETERMINISTIC_MODEL = "signal-stats-v1 (deterministic)"
DEFAULT_LOG_PATH = "logs/decisions.jsonl"
ANOMALY_Z_THRESHOLD = 2.0
TREND_SLOPE_THRESHOLD = 0.01  # fraction of mean per month


class NoDataError(Exception):
    """Raised when filters match nothing; carries valid values as suggestions."""

    def __init__(self, message: str, suggestions: dict[str, list[str]]):
        super().__init__(message)
        self.suggestions = suggestions


class AnalystQuery(BaseModel):
    """A question to the analyst. Filters are case-insensitive substrings."""

    question: str = Field(default="", description="Free-text question, recorded in the audit log")
    offense: Optional[str] = Field(default=None, description="Offence filter, e.g. 'burglary'")
    borough: Optional[str] = Field(default=None, description="Borough filter, e.g. 'brooklyn'")
    months: int = Field(default=12, ge=2, le=24, description="Trailing window size in months")


class TrendStats(BaseModel):
    """Deterministic statistics computed over the filtered window."""

    window_start: str
    window_end: str
    total_complaints: int
    monthly_counts: dict[str, int]
    mom_change_pct: Optional[float] = Field(
        default=None, description="Last month vs the month before, percent"
    )
    yoy_change_pct: Optional[float] = Field(
        default=None, description="Last month vs the same month a year earlier, percent"
    )
    trend_direction: Literal["rising", "falling", "flat"]
    anomalous_months: list[str] = Field(
        default_factory=list, description=f"Months with |z| >= {ANOMALY_Z_THRESHOLD}"
    )
    top_offenses: list[dict] = Field(
        default_factory=list, description="Top 5 offence categories in scope, with counts"
    )


class AnalystAnswer(BaseModel):
    """What the analyst returns: narrative + stats + the audit pointer."""

    narrative: str
    stats: TrendStats
    decision_id: str
    data_source: str
    model_used: str
    human_review_required: bool
    generated_at: datetime


def _slope(series: list[int]) -> float:
    """Least-squares slope of counts against month index."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = range(n)
    mean_x = (n - 1) / 2
    mean_y = sum(series) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series))
    var = sum((x - mean_x) ** 2 for x in xs)
    return cov / var if var else 0.0


def compute_stats(records: list[MonthlyRecord], months: int) -> TrendStats:
    by_month: dict[str, int] = {}
    for rec in records:
        by_month[rec.month] = by_month.get(rec.month, 0) + rec.count
    ordered_months = sorted(by_month)  # YYYY-MM sorts correctly as strings
    window = ordered_months[-months:]
    monthly = {m: by_month[m] for m in window}
    series = list(monthly.values())

    mom = None
    if len(series) >= 2 and series[-2] > 0:
        mom = round((series[-1] - series[-2]) / series[-2] * 100, 1)

    yoy = None
    last_month = window[-1]
    year_earlier = f"{int(last_month[:4]) - 1}-{last_month[5:]}"
    if year_earlier in by_month and by_month[year_earlier] > 0:
        yoy = round((monthly[last_month] - by_month[year_earlier]) / by_month[year_earlier] * 100, 1)

    mean = statistics.mean(series)
    slope = _slope(series)
    if mean > 0 and abs(slope) / mean >= TREND_SLOPE_THRESHOLD:
        direction = "rising" if slope > 0 else "falling"
    else:
        direction = "flat"

    anomalies = []
    if len(series) >= 6:
        stdev = statistics.pstdev(series)
        if stdev > 0:
            anomalies = [
                m for m, v in monthly.items() if abs(v - mean) / stdev >= ANOMALY_Z_THRESHOLD
            ]

    offense_totals: dict[str, int] = {}
    for rec in records:
        if rec.month in monthly:
            offense_totals[rec.offense] = offense_totals.get(rec.offense, 0) + rec.count
    top = sorted(offense_totals.items(), key=lambda kv: -kv[1])[:5]

    return TrendStats(
        window_start=window[0],
        window_end=window[-1],
        total_complaints=sum(series),
        monthly_counts=monthly,
        mom_change_pct=mom,
        yoy_change_pct=yoy,
        trend_direction=direction,
        anomalous_months=anomalies,
        top_offenses=[{"offense": o, "count": c} for o, c in top],
    )


def _template_narrative(stats: TrendStats, query: AnalystQuery) -> str:
    scope_bits = []
    if query.offense:
        scope_bits.append(f"offence matching '{query.offense}'")
    if query.borough:
        scope_bits.append(f"in {query.borough.upper()}")
    scope = " ".join(scope_bits) if scope_bits else "all complaints citywide"

    parts = [
        f"Between {stats.window_start} and {stats.window_end}, NYPD recorded "
        f"{stats.total_complaints:,} complaints for {scope}.",
        f"The trend over the window is {stats.trend_direction}.",
    ]
    if stats.mom_change_pct is not None:
        verb = "up" if stats.mom_change_pct >= 0 else "down"
        parts.append(f"The latest month is {verb} {abs(stats.mom_change_pct)}% on the month before.")
    if stats.yoy_change_pct is not None:
        verb = "up" if stats.yoy_change_pct >= 0 else "down"
        parts.append(f"Year on year, the latest month is {verb} {abs(stats.yoy_change_pct)}%.")
    if stats.anomalous_months:
        parts.append(
            "Anomalous months flagged for human review: "
            + ", ".join(stats.anomalous_months) + "."
        )
    return " ".join(parts)


def _llm_narrative(stats: TrendStats, query: AnalystQuery, model: str) -> str:
    """Phrase the narrative with an LLM. Receives aggregates only, never raw data."""
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        "You are a careful crime-data analyst. Using ONLY the statistics below, "
        "write a 3-4 sentence plain-English summary. Do not invent numbers.\n\n"
        f"Question: {query.question or '(trend summary)'}\n"
        f"Filters: offence={query.offense or 'all'}, borough={query.borough or 'all'}\n"
        f"Statistics: {stats.model_dump_json()}"
    )
    message = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


class Analyst:
    """Answers queries over the complaint data and audit-logs every answer."""

    def __init__(self, log_path: str | None = None, offline: bool | None = None):
        self._logger = DecisionLogger(
            log_path or os.environ.get("SIGNAL_LOG_PATH", DEFAULT_LOG_PATH)
        )
        self._offline = offline

    def ask(self, query: AnalystQuery) -> AnalystAnswer:
        records, source_label = get_records(self._offline)

        filtered = [
            r
            for r in records
            if (not query.offense or query.offense.lower() in r.offense.lower())
            and (not query.borough or query.borough.lower() in r.borough.lower())
        ]
        if not filtered:
            raise NoDataError(
                "No records match those filters.",
                suggestions={
                    "boroughs": sorted({r.borough for r in records}),
                    "offenses": sorted({r.offense for r in records})[:30],
                },
            )

        stats = compute_stats(filtered, query.months)
        model_used = DETERMINISTIC_MODEL
        llm_model = os.environ.get("SIGNAL_LLM_MODEL", "claude-haiku-4-5-20251001")
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                narrative = _llm_narrative(stats, query, llm_model)
                model_used = llm_model
            except Exception:
                narrative = _template_narrative(stats, query)
        else:
            narrative = _template_narrative(stats, query)

        human_review = bool(stats.anomalous_months)
        entry = DecisionEntry(
            model_name=model_used,
            model_provider="Anthropic" if model_used == llm_model else None,
            input_summary=(
                f"question='{query.question}' offense='{query.offense}' "
                f"borough='{query.borough}' months={query.months}"
            ),
            model_output_summary=narrative[:300],
            data_sources=[source_label],
            decision_made="Returned trend analysis to caller via Signal API.",
            decision_category=DecisionCategory.analytical,
            confidence_score=0.95 if model_used == DETERMINISTIC_MODEL else 0.8,
            human_review_required=human_review,
            legislative_basis=(
                "APS Mandatory AI Requirements (Jun 2026); EU AI Act Art. 50 transparency"
            ),
            risk_category=RiskCategory.limited,
            tags=["nypd-complaints", "trend-analysis"],
        )
        self._logger.log(entry)

        return AnalystAnswer(
            narrative=narrative,
            stats=stats,
            decision_id=entry.decision_id,
            data_source=source_label,
            model_used=model_used,
            human_review_required=human_review,
            generated_at=datetime.now(timezone.utc),
        )

    def recent_decisions(self, limit: int = 20) -> list[DecisionEntry]:
        """Read back the most recent audit entries (newest last)."""
        return self._logger.read_all()[-limit:]
