"""API surface: endpoints, error mapping, audit readback."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst
from signalkit.api import create_app


@pytest.fixture()
def client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


def test_dashboard_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Signal" in response.text
    assert "audit" in response.text.lower()


def test_index(client):
    body = client.get("/api").json()
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


def test_decision_resolves_by_id(client):
    decision_id = client.post("/ask", json={"offense": "burglary"}).json()["decision_id"]
    entry = client.get(f"/decisions/{decision_id}").json()
    assert entry["decision_id"] == decision_id
    assert entry["model_name"]
    assert entry["data_sources"]


def test_unknown_decision_id_404(client):
    response = client.get("/decisions/d-doesnotexist")
    assert response.status_code == 404
    assert "d-doesnotexist" in response.json()["detail"]


def test_governance_summary(client):
    client.post("/ask", json={"offense": "burglary"})
    client.post("/ask", json={"offense": "robbery"})
    summary = client.get("/governance/summary").json()
    assert summary["total_decisions"] == 2
    assert summary["by_risk_category"] == {"limited": 2}
    assert "signal-stats-v1 (deterministic)" in summary["by_model"]
    assert summary["first_decision_at"] <= summary["last_decision_at"]


def test_compare_five_boroughs(client):
    body = client.post("/compare", json={"offense": "burglary", "months": 12}).json()
    boroughs = [s["borough"] for s in body["series"]]
    assert len(boroughs) == 5
    assert "BROOKLYN" in boroughs and "STATEN ISLAND" in boroughs
    # series aligned: every borough covers the same window
    windows = {tuple(s["monthly_counts"].keys()) for s in body["series"]}
    assert len(windows) == 1
    assert len(next(iter(windows))) == 12
    assert body["decision_id"].startswith("d-")
    assert body["narrative"]


def test_compare_is_audit_logged(client):
    decision_id = client.post("/compare", json={"offense": "robbery"}).json()["decision_id"]
    entry = client.get(f"/decisions/{decision_id}").json()
    assert "borough-comparison" in entry["tags"]


def test_compare_bad_offense_404(client):
    response = client.post("/compare", json={"offense": "space piracy"})
    assert response.status_code == 404
    assert "offenses" in response.json()["detail"]["valid_values"]


def test_ask_includes_law_category_split(client):
    stats = client.post("/ask", json={"offense": "burglary"}).json()["stats"]
    assert stats["by_law_category"]
    assert sum(stats["by_law_category"].values()) == stats["total_complaints"]


def test_dashboard_gzipped_when_accepted(client):
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert "max-age=300" in response.headers.get("cache-control", "")
