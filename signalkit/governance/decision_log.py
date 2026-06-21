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

import hashlib
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
    parent_decision_id: Optional[str] = Field(
        default=None,
        description="For a sub-decision in a multi-step answer: the composite decision it "
                    "belongs to. Gives per-step accountability for agentic reasoning. [DTA/EU]",
    )
    child_decision_ids: list[str] = Field(
        default_factory=list,
        description="For a composite decision: the sub-decisions it synthesises. [DTA/EU]",
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
    faithfulness_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Faithfulness of the served narrative to the computed statistics "
            "(1.0 = no fabricated figures, no trend contradiction). None for "
            "non-narrated entries. [DTA/EU]"
        ),
    )

    # --- Integrity (tamper-evidence) ---
    # Each entry carries the hash of the one before it, so the log forms a chain.
    # Any retroactive edit or deletion breaks the chain and is detectable. This
    # turns the audit trail from "we wrote it down" into "we can show it was not
    # changed" — the traceability the DTA policy and the EU AI Act ask for. These
    # are set by DecisionLogger.log() at append time, not by the caller.
    prev_hash: Optional[str] = Field(
        default=None,
        description="entry_hash of the previous logged entry ('genesis' for the first). [EU]",
    )
    entry_hash: Optional[str] = Field(
        default=None,
        description="SHA-256 over this entry's content (incl. prev_hash); links it into the chain. [EU]",
    )

    @field_validator("override_reason")
    @classmethod
    def override_reason_required_when_overridden(cls, v: Optional[str], info) -> Optional[str]:
        if info.data.get("override_applied") and not v:
            raise ValueError("override_reason must be provided when override_applied is True.")
        return v

    def content_hash(self) -> str:
        """Deterministic SHA-256 over this entry's content, excluding entry_hash itself.

        prev_hash IS included, which is what chains each entry to the one before
        it. Serialisation is canonical (sorted keys, no whitespace) so the hash is
        reproducible by anyone re-reading the log.

        Fields left at their default (None, empty list, …) are excluded, so adding
        a new optional field to the schema does not change the hash of entries
        written before it existed. Without this, every schema addition would break
        the chain for past records — a tamper-evident log has to survive its own
        evolution. Tamper detection is unaffected: changing a field away from its
        default still changes the hash, and changing one back to its default drops
        it from the canonical form, which also changes the hash.
        """
        payload = self.model_dump(mode="json", exclude={"entry_hash"}, exclude_defaults=True)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
    fairness: str = ""
    public_access: str
    statement: str


class ImpactAssessmentEntry(BaseModel):
    """One AI use case assessed for impact, per DTA Policy v2.0."""

    use_case: str
    risk_category: str
    decisions: int
    data_sources: list[str]
    affected_groups: list[str]
    benefits: str
    risks: list[str]
    mitigations: list[str]
    human_oversight: str
    fairness_considerations: str
    residual_risk: str
    first_used: Optional[datetime] = None
    last_used: Optional[datetime] = None


class ImpactAssessment(BaseModel):
    """A DTA-style AI use-case impact assessment, generated live from the log.

    DTA Policy v2.0 makes AI use-case impact assessments mandatory from
    15 December 2026; one assessment is produced per in-scope use case."""

    agency: str
    accountable_official: str
    policy: str = "Policy for the responsible use of AI in government (DTA, v2.0)"
    mandatory_from: str = "15 December 2026"
    use_cases: list[ImpactAssessmentEntry]
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
    fairness = (
        "Outputs are aggregate counts, not rates. Differences between regions can reflect "
        "population size, reporting behaviour and policing intensity as much as actual "
        "offending, so the figures are not used to rank or target places, communities or "
        "individuals; comparisons carry this caveat explicitly."
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
        f"## Fairness\n{fairness}\n\n"
        f"## Public access and accountability\n{public}\n"
    )
    return TransparencyStatement(
        agency=agency, accountable_official=accountable_official,
        ai_systems=systems, purposes=purposes, data_sources=sources,
        risk_tiers=risks, human_oversight=oversight, fairness=fairness,
        public_access=public, statement=statement,
    )


class ModelCard(BaseModel):
    """A model card for the Signal analyst, generated live from the log.

    Documents the two narrative layers (deterministic template + optional LLM)
    and the faithfulness eval that gates the LLM output, with the live eval
    results read back from the audit trail."""

    name: str = "Signal analyst"
    version: str
    agency: str
    accountable_official: str
    policy: str = "Policy for the responsible use of AI in government (DTA, v2.0)"
    components: list[dict]
    narrative_eval: dict
    check_validation: dict = {}  # measured precision/recall of the faithfulness check itself
    data_sources: list[str]
    intended_use: str
    out_of_scope: list[str]
    limitations: list[str]
    card: str


