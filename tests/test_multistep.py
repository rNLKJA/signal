"""Tests for the governed multi-step analyst (v3 workstream 1).

A compound answer must be as traceable as a single one: each step is its own
logged, faithfulness-checked decision, linked to a parent, and the whole tree
sits in the tamper-evident chain.
"""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst, MultiQuery
from signalkit.api import create_app


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


@pytest.fixture()
def client(analyst):
    return TestClient(create_app(analyst))


def test_multi_step_builds_a_linked_decision_tree(analyst):
    ans = analyst.ask_multi(MultiQuery(
        offenses=["theft", "robbery", "fraud"], region="adelaide", months=18,
    ))
    # one parent, one child per offence
    assert len(ans.steps) == 3
    assert ans.decision_id.startswith("d-")

    parent = analyst.get_decision(ans.decision_id)
    child_ids = [s.decision_id for s in ans.steps]
    assert parent.child_decision_ids == child_ids

    # every child links back to the parent, and is itself in the log
    for cid in child_ids:
        child = analyst.get_decision(cid)
        assert child is not None
        assert child.parent_decision_id == ans.decision_id


def test_whole_tree_is_in_the_tamper_evident_chain(analyst):
    analyst.ask_multi(MultiQuery(offenses=["theft", "robbery"], region="adelaide"))
    report = analyst.verify_log()
    assert report.valid
    assert report.chained_entries == 3  # 2 children + 1 parent


def test_synthesis_is_faithfulness_checked(analyst):
    ans = analyst.ask_multi(MultiQuery(offenses=["theft", "robbery"], region="adelaide"))
    # the synthesis only states figures drawn from the children, so it passes
    assert ans.faithfulness_score == 1.0
    assert "governed decision" in ans.synthesis
    for s in ans.steps:
        assert s.offense.capitalize() in ans.synthesis


def test_human_review_propagates_to_the_parent(analyst):
    ans = analyst.ask_multi(MultiQuery(offenses=["theft", "robbery"], region="adelaide"))
    # if any step needs review, the composite decision does too
    assert ans.human_review_required == any(s.human_review_required for s in ans.steps)


# --- API --------------------------------------------------------------------


def test_ask_multi_endpoint_returns_a_traceable_tree(client):
    body = client.post("/ask/multi", json={
        "offenses": ["theft", "robbery"], "region": "adelaide", "months": 18,
    }).json()
    assert body["decision_id"]
    assert len(body["steps"]) == 2
    # every node in the tree resolves in the audit log
    assert client.get(f"/decisions/{body['decision_id']}").status_code == 200
    for step in body["steps"]:
        got = client.get(f"/decisions/{step['decision_id']}")
        assert got.status_code == 200
        assert got.json()["parent_decision_id"] == body["decision_id"]


def test_ask_multi_requires_at_least_two_offences(client):
    r = client.post("/ask/multi", json={"offenses": ["theft"], "region": "adelaide"})
    assert r.status_code == 422  # min_length=2


def test_ask_multi_no_match_is_404(client):
    r = client.post("/ask/multi", json={"offenses": ["theft", "space piracy"]})
    assert r.status_code == 404
