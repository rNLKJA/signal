"""
signalkit/analyst/core.py
=========================
The analyst layer: turns a filtered slice of the complaint data into trend
statistics and a plain-language narrative, and writes a governance
DecisionEntry for every answer it produces.

Governance design points:

  - The statistics are deterministic and computed from aggregate data only.
  - The optional LLM (set SIGNAL_LLM_API_KEY; any OpenAI-compatible
    provider, DeepSeek by default) phrases the narrative from the computed
    statistics. It never sees raw incident data, only the aggregates in
    TrendStats. The audit entry records which provider and model actually
    produced the words — swapping the model never weakens the trail.
  - Every answer is logged before it is returned, and the API response
    carries the ``decision_id`` so any output can be traced back to its
    audit entry: what model ran, what data informed it, what was decided,
    and whether human review is required.
  - Answers containing anomalous months are flagged
    ``human_review_required=True`` — a spike should be checked by a person
    before anyone acts on it.
"""

from __future__ import annotations

import hashlib
import os
import statistics
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from signalkit.data.nypd import MonthlyRecord, get_records
from signalkit.governance.decision_log import (
    DecisionCategory,
    DecisionEntry,
    DecisionLogger,
    GovernanceSummary,
    RiskCategory,
    summarise,
)

DETERMINISTIC_MODEL = "signal-stats-v1 (deterministic)"
DEFAULT_LOG_PATH = "logs/decisions.jsonl"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-chat"
DEFAULT_LLM_PROVIDER = "DeepSeek"
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
    by_law_category: dict[str, int] = Field(
        default_factory=dict, description="Complaint counts by FELONY / MISDEMEANOR / VIOLATION"
    )


class CompareQuery(BaseModel):
    """Compare one offence scope across all boroughs."""

    question: str = Field(default="", description="Free-text question, recorded in the audit log")
    offense: Optional[str] = Field(default=None, description="Offence filter, e.g. 'burglary'")
    months: int = Field(default=12, ge=2, le=24)


class BoroughSeries(BaseModel):
    borough: str
    monthly_counts: dict[str, int]
    total: int
    yoy_change_pct: Optional[float] = None
    trend_direction: Literal["rising", "falling", "flat"]
    anomalous_months: list[str] = Field(default_factory=list)


class CompareResult(BaseModel):
    window_start: str
    window_end: str
    offense_filter: Optional[str]
    series: list[BoroughSeries]
    narrative: str
    decision_id: str
    data_source: str
    model_used: str
    human_review_required: bool
    generated_at: datetime


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
    law_totals: dict[str, int] = {}
    for rec in records:
        if rec.month in monthly:
            offense_totals[rec.offense] = offense_totals.get(rec.offense, 0) + rec.count
            law_totals[rec.law_category] = law_totals.get(rec.law_category, 0) + rec.count
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
        by_law_category=dict(sorted(law_totals.items(), key=lambda kv: -kv[1])),
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


# Narrative cache: identical aggregates must never re-spend LLM tokens or
# re-pay latency. Keyed on (model, endpoint, prompt); FIFO-capped. Honest
# with governance — the cached words were still produced by the recorded
# model, just not twice.
NARRATIVE_CACHE_MAX = 256
_narrative_cache: dict[str, str] = {}
_narrative_cache_order: deque[str] = deque()
_narrative_cache_lock = threading.Lock()


def _llm_complete(prompt: str, model: str, base_url: str, api_key: str) -> str:
    """Call any OpenAI-compatible chat-completions API, with caching."""
    import httpx

    cache_key = hashlib.sha256(f"{model}|{base_url}|{prompt}".encode()).hexdigest()
    with _narrative_cache_lock:
        if cache_key in _narrative_cache:
            return _narrative_cache[cache_key]

    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            # Reasoning models (e.g. deepseek-v4-pro) spend tokens thinking
            # before answering; the budget must cover both or content
            # comes back empty.
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    narrative = (response.json()["choices"][0]["message"]["content"] or "").strip()
    if not narrative:
        # e.g. a reasoning model that exhausted its budget before answering.
        # An empty narrative must never reach a user; the caller falls back
        # to the deterministic template.
        raise ValueError("LLM returned an empty narrative.")

    with _narrative_cache_lock:
        if cache_key not in _narrative_cache:
            _narrative_cache[cache_key] = narrative
            _narrative_cache_order.append(cache_key)
            while len(_narrative_cache_order) > NARRATIVE_CACHE_MAX:
                _narrative_cache.pop(_narrative_cache_order.popleft(), None)
    return narrative


