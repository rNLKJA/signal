"""
signal/governance/decision_log.py
==================================
Structured audit trail for AI-assisted decisions in Signal.

Aligned to:
  - APS Mandatory AI Requirements (June 15, 2026) — documents what AI was used,
    what decision was made, what data informed it, and whether human review occurred.
  - EU AI Act (2024/1689) — risk-tier classification and traceability requirements.
  - Privacy Act 1988 (Cth) amendment (Dec 10, 2026) — automated decision-making
    disclosure obligation for APP entities.

Usage
-----
    from signal.governance.decision_log import DecisionEntry, DecisionLogger

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
        legislative_basis="APS AI Policy v1.0 — Mandatory Requirement 3",
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
    [APS] satisfy the APS Mandatory AI Requirements. Fields marked [EU] satisfy
    EU AI Act traceability obligations. Fields marked [Privacy] satisfy the
    Privacy Act 1988 automated decision-making disclosure requirement.
    """

    # --- Identity ---
    decision_id: str = Field(
        default_factory=lambda: f"d-{uuid.uuid4().hex[:8]}",
        description="Unique identifier for this decision event. [APS] [EU] [Privacy]",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the decision. [APS] [EU]",
    )

    # --- Model provenance ---
    model_name: str = Field(
        description="Name of the AI model used (e.g. 'gpt-4o', 'claude-sonnet-4-6'). [APS]",
    )
    model_version: Optional[str] = Field(
        default=None,
        description="Model version or release date string. [APS] [EU]",
    )
    model_provider: Optional[str] = Field(
        default=None,
        description="Provider of the model (e.g. 'OpenAI', 'Anthropic'). [APS]",
    )

    # --- Input / output ---
    input_summary: str = Field(
        description="Plain-language summary of the input given to the model. "
                    "Do not include raw PII. [APS] [Privacy]",
    )
    model_output_summary: str = Field(
        description="Plain-language summary of the model's output. [APS] [EU]",
    )
    data_sources: list[str] = Field(
        default_factory=list,
        description="List of datasets or sources that informed the query. [APS] [Privacy]",
    )

    # --- Decision ---
    decision_made: str = Field(
        description="The actual decision or action taken on the basis of the model output. [APS] [Privacy]",
    )
    decision_category: DecisionCategory = Field(
        default=DecisionCategory.analytical,
        description="Broad category of this decision. [APS]",
    )
    confidence_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Model or analyst confidence in the output (0.0–1.0). [EU]",
    )

    # --- Human oversight ---
    human_review_required: bool = Field(
        description="Whether human review was required before acting on this output. [APS] [EU]",
    )
    human_reviewer: Optional[str] = Field(
        default=None,
        description="Identifier (email or role) of the human reviewer, if applicable. [APS]",
    )
    override_applied: bool = Field(
        default=False,
        description="True if a human overrode the model output. [EU]",
    )
    override_reason: Optional[str] = Field(
        default=None,
        description="Reason for override, if override_applied is True. [EU]",
    )

    # --- Governance context ---
    agency: Optional[str] = Field(
        default=None,
        description="Government agency or organisation responsible for this decision. [APS]",
    )
    officer_id: Optional[str] = Field(
        default=None,
        description="Anonymised ID of the officer or user who initiated the query. [APS] [Privacy]",
    )
    legislative_basis: Optional[str] = Field(
        default=None,
        description="Policy or legislative reference authorising AI use in this context. [APS]",
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
