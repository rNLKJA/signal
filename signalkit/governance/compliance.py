"""An auditor-ready compliance report, generated from the audit log.

Buyers in regulated settings do not want a marketing claim that a system is
"compliant"; they want a mapping from named requirements to the evidence that
each is met. This module produces exactly that, for three frameworks a buyer is
likely to ask about:

- **ISO/IEC 42001:2023** — the AI management system standard (Annex A controls).
- **NIST AI Risk Management Framework** — the Govern / Map / Measure / Manage
  functions.
- **DTA Policy for the responsible use of AI in government v2.0** — the Australian
  mandatory requirements.

Every item carries live evidence read from the log (decision counts, the chain
verification, the faithfulness results, the generated artefacts), and an honest
status. Signal is a governance *layer*, not a whole management system, so the
organisational controls (leadership, competence, supplier management) are marked
as the deploying organisation's responsibility rather than claimed.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from signalkit.governance.decision_log import (
    DecisionEntry,
    summarise,
    verify_chain,
)

MET = "met"
PARTIAL = "partial"
ORG = "organisation's responsibility"


class ComplianceItem(BaseModel):
    requirement_id: str
    requirement: str
    how_signal_meets_it: str
    status: str = Field(description=f"{MET} | {PARTIAL} | {ORG}")
    evidence: List[str] = Field(default_factory=list)


class FrameworkMapping(BaseModel):
    framework: str
    summary: str
    items: List[ComplianceItem]


class ComplianceReport(BaseModel):
    """A mapping of recognised compliance requirements to live evidence."""

    agency: str
    accountable_official: str
    decisions_covered: int
    chain_valid: bool
    chain_head: Optional[str] = None
    note: str
    frameworks: List[FrameworkMapping]
    statement: str  # a Markdown render for an auditor


def _evidence(entries: List[DecisionEntry]) -> dict:
    s = summarise(entries)
    chain = verify_chain(entries)
    faith = [e.faithfulness_score for e in entries if e.faithfulness_score is not None]
    return {
        "decisions": s.total_decisions,
        "review_rate_pct": round((s.human_review_rate or 0) * 100),
        "reviews_recorded": s.reviews_recorded,
        "chain_valid": chain.valid,
        "chain_head": chain.head_hash,
        "mean_faithfulness": (round(sum(faith) / len(faith), 3) if faith else None),
        "use_cases": sorted({e.use_case for e in entries if e.use_case}),
        "risk_tiers": s.by_risk_category,
    }


def _iso42001(ev: dict) -> FrameworkMapping:
    chain = f"chain verified, head {ev['chain_head'][:12]}" if ev["chain_valid"] and ev["chain_head"] else "chain not yet started"
    return FrameworkMapping(
        framework="ISO/IEC 42001:2023 (AI management system)",
        summary="Evidence toward the Annex A operational controls that an AI management "
                "system must implement. Organisation-level controls remain the deployer's.",
        items=[
            ComplianceItem(
                requirement_id="A.5.5",
                requirement="AI system impact assessment is conducted and documented.",
                how_signal_meets_it="An AI use-case impact assessment is generated live from the "
                                    "log for every in-scope use case (affected groups, risks, "
                                    "mitigations, residual risk).",
                status=MET,
                evidence=["impact assessment available at /governance/impact-assessment",
                          f"{len(ev['use_cases'])} use case(s) assessed"],
            ),
            ComplianceItem(
                requirement_id="A.6.2.8",
                requirement="Records of AI system operation are kept.",
                how_signal_meets_it="Every AI-assisted decision is written to an append-only, "
                                    "tamper-evident log on the request path; the system cannot "
                                    "answer without logging.",
                status=MET,
                evidence=[f"{ev['decisions']} decisions logged", chain],
            ),
            ComplianceItem(
                requirement_id="A.7.4",
                requirement="Data quality and provenance for the AI system are managed.",
                how_signal_meets_it="Each decision records its data sources and a risk tier; the "
                                    "analyst operates on aggregates only, with no PII entering the "
                                    "system.",
                status=MET,
                evidence=[f"risk tiers recorded: {ev['risk_tiers'] or 'n/a'}"],
            ),
            ComplianceItem(
                requirement_id="A.8.3",
                requirement="Information is provided to users about the AI system.",
                how_signal_meets_it="A transparency statement is generated live from the log; every "
                                    "answer carries a traceable decision id.",
                status=MET,
                evidence=["transparency statement at /governance/transparency"],
            ),
            ComplianceItem(
                requirement_id="A.9.3",
                requirement="Performance of the AI system is monitored and evaluated.",
                how_signal_meets_it="The LLM narrative is checked for faithfulness on every answer, "
                                    "and the check's own precision/recall is measured and reported.",
                status=MET,
                evidence=[f"mean faithfulness {ev['mean_faithfulness']}",
                          "check validation at /governance/faithfulness-eval"],
            ),
            ComplianceItem(
                requirement_id="A.3 / A.4",
                requirement="Leadership, policy, roles, competence and resourcing for the AIMS.",
                how_signal_meets_it="Signal records the accountable official and agency on every "
                                    "decision, but the surrounding management system (leadership, "
                                    "competence, internal audit) is the organisation's to operate.",
                status=ORG,
                evidence=[],
            ),
        ],
    )


def _nist(ev: dict) -> FrameworkMapping:
    return FrameworkMapping(
        framework="NIST AI Risk Management Framework 1.0",
        summary="How the four functions are supported by the governance layer.",
        items=[
            ComplianceItem(
                requirement_id="GOVERN",
                requirement="Accountability structures and policies for AI risk are in place.",
                how_signal_meets_it="An accountable official and agency are stamped on every "
                                    "decision, and a register of in-scope use cases is generated "
                                    "live from the log.",
                status=MET,
                evidence=[f"{len(ev['use_cases'])} use case(s) in the live register",
                          "register at /governance/register"],
            ),
            ComplianceItem(
                requirement_id="MAP",
                requirement="Context is established and AI risks are categorised.",
                how_signal_meets_it="Every decision records its use case, a risk tier (EU AI Act "
                                    "aligned) and its data sources.",
                status=MET,
                evidence=[f"risk tiers: {ev['risk_tiers'] or 'n/a'}"],
            ),
            ComplianceItem(
                requirement_id="MEASURE",
                requirement="AI risks and trustworthiness are analysed and tracked.",
                how_signal_meets_it="Narrative faithfulness is checked on every answer, the check's "
                                    "precision/recall is measured against a labelled set, and "
                                    "anomalous results are flagged for human review.",
                status=MET,
                evidence=[f"mean faithfulness {ev['mean_faithfulness']}",
                          f"human-review rate {ev['review_rate_pct']}%"],
            ),
            ComplianceItem(
                requirement_id="MANAGE",
                requirement="Risks are responded to, monitored and documented over time.",
                how_signal_meets_it="A human-review workflow records reviews and overrides (with a "
                                    "required reason), and the tamper-evident log lets any decision "
                                    "be traced and its integrity verified after the fact.",
                status=MET,
                evidence=[f"{ev['reviews_recorded']} review(s) recorded",
                          "integrity check at /governance/verify"],
            ),
        ],
    )


def _dta(ev: dict) -> FrameworkMapping:
    return FrameworkMapping(
        framework="DTA Policy for the responsible use of AI in government v2.0",
        summary="The Australian mandatory requirements, each generated live from the log.",
        items=[
            ComplianceItem(
                requirement_id="DTA-1",
                requirement="Designate accountable officials for AI use.",
                how_signal_meets_it="The accountable official and agency are configured per "
                                    "deployment and stamped on every decision.",
                status=MET,
                evidence=["recorded on each decision entry"],
            ),
            ComplianceItem(
                requirement_id="DTA-2",
                requirement="Maintain a register of in-scope AI use cases.",
                how_signal_meets_it="The register is computed live from the log, so it can never be "
                                    "out of date.",
                status=MET,
                evidence=[f"{len(ev['use_cases'])} use case(s)", "live at /governance/register"],
            ),
            ComplianceItem(
                requirement_id="DTA-3",
                requirement="Publish an AI transparency statement.",
                how_signal_meets_it="Generated from the log, so it cannot drift from what the system "
                                    "actually does.",
                status=MET,
                evidence=["live at /governance/transparency"],
            ),
            ComplianceItem(
                requirement_id="DTA-4",
                requirement="Complete AI use-case impact assessments (mandatory from 15 Dec 2026).",
                how_signal_meets_it="One impact assessment per in-scope use case, generated from the "
                                    "log with affected groups, risks, mitigations and residual risk.",
                status=MET,
                evidence=["live at /governance/impact-assessment"],
            ),
        ],
    )


def compliance_report(
    entries: List[DecisionEntry],
    *,
    agency: str,
    accountable_official: str,
) -> ComplianceReport:
    """Generate the compliance report from the audit log."""
    ev = _evidence(entries)
    frameworks = [_dta(ev), _nist(ev), _iso42001(ev)]

    lines = [
        "# AI governance compliance report\n",
        f"**Organisation:** {agency}  ·  **Accountable official:** {accountable_official}\n",
        f"Generated live from the audit log. Decisions covered: **{ev['decisions']}**. "
        f"Audit chain: **{'verified' if ev['chain_valid'] else 'NOT verified'}**"
        + (f" (head {ev['chain_head'][:12]})." if ev["chain_head"] else ".") + "\n",
        "Status key: *met* — satisfied by the governance layer with live evidence; "
        "*partial* — partially supported; *organisation's responsibility* — outside the tool, "
        "for the deploying organisation to operate.\n",
    ]
    for fw in frameworks:
        lines.append(f"## {fw.framework}\n\n{fw.summary}\n")
        for it in fw.items:
            ev_txt = ("; ".join(it.evidence)) if it.evidence else "—"
            lines.append(
                f"- **{it.requirement_id} — {it.requirement}**  \n"
                f"  _{it.status}._ {it.how_signal_meets_it}  \n"
                f"  Evidence: {ev_txt}\n"
            )
    statement = "\n".join(lines)

    return ComplianceReport(
        agency=agency,
        accountable_official=accountable_official,
        decisions_covered=ev["decisions"],
        chain_valid=ev["chain_valid"],
        chain_head=ev["chain_head"],
        note="Evidence is read live from the audit log at generation time. Items marked "
             "'organisation's responsibility' are management-system controls outside a "
             "governance tool's scope and are not claimed as met.",
        frameworks=frameworks,
        statement=statement,
    )
