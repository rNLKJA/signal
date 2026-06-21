"""Tests for multi-tenant governance (v3).

Isolation is the whole point: each tenant has its own tamper-evident chain, one
tenant can't see or break another's, and a record is cryptographically bound to
its tenant. A single-tenant deployment is unaffected.
"""

import pytest

from signalkit.governance import (
    DEFAULT_TENANT,
    DecisionEntry,
    RiskCategory,
    TenantLog,
    parse_tenant_keys,
    tenant_for_api_key,
)


def _entry(decision: str) -> DecisionEntry:
    return DecisionEntry(
        model_name="m",
        input_summary=f"in {decision}",
        model_output_summary=f"out {decision}",
        decision_made=decision,
        risk_category=RiskCategory.limited,
        human_review_required=False,
    )


# --- isolation --------------------------------------------------------------


def test_tenants_have_independent_chains():
    log = TenantLog.in_memory()
    log.log(_entry("a1"), tenant_id="acme")
    log.log(_entry("a2"), tenant_id="acme")
    log.log(_entry("b1"), tenant_id="globex")

    acme = [e.decision_made for e in log.read_all("acme")]
    globex = [e.decision_made for e in log.read_all("globex")]
    assert acme == ["a1", "a2"]
    assert globex == ["b1"]  # globex cannot see acme's records
    assert log.verify("acme").valid
    assert log.verify("globex").valid


def test_entry_is_stamped_with_its_tenant():
    log = TenantLog.in_memory()
    log.log(_entry("x"), tenant_id="acme")
    assert log.read_all("acme")[0].tenant_id == "acme"


def test_tenant_id_is_part_of_the_signed_content():
    # An entry cannot be moved to another tenant without breaking its hash.
    log = TenantLog.in_memory()
    log.log(_entry("x"), tenant_id="acme")
    entry = log.read_all("acme")[0]
    h = entry.entry_hash
    moved = entry.model_copy(update={"tenant_id": "globex"})
    assert moved.content_hash() != h


def test_tampering_one_tenant_does_not_affect_another():
    log = TenantLog.in_memory()
    log.log(_entry("a1"), tenant_id="acme")
    log.log(_entry("b1"), tenant_id="globex")
    # corrupt acme's store directly
    store = log._logger("acme")._store
    store._lines[0] = store._lines[0].replace('"decision_made":"a1"', '"decision_made":"X"')

    assert log.verify("acme").valid is False
    assert log.verify("globex").valid is True  # globex is untouched


def test_default_tenant_when_unset():
    log = TenantLog.in_memory()
    log.log(_entry("x"))  # no tenant_id
    assert log.read_all()[0].tenant_id == DEFAULT_TENANT
    assert log.read_all(DEFAULT_TENANT)[0].decision_made == "x"


def test_invalid_tenant_id_rejected():
    log = TenantLog.in_memory()
    with pytest.raises(ValueError):
        log.log(_entry("x"), tenant_id="../etc/passwd")


# --- durable, isolated backends --------------------------------------------


def test_isolation_holds_over_sqlite(tmp_path):
    log = TenantLog.sqlite_dir(str(tmp_path))
    log.log(_entry("a1"), tenant_id="acme")
    log.log(_entry("b1"), tenant_id="globex")
    assert [e.decision_made for e in log.read_all("acme")] == ["a1"]
    assert [e.decision_made for e in log.read_all("globex")] == ["b1"]
    # separate databases on disk
    assert (tmp_path / "acme" / "audit.db").exists()
    assert (tmp_path / "globex" / "audit.db").exists()


def test_per_tenant_artefacts_scope_to_the_tenant():
    log = TenantLog.in_memory()
    log.log(_entry("a1"), tenant_id="acme")
    log.log(_entry("b1"), tenant_id="globex")
    assert log.summary("acme").total_decisions == 1
    reg = log.register("acme", agency="Acme", accountable_official="Jane")
    assert reg.agency == "Acme" and reg.use_cases


# --- per-tenant auth --------------------------------------------------------


def test_parse_and_resolve_tenant_keys():
    keys = parse_tenant_keys("k-acme:acme, k-globex:globex, malformed")
    assert keys == {"k-acme": "acme", "k-globex": "globex"}
    assert tenant_for_api_key("k-acme", keys) == "acme"
    assert tenant_for_api_key("unknown", keys) == DEFAULT_TENANT
    assert tenant_for_api_key(None, keys) == DEFAULT_TENANT
    assert tenant_for_api_key("k-acme", None) == DEFAULT_TENANT