def _generate_narrative(prompt: str, fallback: str) -> tuple[str, str, Optional[str]]:
    """Produce a narrative, preferring the LLM when configured.

    Returns (narrative, model_used, provider). The prompt must contain
    computed aggregates only — never raw records.
    """
    api_key = os.environ.get("SIGNAL_LLM_API_KEY")
    if not api_key:
        return fallback, DETERMINISTIC_MODEL, None
    model = os.environ.get("SIGNAL_LLM_MODEL", DEFAULT_LLM_MODEL)
    base_url = os.environ.get("SIGNAL_LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
    try:
        narrative = _llm_complete(prompt, model, base_url, api_key)
    except Exception:
        return fallback, DETERMINISTIC_MODEL, None
    provider = os.environ.get("SIGNAL_LLM_PROVIDER", DEFAULT_LLM_PROVIDER)
    return narrative, model, provider


def _ask_prompt(stats: TrendStats, query: AnalystQuery) -> str:
    return (
        "You are a careful crime-data analyst. Using ONLY the statistics below, "
        "write a 3-4 sentence plain-English summary. Do not invent numbers.\n\n"
        f"Question: {query.question or '(trend summary)'}\n"
        f"Filters: offence={query.offense or 'all'}, borough={query.borough or 'all'}\n"
        f"Statistics: {stats.model_dump_json()}"
    )


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
        narrative, model_used, provider = _generate_narrative(
            _ask_prompt(stats, query), _template_narrative(stats, query)
        )
        human_review = bool(stats.anomalous_months)
        entry = DecisionEntry(
            model_name=model_used,
            model_provider=provider,
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

    def compare(self, query: CompareQuery) -> CompareResult:
        """One offence scope, all boroughs, aligned monthly series — audit-logged."""
        records, source_label = get_records(self._offline)
        filtered = [
            r
            for r in records
            if (not query.offense or query.offense.lower() in r.offense.lower())
            and r.borough and not r.borough.startswith("(")
        ]
        if not filtered:
            raise NoDataError(
                "No records match that offence filter.",
                suggestions={"offenses": sorted({r.offense for r in records})[:30]},
            )

        # Canonical window: last N months across the whole filtered scope, so
        # every borough's series is aligned (missing cells become 0).
        window = sorted({r.month for r in filtered})[-query.months:]
        series: list[BoroughSeries] = []
        for borough in sorted({r.borough for r in filtered}):
            subset = [r for r in filtered if r.borough == borough]
            stats = compute_stats(subset, query.months)
            counts = {m: stats.monthly_counts.get(m, 0) for m in window}
            series.append(
                BoroughSeries(
                    borough=borough,
                    monthly_counts=counts,
                    total=sum(counts.values()),
                    yoy_change_pct=stats.yoy_change_pct,
                    trend_direction=stats.trend_direction,
                    anomalous_months=stats.anomalous_months,
                )
            )
        series.sort(key=lambda s: -s.total)

        scope = f"offence matching '{query.offense}'" if query.offense else "all complaints"
        compact = {
            s.borough: {"total": s.total, "yoy_pct": s.yoy_change_pct, "trend": s.trend_direction}
            for s in series
        }
        fallback_parts = [
            f"Between {window[0]} and {window[-1]}, comparing {scope} across boroughs: "
            f"{series[0].borough} recorded the most complaints ({series[0].total:,}) and "
            f"{series[-1].borough} the fewest ({series[-1].total:,})."
        ]
        moves = [s for s in series if s.yoy_change_pct is not None]
        if moves:
            biggest = max(moves, key=lambda s: abs(s.yoy_change_pct))
            verb = "up" if biggest.yoy_change_pct >= 0 else "down"
            fallback_parts.append(
                f"Largest year-on-year movement: {biggest.borough}, "
                f"{verb} {abs(biggest.yoy_change_pct)}%."
            )
        prompt = (
            "You are a careful crime-data analyst. Using ONLY the per-borough statistics "
            "below, write a 3-4 sentence plain-English comparison. Do not invent numbers.\n\n"
            f"Question: {query.question or '(borough comparison)'}\n"
            f"Scope: {scope}, window {window[0]} to {window[-1]}\n"
            f"Per-borough statistics: {compact}"
        )
        narrative, model_used, provider = _generate_narrative(prompt, " ".join(fallback_parts))

        human_review = any(s.anomalous_months for s in series)
        entry = DecisionEntry(
            model_name=model_used,
            model_provider=provider,
            input_summary=(
                f"compare question='{query.question}' offense='{query.offense}' "
                f"months={query.months}"
            ),
            model_output_summary=narrative[:300],
            data_sources=[source_label],
            decision_made="Returned borough comparison to caller via Signal API.",
            decision_category=DecisionCategory.analytical,
            confidence_score=0.95 if model_used == DETERMINISTIC_MODEL else 0.8,
            human_review_required=human_review,
            legislative_basis=(
                "APS Mandatory AI Requirements (Jun 2026); EU AI Act Art. 50 transparency"
            ),
            risk_category=RiskCategory.limited,
            tags=["nypd-complaints", "borough-comparison"],
        )
        self._logger.log(entry)

        return CompareResult(
            window_start=window[0],
            window_end=window[-1],
            offense_filter=query.offense,
            series=series,
            narrative=narrative,
            decision_id=entry.decision_id,
            data_source=source_label,
            model_used=model_used,
            human_review_required=human_review,
            generated_at=datetime.now(timezone.utc),
        )

    def recent_decisions(self, limit: int = 20) -> list[DecisionEntry]:
        """Read back the most recent audit entries (newest last)."""
        return self._logger.read_all()[-limit:]

    def get_decision(self, decision_id: str) -> DecisionEntry | None:
        """Resolve a decision_id from an /ask response to its full audit entry."""
        for entry in self._logger.read_all():
            if entry.decision_id == decision_id:
                return entry
        return None

    def governance_summary(self) -> GovernanceSummary:
        """Aggregate the audit log: review rate, risk tiers, model breakdown."""
        return summarise(self._logger.read_all())
