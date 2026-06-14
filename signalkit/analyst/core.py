"""
signalkit/analyst/core.py
=========================
The analyst layer: turns a filtered slice of the recorded-offence data into
trend statistics and a plain-language narrative, and writes a governance
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
import re
import statistics
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from signalkit.data import catalogue, nyc, sa_crime
from signalkit.data.sa_crime import MonthlyRecord
from signalkit.governance.decision_log import (
    DecisionCategory,
    DecisionEntry,
    DecisionLogger,
    GovernanceSummary,
    RiskCategory,
    TransparencyStatement,
    UseCaseRegister,
    register,
    summarise,
    transparency_statement,
)

DEFAULT_AGENCY = "Signal (demo · South Australia Police context)"
DEFAULT_ACCOUNTABLE_OFFICIAL = "Accountable Official (demo)"

# Default (SA) record source. Kept as a module-level name so tests can patch it;
# _records_for routes "nyc" to the NYC layer and everything else through here.
get_records = sa_crime.get_records

SOURCE_AGENCY = {"sa": "SA Police", "nyc": "NYPD"}
SOURCE_REGION_WORD = {"sa": "regions", "nyc": "boroughs"}


def _records_for(source: str, offline):
    if source == "nyc":
        return nyc.get_records(offline)
    return get_records(offline)

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
    offense: Optional[str] = Field(default=None, description="Offence filter, e.g. 'theft'")
    region: Optional[str] = Field(default=None, description="Region/borough filter")
    months: int = Field(default=12, ge=2, le=24, description="Trailing window size in months")
    source: str = Field(default="sa", description="Data source: sa | nyc")


class TrendStats(BaseModel):
    """Deterministic statistics computed over the filtered window."""

    window_start: str
    window_end: str
    total_offences: int
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
    by_offense_division: dict[str, int] = Field(
        default_factory=dict,
        description="Counts by ANZSOC division (against the person / against property)",
    )


class CompareQuery(BaseModel):
    """Compare one offence scope across regions/boroughs."""

    question: str = Field(default="", description="Free-text question, recorded in the audit log")
    offense: Optional[str] = Field(default=None, description="Offence filter, e.g. 'theft'")
    months: int = Field(default=12, ge=2, le=24)
    source: str = Field(default="sa", description="Data source: sa | nyc")


_MONTH_NAMES = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


def _parse_month(value) -> Optional[str]:
    """Extract a sortable YYYY-MM period from many date/period formats.

    Handles ISO/timestamps, DD/MM/YYYY, quarter labels (Q1 2023, Q1-2023/2024 →
    quarter-start month), financial years (2023-24, 2023/2024 → the start year's
    July), and month names (Jul 2023). Returns None when nothing parses, so the
    analyser can fall back honestly rather than inventing a period."""
    s = str(value).strip() if value is not None else ""
    if not s:
        return None
    # ISO / timestamp YYYY-MM (validate the month so '2023-24' falls through)
    m = re.match(r"^(\d{4})-(\d{2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{m.group(2)}"
    # DD/MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(3)}-{int(m.group(2)):02d}"
    # Quarter label with a year: Q1 2023, 2023-Q1, Q1-2023/2024
    q = re.search(r"[Qq]([1-4])", s)
    y = re.search(r"(?:19|20)\d{2}", s)
    if q and y:
        return f"{y.group(0)}-{(int(q.group(1)) - 1) * 3 + 1:02d}"
    # Financial year: 2023-24, 2023/2024, 2023/24 → start year, July (AU FY)
    m = re.match(r"^(\d{4})[/\-](\d{2,4})$", s)
    if m:
        return f"{m.group(1)}-07"
    # Month name + year: Jul 2023, July-2023, Jul-Sep 2023 (first month)
    m = re.search(r"\b([A-Za-z]{3,9})\b", s)
    if m and y:
        mon = _MONTH_NAMES.get(m.group(1)[:3].lower())
        if mon:
            return f"{y.group(0)}-{mon:02d}"
    return None


def _to_float(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _infer_columns(fields: list[dict], rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Find a date/period column and a numeric column by sampling values."""
    sample = rows[:200]
    if not sample:
        return None, None
    ids = [f["id"] for f in fields]
    date_field = next(
        (fid for fid in ids
         if sum(1 for r in sample if _parse_month(r.get(fid))) / len(sample) >= 0.7),
        None,
    )
    value_field = next(
        (fid for fid in ids
         if fid != date_field
         and sum(1 for r in sample if _to_float(r.get(fid)) is not None) / len(sample) >= 0.7),
        None,
    )
    return date_field, value_field