def model_card(
    entries: list[DecisionEntry],
    *,
    agency: str,
    accountable_official: str,
    version: str,
    llm_model: str,
    deterministic_model: str,
    check_validation: dict | None = None,
) -> ModelCard:
    """Generate the analyst model card, with live faithfulness results."""
    check_validation = check_validation or {}
    scored = [e for e in entries if e.faithfulness_score is not None]
    mean_faith = round(sum(e.faithfulness_score for e in scored) / len(scored), 3) if scored else None
    fallbacks = sum(1 for e in entries if "faithfulness-fallback" in (e.tags or []))
    sources = sorted({s for e in entries for s in e.data_sources})[:12]

    components = [
        {
            "name": deterministic_model,
            "type": "deterministic statistics",
            "role": "Computes totals, month-on-month / year-on-year change, trend "
                    "direction and z-score anomalies. Also writes the default narrative.",
        },
        {
            "name": llm_model,
            "type": "LLM narrative (optional, provider-agnostic)",
            "role": "Phrases the narrative from the computed aggregates only. Never "
                    "sees raw records. Output is gated by the faithfulness eval.",
        },
    ]
    narrative_eval = {
        "method": "Deterministic faithfulness check (no model call): every figure in "
                  "the narrative must appear in the computed statistics, and the trend "
                  "sentence must not contradict the computed direction.",
        "on_failure": "The LLM narrative is rejected and the deterministic template is "
                      "served instead; the rejection is recorded in the audit log.",
        "decisions_evaluated": len(scored),
        "mean_faithfulness": mean_faith,
        "fallbacks_to_template": fallbacks,
    }
    intended_use = (
        "Surface trends and anomalies in already-aggregated, de-identified public "
        "crime data, with every answer audit-logged for governance."
    )
    out_of_scope = [
        "Individual-level prediction, profiling, or any decision about a person.",
        "Operational policing or resource-allocation decisions.",
        "Any use over data containing personal information.",
    ]
    limitations = [
        "Counts are not rates: differences across regions may reflect population, "
        "reporting or policing intensity rather than real offending.",
        "Anomaly and trend thresholds are fixed heuristics, not calibrated models.",
        "The LLM narrative is gated for figure-faithfulness, not for tone or nuance; "
        "the deterministic template is always the fallback.",
        "Large explorer datasets are sampled at a row cap (flagged in the result).",
    ]
    card = (
        f"# Model card — Signal analyst v{version}\n\n"
        f"**Agency:** {agency}  ·  **Accountable official:** {accountable_official}\n\n"
        f"**Policy basis:** Policy for the responsible use of AI in government (DTA, v2.0).\n\n"
        f"## Components\n"
        + "".join(f"- **{c['name']}** ({c['type']}) — {c['role']}\n" for c in components)
        + f"\n## Narrative faithfulness eval\n{narrative_eval['method']} "
        f"{narrative_eval['on_failure']}\n\n"
        f"- Decisions evaluated: {len(scored)}\n"
        f"- Mean faithfulness: {mean_faith if mean_faith is not None else 'n/a'}\n"
        f"- Fallbacks to template: {fallbacks}\n\n"
        + _check_validation_section(check_validation)
        + f"## Intended use\n{intended_use}\n\n"
        f"## Out of scope\n" + "".join(f"- {x}\n" for x in out_of_scope)
        + "\n## Limitations\n" + "".join(f"- {x}\n" for x in limitations)
        + f"\n## Data sources\n{', '.join(sources) or 'None recorded yet'}.\n"
    )
    return ModelCard(
        version=version,
        agency=agency,
        accountable_official=accountable_official,
        components=components,
        narrative_eval=narrative_eval,
        check_validation=check_validation,
        data_sources=sources,
        intended_use=intended_use,
        out_of_scope=out_of_scope,
        limitations=limitations,
        card=card,
    )


def _check_validation_section(cv: dict) -> str:
    """Render the measured precision/recall of the faithfulness check for the card."""
    det = cv.get("deterministic_check") if cv else None
    if not det:
        return ""
    lines = (
        f"## How good is the faithfulness check?\n"
        f"Measured against {cv['labelled_cases']} hand-labelled narratives "
        f"(positive class: unfaithful).\n\n"
        f"- Deterministic check: precision {det['precision']}, recall {det['recall']}, "
        f"F1 {det['f1']} (confusion {det['confusion']}).\n"
        f"- {det['note']}\n"
    )
    judge = cv.get("llm_judge") or {}
    if judge.get("precision") is not None:
        lines += (
            f"- LLM judge (second signal): precision {judge['precision']}, recall "
            f"{judge['recall']}, agreement with the deterministic check "
            f"{judge['agreement_with_deterministic']}.\n"
        )
    else:
        lines += f"- LLM judge: {judge.get('note', 'not measured.')}\n"
    return lines + "\n"


