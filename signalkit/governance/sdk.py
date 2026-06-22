"""The Signal governance SDK — add governed, tamper-evident audit to any app.

Wrap each AI answer in ``Governor.record(...)``: the decision is written to a
tamper-evident audit log as the block completes, so the system cannot return an
answer without logging it, and the DTA artefacts (register, transparency
statement, impact assessment, governance summary) come straight from that log.
Multi-tenant out of the box, and ``mount(app)`` exposes the governance endpoints
on any FastAPI app.

A governed decision in a few lines::

    from signalkit.governance import Governor

    gov = Governor("decisions.jsonl", agency="Acme", accountable_official="Jane Doe")

    with gov.record(use_case="support-bot", model_name="gpt-4o", input_summary=question) as rec:
        answer = my_llm(question)
        rec.output(answer)

    assert gov.verify().valid              # the audit chain is intact
    gov.register()                         # the DTA use-case register, generated live

This module depends only on ``signalkit.governance`` (Pydantic + stdlib); FastAPI
is imported lazily, and only if you call ``mount()``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from signalkit.governance.audit_store import AuditStore
from signalkit.governance.decision_log import (
    ChainVerification,
    DecisionCategory,
    DecisionEntry,
    DecisionLogger,
    GovernanceSummary,
    ImpactAssessment,
    RiskCategory,
    TransparencyStatement,
    UseCaseRegister,
    impact_assessment,
    register,
    summarise,
    transparency_statement,
    verify_chain,
)
from signalkit.governance.tenancy import DEFAULT_TENANT, TenantLog


class Recording:
    """One in-progress governed decision, used as a context manager.

    The decision is logged when the ``with`` block exits normally. If the block
    raises, nothing is logged — a failed answer is not a decision. ``decision_id``
    is available immediately so it can be returned to the caller before the block
    ends.
    """

    def __init__(
        self,
        governor: "Governor",
        *,
        use_case: str,
        model_name: str,
        input_summary: str,
        tenant_id: str,
        model_provider: Optional[str],
        risk_category: RiskCategory,
        decision_category: DecisionCategory,
    ) -> None:
        self._gov = governor
        self.decision_id = f"d-{uuid.uuid4().hex[:8]}"  # pre-allocated
        self._tenant_id = tenant_id
        self._use_case = use_case
        self._model_name = model_name
        self._model_provider = model_provider
        self._input_summary = input_summary
        self._risk = risk_category
        self._category = decision_category
        self._output_summary = ""
        self._decision_made = "Returned an AI-assisted answer to the caller."
        self._faithfulness: Optional[float] = None
        self._human_review = False
        self._logged = False

    def __enter__(self) -> "Recording":
        return self

    def output(
        self,
        summary: Any,
        *,
        decision: Optional[str] = None,
        faithfulness_score: Optional[float] = None,
        human_review_required: bool = False,
        risk_category: Optional[RiskCategory] = None,
    ) -> "Recording":
        """Record what the model produced and how it should be governed."""
        self._output_summary = str(summary)[:1000]
        if decision:
            self._decision_made = decision
        if faithfulness_score is not None:
            self._faithfulness = faithfulness_score
        self._human_review = human_review_required
        if risk_category is not None:
            self._risk = risk_category
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._commit()
        return False  # never suppress exceptions

    def _commit(self) -> None:
        if self._logged:
            return
        entry = DecisionEntry(
            decision_id=self.decision_id,
            model_name=self._model_name,
            model_provider=self._model_provider,
            input_summary=self._input_summary,
            model_output_summary=self._output_summary,
            decision_made=self._decision_made,
            decision_category=self._category,
            use_case=self._use_case,
            risk_category=self._risk,
            human_review_required=self._human_review,
            faithfulness_score=self._faithfulness,
            agency=self._gov.agency,
            human_reviewer=None,
            tenant_id=self._tenant_id,
        )
        self._gov._log(entry, self._tenant_id)
        self._logged = True


class Governor:
    """Governance for any app, in a few lines.

    ``store`` may be a path (a JSONL file, the default), an ``AuditStore``, a
    ``DecisionLogger``, or a ``TenantLog`` for multi-tenant deployments.
    """

    def __init__(
        self,
        store: "str | Path | AuditStore | DecisionLogger | TenantLog" = "decisions.jsonl",
        *,
        agency: Optional[str] = None,
        accountable_official: Optional[str] = None,
    ) -> None:
        self.agency = agency
        self.accountable_official = accountable_official
        if isinstance(store, TenantLog):
            self._tenant: Optional[TenantLog] = store
            self._single: Optional[DecisionLogger] = None
        elif isinstance(store, DecisionLogger):
            self._tenant, self._single = None, store
        else:
            self._tenant, self._single = None, DecisionLogger(store)

    # --- recording ---------------------------------------------------------

    def record(
        self,
        *,
        use_case: str,
        model_name: str,
        input_summary: str = "",
        tenant_id: str = DEFAULT_TENANT,
        model_provider: Optional[str] = None,
        risk_category: RiskCategory = RiskCategory.limited,
        decision_category: DecisionCategory = DecisionCategory.analytical,
    ) -> Recording:
        return Recording(
            self,
            use_case=use_case,
            model_name=model_name,
            input_summary=input_summary,
            tenant_id=tenant_id,
            model_provider=model_provider,
            risk_category=risk_category,
            decision_category=decision_category,
        )

    def _log(self, entry: DecisionEntry, tenant_id: str) -> None:
        if self._tenant is not None:
            self._tenant.log(entry, tenant_id)
        else:
            self._single.log(entry)

    def _entries(self, tenant_id: str) -> list[DecisionEntry]:
        if self._tenant is not None:
            return self._tenant.read_all(tenant_id)
        return self._single.read_all()

    # --- live governance views --------------------------------------------

    def decisions(self, tenant_id: str = DEFAULT_TENANT, limit: int = 50) -> list[DecisionEntry]:
        return self._entries(tenant_id)[-limit:]

    def verify(self, tenant_id: str = DEFAULT_TENANT) -> ChainVerification:
        return verify_chain(self._entries(tenant_id))

    def summary(self, tenant_id: str = DEFAULT_TENANT) -> GovernanceSummary:
        return summarise(self._entries(tenant_id))

    def register(self, tenant_id: str = DEFAULT_TENANT) -> UseCaseRegister:
        return register(self._entries(tenant_id), self._agency(), self._official())

    def transparency(self, tenant_id: str = DEFAULT_TENANT) -> TransparencyStatement:
        return transparency_statement(self._entries(tenant_id), self._agency(), self._official())

    def impact_assessment(self, tenant_id: str = DEFAULT_TENANT) -> ImpactAssessment:
        return impact_assessment(self._entries(tenant_id), self._agency(), self._official())

    def _agency(self) -> str:
        return self.agency or "Unnamed organisation"

    def _official(self) -> str:
        return self.accountable_official or "Unnamed accountable official"

    # --- FastAPI integration (optional) -----------------------------------

    def mount(self, app: Any, prefix: str = "/governance", tenant_id: str = DEFAULT_TENANT) -> None:
        """Expose the governance endpoints on a FastAPI app.

        Adds ``{prefix}/verify``, ``/summary``, ``/register``, ``/transparency``,
        ``/impact-assessment`` and ``/decisions``. FastAPI is imported here, so the
        rest of the SDK has no web-framework dependency.
        """
        from fastapi import APIRouter  # lazy: only needed if you mount

        router = APIRouter(prefix=prefix)

        @router.get("/verify")
        def _verify() -> dict:
            return self.verify(tenant_id).model_dump(mode="json")

        @router.get("/summary")
        def _summary() -> dict:
            return self.summary(tenant_id).model_dump(mode="json")

        @router.get("/register")
        def _register() -> dict:
            return self.register(tenant_id).model_dump(mode="json")

        @router.get("/transparency")
        def _transparency() -> dict:
            return self.transparency(tenant_id).model_dump(mode="json")

        @router.get("/impact-assessment")
        def _impact() -> dict:
            return self.impact_assessment(tenant_id).model_dump(mode="json")

        @router.get("/decisions")
        def _decisions(limit: int = 50) -> list:
            return [e.model_dump(mode="json") for e in self.decisions(tenant_id, limit)]

        app.include_router(router)