class GenericTrend(BaseModel):
    """Trend statistics over an arbitrary data.sa dataset (date + numeric)."""

    date_field: str
    value_field: str
    window_start: str
    window_end: str
    total: float
    monthly_counts: dict[str, float]
    trend_direction: Literal["rising", "falling", "flat"]
    mom_change_pct: Optional[float] = None
    yoy_change_pct: Optional[float] = None
    anomalous_months: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """A human review recorded against a prior decision."""

    reviewer: str = Field(min_length=1, description="Who reviewed (email or role)")
    override: bool = Field(default=False, description="True if the human overrode the result")
    override_reason: Optional[str] = Field(
        default=None, description="Required when override is true"
    )
    note: Optional[str] = Field(default=None, description="Optional free-text note")

    @model_validator(mode="after")
    def _reason_required_when_override(self) -> "ReviewRequest":
        if self.override and not (self.override_reason and self.override_reason.strip()):
            raise ValueError("override_reason is required when override is true.")
        return self


class AnalyseRequest(BaseModel):
    """Request to combine and trend one or more catalogue resources."""

    resource_ids: list[str] = Field(min_length=1, description="data store resource ids to combine")
    title: str = Field(default="", description="Label for the combined dataset")
    portal: str = Field(default="sa", description="Portal key: sa | nsw | vic")


class RegionSeries(BaseModel):
    region: str
    monthly_counts: dict[str, int]
    total: int
    yoy_change_pct: Optional[float] = None
    trend_direction: Literal["rising", "falling", "flat"]
    anomalous_months: list[str] = Field(default_factory=list)


class CompareResult(BaseModel):
    window_start: str
    window_end: str
    offense_filter: Optional[str]
    series: list[RegionSeries]
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
    division_totals: dict[str, int] = {}
    for rec in records:
        if rec.month in monthly:
            offense_totals[rec.offense] = offense_totals.get(rec.offense, 0) + rec.count
            division_totals[rec.offense_division] = (
                division_totals.get(rec.offense_division, 0) + rec.count
            )
    top = sorted(offense_totals.items(), key=lambda kv: -kv[1])[:5]

    return TrendStats(
        window_start=window[0],
        window_end=window[-1],
        total_offences=sum(series),
        monthly_counts=monthly,
        mom_change_pct=mom,
        yoy_change_pct=yoy,
        trend_direction=direction,
        anomalous_months=anomalies,
        top_offenses=[{"offense": o, "count": c} for o, c in top],
        by_offense_division=dict(sorted(division_totals.items(), key=lambda kv: -kv[1])),
    )


