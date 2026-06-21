"""Conformance test for the core governance invariant: answering is logging.

The product's central claim is that the analyst cannot return an answer without
first writing it to the audit log. These tests enforce that at the API boundary
(every answer's decision_id must be retrievable from the log) and structurally
(there is exactly one place that writes to the log). A new answer path that
forgets to log, or a bypass that logs somewhere else, fails CI.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst import core
from signalkit.analyst.core import Analyst
from signalkit.api import create_app


@pytest.fixture()
def client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


def _count(client) -> int:
    """How many decisions are in the log right now."""
    return len(client.get("/decisions?limit=100").json())


def _assert_answer_logged(client, body: dict) -> None:
    """An answer is only valid if its decision_id is in the audit log."""
    assert "decision_id" in body, "answer carries no decision_id"
    got = client.get(f"/decisions/{body['decision_id']}")
    assert got.status_code == 200, "answer's decision_id is not in the audit log"
    assert got.json()["decision_id"] == body["decision_id"]


# --- every answer is logged exactly once, and retrievable -------------------


def test_ask_logs_exactly_one_and_is_retrievable(client):
    before = _count(client)
    body = client.post("/ask", json={"offense": "theft", "region": "adelaide"}).json()
    assert _count(client) == before + 1
    _assert_answer_logged(client, body)


def test_compare_logs_exactly_one_and_is_retrievable(client):
    before = _count(client)
    body = client.post("/compare", json={"offense": "theft"}).json()
    assert _count(client) == before + 1
    _assert_answer_logged(client, body)


def test_review_logs_exactly_one_and_is_retrievable(client):
    answer = client.post("/ask", json={"offense": "theft"}).json()
    before = _count(client)
    body = client.post(
        f"/decisions/{answer['decision_id']}/review",
        json={"reviewer": "officer@agency.gov.au"},
    ).json()
    assert _count(client) == before + 1
    _assert_answer_logged(client, body)


# --- error paths create no phantom entries ----------------------------------


def test_no_match_does_not_log(client):
    before = _count(client)
    r = client.post("/ask", json={"offense": "space piracy"})
    assert r.status_code == 404
    assert _count(client) == before  # a failed answer logs nothing


def test_validation_error_does_not_log(client):
    before = _count(client)
    r = client.post("/ask", json={"months": 999})  # outside the allowed range
    assert r.status_code == 422
    assert _count(client) == before


# --- the invariant has teeth ------------------------------------------------


def test_unlogged_answer_is_detectable(client):
    # The retrievability check is what catches a bypass: an answer whose
    # decision_id is not in the log fails it.
    fabricated = {"decision_id": "d-deadbeef"}
    with pytest.raises(AssertionError):
        _assert_answer_logged(client, fabricated)


def test_single_log_choke_point_by_construction():
    """Logging happens in exactly one place — the _commit choke point.

    Every answer path funnels through Analyst._commit, so a new path that logs
    directly (a bypass, or double-logging) changes this count and fails CI.
    """
    src = inspect.getsource(core)
    assert src.count("self._logger.log(") == 1
    # ...and that one call lives inside _commit.
    commit_src = inspect.getsource(Analyst._commit)
    assert "self._logger.log(" in commit_src
