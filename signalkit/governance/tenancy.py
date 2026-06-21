"""Multi-tenant governance: a tenant-scoped audit log.

One deployment can serve many organisations without their records ever mixing.
Each tenant gets its own ``DecisionLogger`` over its own storage, so each has an
*independent* tamper-evident chain. Isolation is structural, not a filter applied
after the fact: a tenant simply has no handle to another tenant's log, and a
record carries its ``tenant_id`` inside the signed content, so it cannot be moved
between tenants without breaking its hash.

A single-tenant deployment never needs this — it just uses ``DecisionLogger``
directly, and entries leave ``tenant_id`` unset.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from signalkit.governance.audit_store import (
    AuditStore,
    InMemoryAuditStore,
    JsonlAuditStore,
    SqliteAuditStore,
)
from signalkit.governance.decision_log import (
    ChainVerification,
    DecisionEntry,
    DecisionLogger,
    GovernanceSummary,
    ImpactAssessment,
    TransparencyStatement,
    UseCaseRegister,
    impact_assessment,
    register,
    summarise,
    transparency_statement,
)

#: The implicit tenant when none is given (keeps single-tenant use unchanged).
DEFAULT_TENANT = "public"

#: Tenant ids must be safe to use as a storage namespace.
_TENANT_RE = re.compile(r"[A-Za-z0-9._-]+")


def _safe_tenant(tenant_id: Optional[str]) -> str:
    tid = (tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT
    if not _TENANT_RE.fullmatch(tid):
        raise ValueError(f"invalid tenant_id {tenant_id!r}: use letters, digits, . _ -")
    return tid


class TenantLog:
    """Routes governed decisions to a per-tenant audit log.

    Built from a ``store_factory(tenant_id) -> AuditStore``; the convenience
    constructors cover the common backends. Every method takes a ``tenant_id`` and
    operates only on that tenant's chain.
    """

    def __init__(self, store_factory: Callable[[str], AuditStore]) -> None:
        self._factory = store_factory
        self._loggers: Dict[str, DecisionLogger] = {}

    # --- constructors ------------------------------------------------------

    @classmethod
    def in_memory(cls) -> "TenantLog":
        """Ephemeral per-tenant stores — for tests and demos."""
        stores: Dict[str, InMemoryAuditStore] = {}
        return cls(lambda tid: stores.setdefault(tid, InMemoryAuditStore()))

    @classmethod
    def jsonl_dir(cls, base_dir: str | Path) -> "TenantLog":
        """A JSONL file per tenant under ``base_dir/<tenant>/decisions.jsonl``."""
        base = Path(base_dir)
        return cls(lambda tid: JsonlAuditStore(base / tid / "decisions.jsonl"))

    @classmethod
    def sqlite_dir(cls, base_dir: str | Path) -> "TenantLog":
        """A SQLite database per tenant under ``base_dir/<tenant>/audit.db``."""
        base = Path(base_dir)
        return cls(lambda tid: SqliteAuditStore(base / tid / "audit.db"))

    # --- per-tenant operations --------------------------------------------

    def _logger(self, tenant_id: Optional[str]) -> DecisionLogger:
        tid = _safe_tenant(tenant_id)
        if tid not in self._loggers:
            self._loggers[tid] = DecisionLogger(self._factory(tid))
        return self._loggers[tid]

    def log(self, entry: DecisionEntry, tenant_id: str = DEFAULT_TENANT) -> None:
        """Stamp the entry with its tenant and append it to that tenant's chain."""
        entry.tenant_id = _safe_tenant(tenant_id)
        self._logger(tenant_id).log(entry)

    def read_all(self, tenant_id: str = DEFAULT_TENANT) -> List[DecisionEntry]:
        return self._logger(tenant_id).read_all()

    def verify(self, tenant_id: str = DEFAULT_TENANT) -> ChainVerification:
        return self._logger(tenant_id).verify()

    def summary(self, tenant_id: str = DEFAULT_TENANT) -> GovernanceSummary:
        return summarise(self.read_all(tenant_id))

    def register(self, tenant_id: str, agency: str, accountable_official: str) -> UseCaseRegister:
        return register(self.read_all(tenant_id), agency, accountable_official)

    def transparency(self, tenant_id: str, agency: str, accountable_official: str) -> TransparencyStatement:
        return transparency_statement(self.read_all(tenant_id), agency, accountable_official)

    def impact_assessment(self, tenant_id: str, agency: str, accountable_official: str) -> ImpactAssessment:
        return impact_assessment(self.read_all(tenant_id), agency, accountable_official)


# ---------------------------------------------------------------------------
# Per-tenant authentication: map an API key to a tenant
# ---------------------------------------------------------------------------

def parse_tenant_keys(spec: Optional[str]) -> Dict[str, str]:
    """Parse ``'key1:tenantA,key2:tenantB'`` (e.g. from ``SIGNAL_TENANT_KEYS``).

    Returns a {api_key: tenant_id} map. Malformed pairs are skipped.
    """
    out: Dict[str, str] = {}
    for pair in (spec or "").split(","):
        pair = pair.strip()
        if ":" in pair:
            key, tenant = pair.split(":", 1)
            if key.strip() and tenant.strip():
                out[key.strip()] = _safe_tenant(tenant.strip())
    return out


def tenant_for_api_key(api_key: Optional[str], key_map: Optional[Dict[str, str]] = None) -> str:
    """Resolve an API key to its tenant, or DEFAULT_TENANT when absent/unmapped."""
    if api_key and key_map:
        return key_map.get(api_key, DEFAULT_TENANT)
    return DEFAULT_TENANT