_RISK_ORDER = {"unacceptable": 3, "high": 2, "limited": 1, "minimal": 0}


def _is_crime_use_case(name: str) -> bool:
    n = name.lower()
    return any(w in n for w in ("crime", "comparison", "trend", "offence", "offense"))


def impact_assessment(
    entries: list[DecisionEntry], agency: str, accountable_official: str
) -> ImpactAssessment:
    """Generate a DTA-style AI use-case impact assessment from the decision log.

    One assessment per in-scope use case, with mitigations that cite the live
    governance posture (faithfulness eval results and human-review rate)."""
    groups: dict[str, list[DecisionEntry]] = {}
    for e in entries:
        groups.setdefault(_use_case_of(e), []).append(e)

    rows: list[ImpactAssessmentEntry] = []
    for use_case, es in sorted(groups.items()):
        highest = max((e.risk_category.value for e in es), key=lambda r: _RISK_ORDER.get(r, 0))
        sources = sorted({s for e in es for s in e.data_sources})[:10]
        needs = [e for e in es if e.human_review_required]
        review_rate = round(len(needs) / len(es), 3) if es else 0.0
        scored = [e for e in es if e.faithfulness_score is not None]
        mean_faith = round(sum(e.faithfulness_score for e in scored) / len(scored), 3) if scored else None
        fallbacks = sum(1 for e in es if "faithfulness-fallback" in (e.tags or []))
        is_crime = _is_crime_use_case(use_case)

        affected = (
            ["Members of the public in the analysed regions",
             "Communities subject to differential policing or reporting"]
            if is_crime else
            ["Consumers of the analysis", "Groups represented in the underlying dataset"]
        )
        benefits = (
            f"Surfaces trends and anomalies for '{use_case}' over aggregate, de-identified "
            "data, with every answer audit-logged for accountability."
        )
        risks = [
            "Aggregate counts may be misread as rates, overstating differences between regions or groups.",
            "An LLM-written narrative could misstate a figure or the direction of a trend.",
            "Statistical anomalies could be over- or under-interpreted without context.",
        ]
        mitigations = [
            "Aggregates only — no personal information enters the system, so re-identification risk is minimal.",
            (
                "Every LLM narrative is checked by a deterministic faithfulness eval; unfaithful "
                "output is rejected and the deterministic template served "
                f"(mean faithfulness {mean_faith if mean_faith is not None else 'n/a'}, "
                f"{fallbacks} fallback{'' if fallbacks == 1 else 's'})."
            ),
            (
                "Statistically anomalous results are flagged for human review "
                f"({round(review_rate * 100)}% of decisions in this use case); reviews and "
                "overrides are recorded against the decision."
            ),
            "Full audit trail is public (GET /decisions); every output carries a decision_id.",
        ]
        oversight = (
            f"{len(needs)} of {len(es)} decisions in this use case were flagged for human "
            "review. Anomalous results require human review before action."
        )
        fairness = (
            "Counts are not rates: differences across regions may reflect population, reporting "
            "or policing intensity rather than real offending. Outputs must not be used to rank "
            "places or people."
            if is_crime else
            "Differences across groups may reflect sampling or collection bias in the underlying "
            "dataset rather than real-world effects."
        )
        residual = (
            "Limited. With aggregates-only data, figure-faithful narratives and human review on "
            "anomalies, residual risk is low; the main residual risk is misinterpretation of "
            "counts as rates by downstream readers."
        )
        rows.append(ImpactAssessmentEntry(
            use_case=use_case, risk_category=highest, decisions=len(es),
            data_sources=sources, affected_groups=affected, benefits=benefits,
            risks=risks, mitigations=mitigations, human_oversight=oversight,
            fairness_considerations=fairness, residual_risk=residual,
            first_used=min((e.timestamp for e in es), default=None),
            last_used=max((e.timestamp for e in es), default=None),
        ))
    rows.sort(key=lambda r: -r.decisions)

    header = (
        "# AI use-case impact assessment\n\n"
        f"**Agency:** {agency}  ·  **Accountable official:** {accountable_official}\n\n"
        "**Policy basis:** Policy for the responsible use of AI in government (DTA, v2.0); "
        "AI use-case impact assessments mandatory from 15 December 2026.\n\n"
    )
    body = "".join(
        f"## {r.use_case} ({r.risk_category} risk)\n"
        f"- **Decisions:** {r.decisions}\n"
        f"- **Affected groups:** {', '.join(r.affected_groups)}\n"
        f"- **Benefits:** {r.benefits}\n"
        f"- **Risks:** {'; '.join(r.risks)}\n"
        f"- **Mitigations:** {'; '.join(r.mitigations)}\n"
        f"- **Human oversight:** {r.human_oversight}\n"
        f"- **Fairness:** {r.fairness_considerations}\n"
        f"- **Residual risk:** {r.residual_risk}\n\n"
        for r in rows
    )
    statement = header + (body or "No in-scope AI use cases recorded yet.\n")
    return ImpactAssessment(
        agency=agency, accountable_official=accountable_official,
        use_cases=rows, statement=statement,
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

    #: prev_hash value for the first entry in a chain.
    GENESIS = "genesis"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash: Optional[str] = None  # lazily loaded from the file tail

    def _tail_hash(self) -> str:
        """entry_hash of the last logged entry, or GENESIS for an empty/legacy log.

        Cached in memory; the single-writer, single-container design means the
        cache cannot go stale. On a cold start it is rebuilt from the file's last
        line. A legacy log whose last entry predates hashing chains from GENESIS,
        so the tamper-evident chain simply begins at the next entry.
        """
        if self._last_hash is not None:
            return self._last_hash
        last_line = None
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line.strip()
        if last_line:
            self._last_hash = DecisionEntry.model_validate_json(last_line).entry_hash or self.GENESIS
        else:
            self._last_hash = self.GENESIS
        return self._last_hash

    def log(self, entry: DecisionEntry) -> None:
        """Append a DecisionEntry, chaining it to the previous one by hash."""
        entry.prev_hash = self._tail_hash()
        entry.entry_hash = entry.content_hash()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(entry.to_jsonl_line() + "\n")
        self._last_hash = entry.entry_hash

    def verify(self) -> "ChainVerification":
        """Re-walk the log and confirm the hash chain is intact."""
        return verify_chain(self.read_all())

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
# Tamper-evidence: verify the hash chain
# ---------------------------------------------------------------------------

class ChainVerification(BaseModel):
    """The verdict on whether the audit log's hash chain is intact."""

    valid: bool = Field(description="True if no edit, deletion or reordering was detected")
    entries_total: int = Field(description="Total entries read from the log")
    chained_entries: int = Field(default=0, description="Entries covered by the tamper-evident chain")
    legacy_entries: int = Field(
        default=0, description="Leading entries written before hashing began (unverifiable, not tampered)"
    )
    head_hash: Optional[str] = Field(
        default=None,
        description="entry_hash of the last chained entry — the digest that commits the whole chain",
    )
    broken_at: Optional[str] = Field(
        default=None, description="decision_id where the chain first fails, if any"
    )
    reason: Optional[str] = Field(default=None, description="Why it failed, in plain words")


def verify_chain(entries: list[DecisionEntry]) -> ChainVerification:
    """Re-walk a list of entries and confirm each one's hash, and the links between them.

    A tamper-evident chain begins at the first entry that carries an ``entry_hash``.
    Entries written before the feature shipped have no hash; they are counted as
    ``legacy`` and reported honestly rather than failing the check. Once the chain
    has begun, every entry must hash correctly and point at its predecessor, and no
    unchained entry may appear after it (that would be an inserted or rewound record).
    """
    legacy = 0
    chained = 0
    prev: Optional[str] = None  # None until the chain begins
    for idx, e in enumerate(entries):
        if e.entry_hash is None:
            if prev is not None:
                return ChainVerification(
                    valid=False, entries_total=len(entries), chained_entries=chained,
                    legacy_entries=legacy, broken_at=e.decision_id,
                    reason="an unchained entry appears after the hash chain began",
                )
            legacy += 1
            continue
        expected_prev = DecisionLogger.GENESIS if prev is None else prev
        if e.prev_hash != expected_prev:
            return ChainVerification(
                valid=False, entries_total=len(entries), chained_entries=chained,
                legacy_entries=legacy, broken_at=e.decision_id,
                reason="prev_hash does not match the previous entry (an entry was removed or reordered)",
            )
        if e.entry_hash != e.content_hash():
            return ChainVerification(
                valid=False, entries_total=len(entries), chained_entries=chained,
                legacy_entries=legacy, broken_at=e.decision_id,
                reason="entry content does not match its hash (the entry was altered)",
            )
        prev = e.entry_hash
        chained += 1
    return ChainVerification(
        valid=True, entries_total=len(entries), chained_entries=chained,
        legacy_entries=legacy, head_hash=prev,
    )


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
