"""Tests for the auditor-ready compliance report (v5)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst, AnalystQuery
from signalkit.api import create_app
from signalkit.governance import Governor, compliance_report
from signalkit.governance.compliance import MET, ORG


@pytest.fixture()
def analyst(tmp_path):
    a = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    a.ask(AnalystQuery(offense="theft", region="adelaide"))
    return a


def test_report_maps_three_frameworks_with_live_evidence(analyst):
    r = analyst.compliance_report()
    frameworks = [f.framework for f in r.frameworks]
    assert any("42001" in f for f in frameworks)
    assert any("NIST" in f for f in frameworks)
    assert any("DTA" in f for f in frameworks)
    assert r.decisions_covered == 1
    assert r.chain_valid is True
    assert sum(len(f.items) for f in r.frameworks) >= 12


def test_dta_has_the_four_mandatory_requirements(analyst):
    r = analyst.compliance_report()
    dta = next(f for f in r.frameworks if "DTA" in f.framework)
    ids = {i.requirement_id for i in dta.items}
    assert {"DTA-1", "DTA-2", "DTA-3", "DTA-4"} <= ids
    assert all(i.status == MET for i in dta.items)


def test_report_is_honest_about_org_responsibilities(analyst):
    r = analyst.compliance_report()
    statuses = {i.status for f in r.frameworks for i in f.items}
    assert MET in statuses
    assert ORG in statuses  # management-system controls are not claimed as met


def test_evidence_is_drawn_from_the_log():
    # No decisions yet → the report says so honestly.
    empty = compliance_report([], agency="Acme", accountable_official="Jane")
    assert empty.decisions_covered == 0
    populated_note = empty.statement
    assert "Decisions covered: **0**" in populated_note


def test_markdown_statement_reads_as_a_report(analyst):
    stmt = analyst.compliance_report().statement
    assert stmt.startswith("# AI governance compliance report")
    assert "## ISO/IEC 42001" in stmt
    assert "Evidence:" in stmt


def test_compliance_report_endpoint(analyst):
    client = TestClient(create_app(analyst))
    body = client.get("/governance/compliance-report").json()
    assert body["decisions_covered"] == 1
    assert len(body["frameworks"]) == 3


def test_sdk_exposes_compliance_report(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"), agency="Acme", accountable_official="Jane")
    with gov.record(use_case="bot", model_name="m") as rec:
        rec.output("ok")
    r = gov.compliance_report()
    assert r.agency == "Acme" and r.decisions_covered == 1

    app = FastAPI()
    gov.mount(app)
    body = TestClient(app).get("/governance/compliance-report").json()
    assert len(body["frameworks"]) == 3