def _template_narrative(stats: TrendStats, query: AnalystQuery) -> str:
    scope_bits = []
    if query.offense:
        scope_bits.append(f"matching '{query.offense}'")
    if query.region:
        scope_bits.append(f"in {query.region.upper()}")
    scope = " ".join(scope_bits) if scope_bits else "in total"
    agency = SOURCE_AGENCY.get(query.source, "SA Police")

    parts = [
        f"Between {stats.window_start} and {stats.window_end}, {agency} recorded "
        f"{stats.total_offences:,} offences {scope}.",
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
    agency = SOURCE_AGENCY.get(query.source, "SA Police")
    return (
        f"You are a careful crime-data analyst working with {agency} data. Using ONLY the "
        "statistics below, write a 3-4 sentence plain-English summary. Do not invent "
        "numbers.\n\n"
        f"Question: {query.question or '(trend summary)'}\n"
        f"Filters: offence={query.offense or 'all'}, region={query.region or 'all'}\n"
        f"Statistics: {stats.model_dump_json()}"
    )


class Analyst:
    """Answers queries over the recorded-offence data and audit-logs every answer."""

    def __init__(self, log_path: str | None = None, offline: bool | None = None):
        self._logger = DecisionLogger(
            log_path or os.environ.get("SIGNAL_LOG_PATH", DEFAULT_LOG_PATH)
        )
        self._offline = offline
        self._agency = os.environ.get("SIGNAL_AGENCY", DEFAULT_AGENCY)
        self._accountable_official = os.environ.get(
            "SIGNAL_ACCOUNTABLE_OFFICIAL", DEFAULT_ACCOUNTABLE_OFFICIAL
        )

    def ask(self, query: AnalystQuery) -> AnalystAnswer:
        records, source_label = _records_for(query.source, self._offline)

        filtered = [
            r
            for r in records
            if (not query.offense or query.offense.lower() in r.offense.lower())
            and (not query.region or query.region.lower() in r.region.lower())
        ]
        if not filtered:
            raise NoDataError(
                "No records match those filters.",
                suggestions={
                    "regions": sorted({r.region for r in records}),
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
                f"source='{query.source}' question='{query.question}' offense='{query.offense}' "
                f"region='{query.region}' months={query.months}"
            ),
            model_output_summary=narrative[:300],
            data_sources=[source_label],
            decision_made="Returned trend analysis to caller via Signal API.",
            decision_category=DecisionCategory.analytical,
            use_case="Crime trend analysis",
            confidence_score=0.95 if model_used == DETERMINISTIC_MODEL else 0.8,
            human_review_required=human_review,
            legislative_basis=(
                "Policy for the responsible use of AI in government (DTA v2.0); EU AI Act Art. 50"
            ),
            risk_category=RiskCategory.limited,
            tags=[f"{query.source}-crime", "trend-analysis"],
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
        """One offence scope, all SA regions, aligned monthly series — audit-logged."""
        records, source_label = _records_for(query.source, self._offline)
        # The folded tail, withheld-suburb and unknown-borough buckets are not
        # single places, so they are excluded from a region-versus-region compare.
        excluded = {"OTHER SA AREAS", "NOT DISCLOSED", "(null)"}
        filtered = [
            r
            for r in records
            if (not query.offense or query.offense.lower() in r.offense.lower())
            and r.region and r.region not in excluded
        ]
        if not filtered:
            raise NoDataError(
                "No records match that offence filter.",
                suggestions={"offenses": sorted({r.offense for r in records})[:30]},
            )

        # Canonical window: last N months across the whole filtered scope, so
        # every region's series is aligned (missing cells become 0).
        window = sorted({r.month for r in filtered})[-query.months:]
        series: list[RegionSeries] = []
        for region in sorted({r.region for r in filtered}):
            subset = [r for r in filtered if r.region == region]
            stats = compute_stats(subset, query.months)
            counts = {m: stats.monthly_counts.get(m, 0) for m in window}
            series.append(
                RegionSeries(
                    region=region,
                    monthly_counts=counts,
                    total=sum(counts.values()),
                    yoy_change_pct=stats.yoy_change_pct,
                    trend_direction=stats.trend_direction,
                    anomalous_months=stats.anomalous_months,
                )
            )
        series.sort(key=lambda s: -s.total)

        scope = f"offence matching '{query.offense}'" if query.offense else "all offences"
        compact = {
            s.region: {"total": s.total, "yoy_pct": s.yoy_change_pct, "trend": s.trend_direction}
            for s in series
        }
        region_word = SOURCE_REGION_WORD.get(query.source, "regions")
        fallback_parts = [
            f"Between {window[0]} and {window[-1]}, comparing {scope} across {region_word}: "
            f"{series[0].region} recorded the most offences ({series[0].total:,}) and "
            f"{series[-1].region} the fewest ({series[-1].total:,})."
        ]
        moves = [s for s in series if s.yoy_change_pct is not None]
        if moves:
            biggest = max(moves, key=lambda s: abs(s.yoy_change_pct))
            verb = "up" if biggest.yoy_change_pct >= 0 else "down"
            fallback_parts.append(
                f"Largest year-on-year movement: {biggest.region}, "
                f"{verb} {abs(biggest.yoy_change_pct)}%."
            )
        prompt = (
            f"You are a careful crime-data analyst working with {SOURCE_AGENCY.get(query.source, 'SA Police')} "
            f"data. Using ONLY the per-{region_word[:-1]} statistics below, write a 3-4 sentence "
            "plain-English comparison. Do not invent numbers.\n\n"
            f"Question: {query.question or '(comparison)'}\n"
            f"Scope: {scope}, window {window[0]} to {window[-1]}\n"
            f"Statistics: {compact}"
        )
        narrative, model_used, provider = _generate_narrative(prompt, " ".join(fallback_parts))

        human_review = any(s.anomalous_months for s in series)
        entry = DecisionEntry(
            model_name=model_used,
            model_provider=provider,
            input_summary=(
                f"compare source='{query.source}' question='{query.question}' "
                f"offense='{query.offense}' months={query.months}"
            ),
            model_output_summary=narrative[:300],
            data_sources=[source_label],
            decision_made="Returned region comparison to caller via Signal API.",
            decision_category=DecisionCategory.analytical,
            use_case="Regional crime comparison",
            confidence_score=0.95 if model_used == DETERMINISTIC_MODEL else 0.8,
            human_review_required=human_review,
            legislative_basis=(
                "Policy for the responsible use of AI in government (DTA v2.0); EU AI Act Art. 50"
            ),
            risk_category=RiskCategory.limited,
            tags=[f"{query.source}-crime", "region-comparison"],
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

    def record_review(self, decision_id: str, req: ReviewRequest) -> DecisionEntry | None:
        """Record a human review of a prior decision as its own audit event.

        Returns None if the decision is unknown. The review is appended (the log
        is never mutated), references the original via ``reviews_decision_id``,
        and closes the loop on the 'whether a human reviewed it' requirement.
        """
        original = self.get_decision(decision_id)
        if original is None:
            return None
        verb = "Overrode" if req.override else "Confirmed"
        entry = DecisionEntry(
            model_name="human-review (manual)",
            model_provider=None,
            input_summary=f"Human review of decision {decision_id}",
            model_output_summary=(req.note or f"{verb} by {req.reviewer}.")[:300],
            data_sources=original.data_sources,
            decision_made=f"{verb} decision {decision_id}.",
            decision_category=DecisionCategory.review,
            use_case="Human review of AI decision",
            human_review_required=False,
            human_reviewer=req.reviewer,
            override_applied=req.override,
            override_reason=req.override_reason,
            reviews_decision_id=decision_id,
            legislative_basis="Policy for the responsible use of AI in government (DTA v2.0) — human oversight",
            risk_category=RiskCategory.minimal,
            tags=["human-review", decision_id],
            notes=req.note,
        )
        self._logger.log(entry)
        return entry

    def governance_summary(self) -> GovernanceSummary:
        """Aggregate the audit log: review rate, risk tiers, model breakdown."""
        return summarise(self._logger.read_all())

    def use_case_register(self) -> UseCaseRegister:
        """The DTA-style register of in-scope AI use cases, live from the log."""
        return register(self._logger.read_all(), self._agency, self._accountable_official)

    def transparency(self) -> TransparencyStatement:
        """A DTA-style AI transparency statement, generated from the log."""
        return transparency_statement(
            self._logger.read_all(), self._agency, self._accountable_official
        )

    def preview_dataset(
        self, resource_id: str, dataset_title: str = "", limit: int = 20, portal: str = "sa"
    ) -> dict:
        """Preview an open-data datastore resource, logging the lookup.

        Even an ad-hoc data preview is a data-provenance event, so it gets its
        own audit entry (retrieval category) — the explorer is governed too.
        """
        preview = catalogue.preview_resource(resource_id, limit, portal)
        scope = f" — {dataset_title}" if dataset_title else ""
        portal_base = catalogue.PORTALS.get(portal, portal)
        entry = DecisionEntry(
            model_name="catalogue preview (no model)",
            model_provider=None,
            input_summary=f"Preview of {portal} resource {resource_id}{scope}",
            model_output_summary=(
                f"Returned {len(preview.records)} of {preview.total} rows, "
                f"{len(preview.fields)} columns."
            ),
            data_sources=[f"{portal_base} resource {resource_id}{scope}"],
            decision_made="Returned a catalogue data preview via Signal API.",
            decision_category=DecisionCategory.retrieval,
            use_case="Open-data catalogue preview",
            human_review_required=False,
            legislative_basis="Policy for the responsible use of AI in government (DTA v2.0) — provenance",
            risk_category=RiskCategory.minimal,
            tags=[portal, "catalogue-preview"],
        )
        self._logger.log(entry)
        out = preview.model_dump()
        out["decision_id"] = entry.decision_id
        return out

    def analyse_resource(
        self,
        resource_id: str,
        title: str = "",
        date_field: str | None = None,
        value_field: str | None = None,
        max_rows: int = 5000,
        portal: str = "sa",
    ) -> dict:
        """Run the trend engine on an arbitrary catalogue resource, if it has a
        date and a numeric column. Falls back honestly when it cannot, rather
        than forcing a misleading trend. Every analysis is audit-logged."""
        try:
            fields, rows = catalogue.fetch_rows(resource_id, max_rows, portal)
        except Exception:
            return {
                "analysable": False,
                "reason": "This resource has no queryable data table to analyse.",
            }
        src = f"{catalogue.PORTALS.get(portal, portal)} resource {resource_id}" + (
            f" ({title})" if title else ""
        )
        return self._analyse_and_log(
            fields, rows, title or f"resource {resource_id}", portal, [src],
            date_field, value_field, source_count=1, truncated=len(rows) >= max_rows,
        )

    def analyse_resources(
        self, resource_ids: list[str], title: str = "", portal: str = "sa", max_rows_each: int = 20000
    ) -> dict:
        """Combine several catalogue resources into one series and trend them.

        Each resource is fetched (row-capped) and concatenated, then analysed as
        one dataset — e.g. several financial-year files become one long trend.
        Large datasets are sampled at the cap; the result flags that honestly."""
        all_fields: list[dict] = []
        all_rows: list[dict] = []
        used: list[str] = []
        truncated = False
        for rid in (resource_ids or [])[:12]:
            try:
                fields, rows = catalogue.fetch_rows(rid, max_rows_each, portal)
            except Exception:
                continue
            if not all_fields and fields:
                all_fields = fields
            all_rows.extend(rows)
            used.append(rid)
            if len(rows) >= max_rows_each:
                truncated = True
        if not all_rows:
            return {"analysable": False, "reason": "None of the selected resources have a queryable table."}
        base = catalogue.PORTALS.get(portal, portal)
        sources = [f"{base} resource {r}" for r in used]
        label = title or f"{len(used)} {portal.upper()} datasets"
        return self._analyse_and_log(
            all_fields, all_rows, label, portal, sources,
            None, None, source_count=len(used), truncated=truncated,
        )

    def _analyse_and_log(
        self, fields, rows, scope, portal, sources, date_field, value_field, source_count, truncated
    ) -> dict:
        """Shared core: infer columns, build a monthly series, trend it, log it."""
        inferred_d, inferred_v = _infer_columns(fields, rows)
        date_field = date_field or inferred_d
        value_field = value_field or inferred_v
        if not date_field or not value_field:
            return {
                "analysable": False,
                "reason": "No date + numeric column detected in this dataset.",
                "fields": fields,
            }

        monthly: dict[str, float] = {}
        for r in rows:
            month = _parse_month(r.get(date_field))
            x = _to_float(r.get(value_field))
            if month and x is not None:
                monthly[month] = monthly.get(month, 0.0) + x
        months = sorted(monthly)
        if len(months) < 3:
            return {
                "analysable": False,
                "reason": f"Only {len(months)} month(s) of data — at least 3 are needed to trend.",
                "date_field": date_field,
                "value_field": value_field,
            }

        series = [monthly[m] for m in months]
        mom = None
        if len(series) >= 2 and series[-2] != 0:
            mom = round((series[-1] - series[-2]) / series[-2] * 100, 1)
        yoy = None
        last = months[-1]
        prior = f"{int(last[:4]) - 1}-{last[5:]}"
        if prior in monthly and monthly[prior] != 0:
            yoy = round((monthly[last] - monthly[prior]) / monthly[prior] * 100, 1)
        mean = statistics.mean(series)
        slope = _slope(series)
        direction = "flat"
        if mean and abs(slope) / abs(mean) >= TREND_SLOPE_THRESHOLD:
            direction = "rising" if slope > 0 else "falling"
        anomalies = []
        if len(series) >= 6:
            stdev = statistics.pstdev(series)
            if stdev > 0:
                anomalies = [m for m, v in monthly.items() if abs(v - mean) / stdev >= ANOMALY_Z_THRESHOLD]

        stats = GenericTrend(
            date_field=date_field,
            value_field=value_field,
            window_start=months[0],
            window_end=months[-1],
            total=round(sum(series), 2),
            monthly_counts={m: round(monthly[m], 2) for m in months},
            trend_direction=direction,
            mom_change_pct=mom,
            yoy_change_pct=yoy,
            anomalous_months=anomalies,
        )
        across = f" across {source_count} datasets" if source_count > 1 else ""
        sample_note = " (based on a capped sample of each dataset)" if truncated else ""
        template = (
            f"Between {months[0]} and {months[-1]}, the monthly total of "
            f"'{value_field}'{across} in {scope} is {direction}. The series spans {len(months)} "
            f"months{sample_note}."
            + (f" Anomalous months flagged for review: {', '.join(anomalies)}." if anomalies else "")
        )
        prompt = (
            "You are a careful data analyst. Using ONLY these statistics, write a 2-3 sentence "
            "plain-English summary of the monthly trend. Do not invent numbers.\n\n"
            f"Dataset: {scope} ({source_count} source(s)); metric: monthly total of '{value_field}'.\n"
            f"Statistics: {stats.model_dump_json()}"
        )
        narrative, model_used, provider = _generate_narrative(prompt, template)
        human_review = bool(anomalies)
        tags = [portal, "generic-analysis"] + (["multi-dataset"] if source_count > 1 else [])
        entry = DecisionEntry(
            model_name=model_used,
            model_provider=provider,
            input_summary=(
                f"Generic trend of '{value_field}' by '{date_field}'{across} in {scope}"
            ),
            model_output_summary=narrative[:300],
            data_sources=sources,
            decision_made="Returned a generic trend analysis via Signal API.",
            decision_category=DecisionCategory.analytical,
            use_case="Open-data trend analysis",
            confidence_score=0.95 if model_used == DETERMINISTIC_MODEL else 0.8,
            human_review_required=human_review,
            legislative_basis="Policy for the responsible use of AI in government (DTA v2.0); EU AI Act Art. 50",
            risk_category=RiskCategory.limited,
            tags=tags,
        )
        self._logger.log(entry)
        return {
            "analysable": True,
            "stats": stats.model_dump(),
            "narrative": narrative,
            "model_used": model_used,
            "decision_id": entry.decision_id,
            "human_review_required": human_review,
            "resource_count": source_count,
            "rows_analysed": len(rows),
            "truncated": truncated,
        }
