"""AI use-case impact assessment — the DTA v2.0 artefact, generated live from the log."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst, AnalystQuery
from signalkit.api import create_app
from signalkit.governance.decision_log import impact_assessment


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_impact_assessment_is_generated_per_use_case(analyst):
    analyst.ask(AnalystQuery(offense="theft"))
    ia = analyst.impact_assessment()

    assert ia.use_cases, "an answered query should produce an in-scope use case"
    uc = ia.use_cases[0]
    assert uc.decisions >= 1
    assert uc.risk_category in {"unacceptable", "high", "limited", "minimal"}
    assert uc.affected_groups and uc.risks and uc.mitigations
    assert uc.fairness_considerations and uc.residual_risk
    assert "15 December 2026" in ia.mandatory_from


def test_mitigations_cite_the_faithfulness_eval(analyst):
    analyst.ask(AnalystQuery(offense="theft"))
    ia = analyst.impact_assessment()
    mitigations = " ".join(ia.use_cases[0].mitigations).lower()
    assert "faithfulness" in mitigations          # ties back to RNL-86
    assert "human review" in mitigations


def test_empty_log_yields_no_use_cases():
    ia = impact_assessment([], agency="Demo", accountable_official="Official")
    assert ia.use_cases == []
    assert "No in-scope AI use cases" in ia.statement


def test_impact_assessment_endpoint(analyst):
    client = TestClient(create_app(analyst))
    client.post("/ask", json={"offense": "theft", "region": "adelaide"})

    ia = client.get("/governance/impact-assessment").json()
    assert ia["policy"].startswith("Policy for the responsible use of AI")
    assert ia["use_cases"]
    assert "statement" in ia and "impact assessment" in ia["statement"].lower()
