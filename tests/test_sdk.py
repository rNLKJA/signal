"""Tests for the governance SDK (v4 drop-in adapter).

The SDK is the product's adoption path: a few lines give any app a governed,
tamper-evident audit trail and the DTA artefacts. These tests use a generic
support-bot, not the crime app, to keep it honest about being domain-agnostic.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from signalkit.governance import Governor, TenantLog


def _bot(q: str) -> str:
    return f"To do {q}, follow these steps..."


def test_record_logs_a_governed_decision(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"), agency="Acme", accountable_official="Jane")
    with gov.record(use_case="support-bot", model_name="gpt-4o", input_summary="reset password") as rec:
        rec.output(_bot("reset password"))
    assert len(gov.decisions()) == 1
    assert gov.verify().valid
    logged = gov.decisions()[0]
    assert logged.decision_id == rec.decision_id
    assert logged.use_case == "support-bot"
    assert logged.agency == "Acme"


def test_a_failed_answer_logs_nothing(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"))
    with pytest.raises(RuntimeError):
        with gov.record(use_case="support-bot", model_name="gpt-4o"):
            raise RuntimeError("model timed out")
    assert gov.decisions() == []  # an exception is not a decision


def test_decision_id_available_before_block_ends(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"))
    with gov.record(use_case="x", model_name="m") as rec:
        # the caller can return this id to the user mid-flight
        assert rec.decision_id.startswith("d-")
    assert gov.decisions()[0].decision_id == rec.decision_id


def test_artefacts_generate_from_the_sdk(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"), agency="Acme", accountable_official="Jane")
    with gov.record(use_case="support-bot", model_name="m") as rec:
        rec.output("ok")
    assert gov.summary().total_decisions == 1
    assert gov.register().agency == "Acme"
    assert gov.transparency().statement
    assert gov.impact_assessment().use_cases


def test_sdk_is_multi_tenant(tmp_path):
    gov = Governor(TenantLog.sqlite_dir(str(tmp_path)))
    with gov.record(use_case="bot", model_name="m", tenant_id="acme") as rec:
        rec.output("a")
    with gov.record(use_case="bot", model_name="m", tenant_id="globex") as rec:
        rec.output("b")
    assert len(gov.decisions("acme")) == 1
    assert len(gov.decisions("globex")) == 1
    assert gov.decisions("acme")[0].tenant_id == "acme"
    assert gov.verify("acme").valid and gov.verify("globex").valid


def test_tamper_evidence_through_the_sdk(tmp_path):
    p = tmp_path / "d.jsonl"
    gov = Governor(str(p))
    with gov.record(use_case="bot", model_name="m") as rec:
        rec.output("a")
    with gov.record(use_case="bot", model_name="m") as rec:
        rec.output("b")
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace('"use_case":"bot"', '"use_case":"HACKED"')
    p.write_text("\n".join(lines) + "\n")
    assert gov.verify().valid is False


def test_mount_exposes_governance_endpoints(tmp_path):
    gov = Governor(str(tmp_path / "d.jsonl"), agency="Acme", accountable_official="Jane")
    with gov.record(use_case="support-bot", model_name="m") as rec:
        rec.output("ok")

    app = FastAPI()
    gov.mount(app)
    client = TestClient(app)

    assert client.get("/governance/verify").json()["valid"] is True
    assert client.get("/governance/summary").json()["total_decisions"] == 1
    assert client.get("/governance/register").json()["agency"] == "Acme"
    assert client.get("/governance/transparency").json()["statement"]
    assert len(client.get("/governance/decisions").json()) == 1
