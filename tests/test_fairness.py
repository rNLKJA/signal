"""Fairness lens: region comparisons must carry a non-trivial disparate-impact
caveat, and the transparency statement must state the same."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst, CompareQuery
from signalkit.api import create_app


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_compare_carries_a_fairness_note(analyst):
    result = analyst.compare(CompareQuery(offense="theft"))
    note = result.fairness_note.lower()
    assert note
    # the substance, not boilerplate: counts-not-rates + the confounders + don't-rank
    assert "not rates" in note
    assert "population" in note and "reporting" in note and "policing" in note
    assert "rank" in note or "target" in note


def test_fairness_note_is_source_aware(analyst):
    sa = analyst.compare(CompareQuery(offense="theft", source="sa")).fairness_note
    assert "regions" in sa.lower()


def test_compare_endpoint_exposes_fairness_note(analyst):
    client = TestClient(create_app(analyst))
    body = client.post("/compare", json={"offense": "theft"}).json()
    assert body["fairness_note"]
    assert "not rates" in body["fairness_note"].lower()


def test_transparency_statement_states_fairness(analyst):
    ts = analyst.transparency()
    assert ts.fairness
    assert "not rates" in ts.fairness.lower() or "not used to rank" in ts.fairness.lower()
    assert "## Fairness" in ts.statement
