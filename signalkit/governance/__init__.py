"""Signal governance — a standalone toolkit for governed AI decisions.

This package is the heart of Signal, and it stands on its own: it depends only on
Pydantic and the standard library, with no tie to the crime-data app that happens
to use it. Drop it into any AI system to get the same guarantees:

- a typed, append-only **audit log** of every AI-assisted decision
  (`DecisionEntry`, `DecisionLogger`);
- a **tamper-evident** hash chain over that log, with verification
  (`verify_chain`, `ChainVerification`);
- the **compliance artefacts** a regulator asks for, generated from the log
  rather than hand-written: a use-case register (`register`), a transparency
  statement (`transparency_statement`), an impact assessment
  (`impact_assessment`), a governance summary (`summarise`), and a model card
  (`model_card`).

The design rule that makes it worth adopting: logging is meant to sit on the
request path, so the system cannot answer without first writing the record.

A governed, tamper-evident decision in a few lines::

    from signalkit.governance import DecisionEntry, DecisionLogger, RiskCategory

    log = DecisionLogger("decisions.jsonl")
    log.log(DecisionEntry(
        model_name="my-model-v1",
        input_summary="user asked X",
        model_output_summary="answered Y",
        decision_made="Returned Y to the user.",
        risk_category=RiskCategory.limited,
        human_review_required=False,
    ))
    assert log.verify().valid   # the chain is intact

See ``signalkit/governance/README.md`` for the full guide.
"""

from signalkit.governance.audit_store import (
    AuditStore,
    InMemoryAuditStore,
    JsonlAuditStore,
    SqliteAuditStore,
)
from signalkit.governance.decision_log import (
    ChainVerification,
    DecisionCategory,
    DecisionEntry,
    DecisionLogger,
    GovernanceSummary,
    ImpactAssessment,
    ImpactAssessmentEntry,
    ModelCard,
    RiskCategory,
    TransparencyStatement,
    UseCaseEntry,
    UseCaseRegister,
    impact_assessment,
    model_card,
    register,
    summarise,
    transparency_statement,
    verify_chain,
)
from signalkit.governance.tenancy import (
    DEFAULT_TENANT,
    TenantLog,
    parse_tenant_keys,
    tenant_for_api_key,
)

__all__ = [
    # audit log
    "DecisionEntry",
    "DecisionLogger",
    "DecisionCategory",
    "RiskCategory",
    # pluggable storage
    "AuditStore",
    "JsonlAuditStore",
    "InMemoryAuditStore",
    "SqliteAuditStore",
    # multi-tenancy
    "TenantLog",
    "DEFAULT_TENANT",
    "tenant_for_api_key",
    "parse_tenant_keys",
    # tamper-evidence
    "ChainVerification",
    "verify_chain",
    # artefacts generated from the log
    "GovernanceSummary",
    "summarise",
    "UseCaseEntry",
    "UseCaseRegister",
    "register",
    "TransparencyStatement",
    "transparency_statement",
    "ImpactAssessmentEntry",
    "ImpactAssessment",
    "impact_assessment",
    "ModelCard",
    "model_card",
]
