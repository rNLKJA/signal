"""
signalkit/governance/decision_log.py
====================================
Structured audit trail for AI-assisted decisions in Signal.

Aligned to the Australian Government's **Policy for the responsible use of AI in
government** (Digital Transformation Agency, Version 2.0, published 1 Dec 2025).
The policy's mandatory requirements phase in from **15 June 2026**, with
mandatory AI use-case impact assessments by **15 December 2026**. Signal
implements the policy's core mandatory artifacts in running code:

  - **Accountable official & use-case owner** — ``human_reviewer``, ``officer_id``,
    ``agency`` record who is accountable for each AI-assisted decision.
  - **Use-case register** — the log *is* the register of in-scope AI use cases;
    ``register()`` rolls it up per use case (owner, risk, review rate).
  - **AI transparency statement** — ``transparency_statement()`` generates a
    DTA-style statement from the log (what AI, for what, oversight, data, risk).
  - **AI use-case impact assessment** — ``risk_category``, ``confidence_score``,
    the input/output summaries and ``data_sources`` map to the DTA assessment.

Also informed by the **EU AI Act** (2024/1689) risk tiers and the **Privacy Act
1988 (Cth)** automated decision-making disclosure reforms.

Usage
-----
    from signalkit.governance.decision_log import DecisionEntry, DecisionLogger

    logger = DecisionLogger("logs/decisions.jsonl")

    entry = DecisionEntry(
        decision_id="d-001",
        model_name="gpt-4o",
        model_version="2025-11-01",
        input_summary="Quarterly crime trend query for SA region",
        model_output_summary="Trend shows 12% increase in property offences Q1→Q2",
        decision_made="Escalate finding to senior analyst for review",
        decision_category="analytical",
        data_sources=["SAPOL IAPro export 2026-Q1", "ABS crime stats 2025"],
        confidence_score=0.87,
        human_review_required=True,
        human_reviewer="analyst@example.gov.au",
        agency="South Australia Police",
        legislative_basis="Policy for the responsible use of AI in government (DTA v2.0)",
        risk_category="limited",
        tags=["crime-trends", "property-offences", "SA"],
    )

    logger.log(entry)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DecisionCategory(str, Enum):
    """Broad category of the AI-assisted decision."""
    analytical   = "analytical"    # data analysis, trend detection, summarisation
    operational  = "operational"   # workflow routing, triage, prioritisation
    generative   = "generative"    # content creation, drafting, code generation
    retrieval    = "retrieval"     # information lookup, RAG responses
    classification = "classification"  # labelling, tagging, risk scoring
    review       = "review"        # a human review recorded against a prior decision
    other        = "other"


class RiskCategory(str, Enum):
    """
    EU AI Act risk tiers (Art. 6–7) and APS risk framing.
    Assign the highest applicable tier.
    """
    unacceptable = "unacceptable"  # prohibited under EU AI Act Art. 5
    high         = "high"          # high-risk systems (Annex III) — requires logging
    limited      = "limited"       # limited risk — transparency obligations apply
    minimal      = "minimal"       # minimal risk — no specific obligations


# ---------------------------------------------------------------------------
# Core schema
# ---------------------------------------------------------------------------

class DecisionEntry(BaseModel):
    """
    A single AI-assisted decision event.

    Every field maps to at least one compliance requirement. Fields marked
    [DTA] satisfy the Policy for the responsible use of AI in government (v2.0).
    Fields marked [EU] satisfy EU AI Act traceability obligations. Fields marked
    [Privacy] satisfy the Privacy Act 1988 automated decision-making disclosure.
    """

    # --- Identity ---
    decision_id: str = Field(
        default_factory=lambda: f"d-{uuid.uuid4().hex[:8]}",
        description="Unique identifier for this decision event. [DTA] [EU] [Privacy]",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the decision. [DTA] [EU]",
    )

    # --- Model provenance ---
    model_name: str = Field(
        description="Name of the AI model used (e.g. 'gpt-4o', 'claude-sonnet-4-6'). [DTA]",
    )
    model_version: Optional[str] = Field(
        default=None,
        description="Model version or release date string. [DTA] [EU]",
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Provider of the model (e.g. 'OpenAI', 'Anthropic'). [DTA]",
    )

    # --- Input / output ---
    input_summary: str = Field(
        description="Plain-language summary of the input given to the model. "
                    "Do not include raw PII. [DTA] [Privacy]",
    )
    model_output_summary: str = Field(
        description="Plain-language summary of the model's output. [DTA] [EU]",
    )
    data_sources: list[str] = Field(
        default_factory=list,
        description="List of datasets or sources that informed the query. [DTA] [Privacy]",
    )

    # --- Decision ---
    decision_made: str = Field(
        description="The actual decision or action taken on the basis of the model output. [DTA] [Privacy]",
    )
    decision_category: DecisionCategory = Field(
        default=DecisionCategory.analytical,
        description="Broad category of this decision. [DTA]",
    )
    use_case: Optional[str] = Field(
        default=None,
        description="Named AI use case this decision belongs to, for the DTA "
                    "use-case register (e.g. 'Crime trend analysis'). [DTA]",
    )
    confidence_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Model or analyst confidence in the output (0.0–1.0). [EU]",
    )

    # --- Human oversight ---
    human_review_required: bool = Field(
        description="Whether human review was required before acting on this output. [DTA] [EU]",
    )
    human_reviewer: Optional[str] = Field(
        default=None,
        description="Identifier (email or role) of the human reviewer, if applicable. [DTA]",
    )
    override_applied: bool = Field(
        default=False,
        description="True if a human overrode the model output. [EU]",
    )
    override_reason: Optional[str] = Field(
        default=None,
        description="Reason for override, if override_applied is True. [EU]",
    )
    reviews_decision_id: Optional[str] = Field(
        default=None,
        description="For a review event: the decision_id this review applies to. [DTA]",
    )

    # --- Governance context ---
    agency: Optional[str] = Field(
        default=None,
        description="Government agency or organisation responsible for this decision. [DTA]",
    )
    officer_id: Optional[str] = Field(
        default=None,
        description="Anonymised ID of the officer or user who initiated the query. [DTA] [Privacy]",
    )
    legislative_basis: Optional[str] = Field(
        default=None,
        description="Policy or legislative reference authorising AI use in this context. [DTA]",
    )
    risk_category: RiskCategory = Field(
        default=RiskCategory.limited,
        description="EU AI Act risk tier assigned to this use case. [EU]",
    )

    # --- Metadata ---
    tags: list[str] = Field(
        default_factory=list,
        description="Free-form tags for filtering and analysis.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Any additional context not captured by the structured fields.",
    )

    @field_validator("override_reason")
    @classmethod
    def override_reason_required_when_overridden(cls, v: Optional[str], info) -> Optional[str]:
        if info.data.get("override_applied") and not v:
            raise ValueError("override_reason must be provided when override_applied is True.")
        return v

    def to_jsonl_line(self) -> str:
        """Serialise to a single JSON line for JSONL append."""
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Summary analytics
# ---------------------------------------------------------------------------

class GovernanceSummary(BaseModel):
    """Aggregate view of the audit log — the governance posture, quantified."""

    total_decisions: int
    human_review_required_count: int
    human_review_rate: Optional[float] = Field(
        default=None, description="Fraction of decisions flagged for human review (0–1)."
    )
    reviews_recorded: int = Field(
        default=0, description="Number of human-review events logged against decisions."
    )
    outstanding_reviews: int = Field(
        default=0,
        description="Decisions flagged for review with no review recorded yet.",
    )
    by_risk_category: dict[str, int]
    by_model: dict[str, int]
    by_decision_category: dict[str, int]
    first_decision_at: Optional[datetime] = None
    last_decision_at: Optional[datetime] = None


def summarise(entries: list[DecisionEntry]) -> GovernanceSummary:
    """Compute the governance summary over a list of audit entries.

    Human-review events (category ``review``) are counted separately from the
    decisions they review, so they never inflate the decision totals — the
    decision rates stay honest while the review activity is still surfaced.
    """
    reviews = [e for e in entries if e.decision_category == DecisionCategory.review]
    decisions = [e for e in entries if e.decision_category != DecisionCategory.review]
    reviewed_ids = {e.reviews_decision_id for e in reviews if e.reviews_decision_id}

    by_risk: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_category: dict[str, int] = {}
    review_required = 0
    outstanding = 0
    for e in decisions:
        by_risk[e.risk_category.value] = by_risk.get(e.risk_category.value, 0) + 1
        by_model[e.model_name] = by_model.get(e.model_name, 0) + 1
        by_category[e.decision_category.value] = by_category.get(e.decision_category.value, 0) + 1
        if e.human_review_required:
            review_required += 1
            if e.decision_id not in reviewed_ids:
                outstanding += 1
    return GovernanceSummary(
        total_decisions=len(decisions),
        human_review_required_count=review_required,
        human_review_rate=round(review_required / len(decisions), 3) if decisions else None,
        reviews_recorded=len(reviews),
        outstanding_reviews=outstanding,
        by_risk_category=by_risk,
        by_model=by_model,
        by_decision_category=by_category,
        first_decision_at=min((e.timestamp for e in entries), default=None),
        last_decision_at=max((e.timestamp for e in entries), default=None),
    )


# ---------------------------------------------------------------------------
# DTA Policy v2.0 artifacts: use-case register & transparency statement
# ---------------------------------------------------------------------------

class UseCaseEntry(BaseModel):
    """One row of the AI use-case register (DTA Policy v2.0)."""

    use_case: str
    decisions: int
    risk_categories: dict[str, int]
    models: list[str]
    accountable_reviewers: list[str]
    human_review_rate: Optional[float]
    data_sources: list[str]
    first_used: Optional[datetime] = None
    last_used: Optional[datetime] = None


class UseCaseRegister(BaseModel):
    """Register of in-scope AI use cases — the artifact the DTA policy requires
    each agency to maintain, generated live from the decision log."""

    agency: str
    accountable_official: str
    policy: str = "Policy for the responsible use of AI in government (DTA, v2.0)"
    use_cases: list[UseCaseEntry]


class TransparencyStatement(BaseModel):
    """An AI transparency statement in the DTA style, generated from the log."""

    agency: str
    accountable_official: str
    policy: str = "Policy for the responsible use of AI in government (DTA, v2.0)"
    ai_systems: list[str]
    purposes: list[str]
    data_sources: list[str]
    risk_tiers: dict[str, int]
    human_oversight: str
    public_access: str
    statement: str


def _use_case_of(e: "DecisionEntry") -> str:
    return e.use_case or e.decision_category.value.title()


def register(entries: list[DecisionEntry], agency: str, accountable_official: str) -> UseCaseRegister:
    """Roll the decision log up into an AI use-case register."""
    groups: dict[str, list[DecisionEntry]] = {}
    for e in entries:
        groups.setdefault(_use_case_of(e), []).append(e)
    rows = []
    for use_case, es in sorted(groups.items()):
        risks: dict[str, int] = {}
        for e in es:
            risks[e.risk_category.value] = risks.get(e.risk_category.value, 0) + 1
        reviewers = sorted({e.human_reviewer for e in es if e.human_reviewer})
        sources = sorted({s for e in es for s in e.data_sources})
        needs = [e for e in es if e.human_review_required]
        rows.append(UseCaseEntry(
            use_case=use_case,
            decisions=len(es),
            risk_categories=risks,
            models=sorted({e.model_name for e in es}),
            accountable_reviewers=reviewers,
            human_review_rate=round(len(needs) / len(es), 3) if es else None,
            data_sources=sources[:10],
            first_used=min((e.timestamp for e in es), default=None),
            last_used=max((e.timestamp for e in es), default=None),
        ))
    rows.sort(key=lambda r: -r.decisions)
    return UseCaseRegister(agency=agency, accountable_official=accountable_official, use_cases=rows)


def transparency_statement(
    entries: list[DecisionEntry], agency: str, accountable_official: str
) -> TransparencyStatement:
    """Generate an AI transparency statement from the decision log."""
    systems = sorted({e.model_name for e in entries})
    purposes = sorted({_use_case_of(e) for e in entries})
    sources = sorted({s for e in entries for s in e.data_sources})[:12]
    risks: dict[str, int] = {}
    for e in entries:
        risks[e.risk_category.value] = risks.get(e.risk_category.value, 0) + 1
    review_required = sum(1 for e in entries if e.human_review_required)
    oversight = (
        "Outputs are reviewable by a human. Statistically anomalous results are "
        "flagged for human review before action, and reviews/overrides are "
        f"recorded against the decision. {review_required} of {len(entries)} logged "
        "decisions were flagged for human review."
    )
    public = (
        "Every AI-assisted answer carries a decision_id, and the full audit trail "
        "is a public endpoint (GET /decisions). The use-case register and this "
        "statement are generated live from that log."
    )
    statement = (
        f"# AI transparency statement\n\n"
        f"**Agency:** {agency}\n\n"
        f"**Accountable official:** {accountable_official}\n\n"
        f"**Policy basis:** Policy for the responsible use of AI in government "
        f"(DTA, v2.0); mandatory requirements from 15 June 2026.\n\n"
        f"## What AI we use\n{', '.join(systems) or 'None recorded yet'}.\n\n"
        f"## What we use it for\n{', '.join(purposes) or 'None recorded yet'}.\n\n"
        f"## What data informs it\n{', '.join(sources) or 'None recorded yet'}.\n\n"
        f"## Risk classification (EU AI Act tiers)\n"
        f"{', '.join(f'{k}: {v}' for k, v in sorted(risks.items())) or 'None recorded yet'}.\n\n"
        f"## Human oversight\n{oversight}\n\n"
        f"## Public access and accountability\n{public}\n"
    )
    return TransparencyStatement(
        agency=agency, accountable_official=accountable_official,
        ai_systems=systems, purposes=purposes, data_sources=sources,
        risk_tiers=risks, human_oversight=oversight, public_access=public,
        statement=statement,
    )


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class DecisionLogger:
    """
    Append-only JSONL logger for DecisionEntry records.

    JSONL format: one JSON object per line, UTF-8, no trailing comma.
    Human-readable and grep-able without special tooling.

    Example
    -------
        logger = DecisionLogger("logs/decisions.jsonl")
        logger.log(entry)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, entry: DecisionEntry) -> None:
        """Append a DecisionEntry to the JSONL file."""
        with self.path.open("a", encoding="utf-8") as f:
            f.write(entry.to_jsonl_line() + "\n")

    def read_all(self) -> list[DecisionEntry]:
        """Read and parse all entries from the log file."""
        if not self.path.exists():
            return []
        entries = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(DecisionEntry.model_validate_json(line))
        return entries

    def to_dicts(self) -> list[dict]:
        """Return all entries as plain dicts (e.g. for pandas or DuckDB)."""
        return [json.loads(e.to_jsonl_line()) for e in self.read_all()]


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        log_path = tmp.name

    logger = DecisionLogger(log_path)

    entry = DecisionEntry(
        model_name="claude-sonnet-4-6",
        model_version="2026-06-01",
        model_provider="Anthropic",
        input_summary="Summarise Q1 2026 property crime trends for SA region.",
        model_output_summary="Property offences up 12% QoQ. Hotspots: Adelaide CBD, Port Adelaide.",
        decision_made="Flagged for senior analyst review and inclusion in Q1 brief.",
        decision_category=DecisionCategory.analytical,
        data_sources=["SAPOL IAPro 2026-Q1 export", "ABS Crime Statistics 2025"],
        confidence_score=0.91,
        human_review_required=True,
        human_reviewer="senior.analyst@sapol.sa.gov.au",
        agency="South Australia Police",
        legislative_basis="APS Mandatory AI Requirements v1.0 — Requirement 3 (June 2026)",
        risk_category=RiskCategory.limited,
        tags=["crime-trends", "property-offences", "SA", "Q1-2026"],
    )

    logger.log(entry)
    print("Logged entry:")
    print(json.dumps(json.loads(entry.to_jsonl_line()), indent=2))
    print(f"\nLog file: {log_path}")
    print(f"Total entries: {len(logger.read_all())}")
