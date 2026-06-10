"""API surface: endpoints, error mapping, audit readback."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst
from signalkit.api import create_app


@pytest.fixture()
def client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


def test_index(client):
    body = client.get("/").json()
    assert body["service"] == "signal"
    assert "/ask (POST)" in body["endpoints"]


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ask_returns_decision_id(client):
    response = client.post("/ask", json={"offense": "burglary", "borough": "brooklyn"})
    assert response.status_code == 200
    body = response.json()
    assert body["decision_id"].startswith("d-")
    assert body["stats"]["total_complaints"] > 0
    assert body["narrative"]


def test_ask_traceable_in_decisions(client):
    decision_id = client.post("/ask", json={"offense": "robbery"}).json()["decision_id"]
    decisions = client.get("/decisions").json()
    assert decision_id in [d["decision_id"] for d in decisions]


def test_ask_bad_filter_404_with_suggestions(client):
    response = client.post("/ask", json={"offense": "space piracy"})
    assert response.status_code == 404
    assert "boroughs" in response.json()["detail"]["valid_values"]


def test_ask_validates_months(client):
    response = client.post("/ask", json={"months": 99})
    assert response.status_code == 422


def test_decisions_limit_validated(client):
    assert client.get("/decisions?limit=500").status_code == 422
