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
    response = client.post("/ask", json={"offense": "theft", "region": "adelaide"})
    assert response.status_code == 200
    body = response.json()
    assert body["decision_id"].startswith("d-")
    assert body["stats"]["total_offences"] > 0
    assert body["narrative"]


def test_ask_traceable_in_decisions(client):
    decision_id = client.post("/ask", json={"offense": "robbery"}).json()["decision_id"]
    decisions = client.get("/decisions").json()
    assert decision_id in [d["decision_id"] for d in decisions]


def test_ask_bad_filter_404_with_suggestions(client):
    response = client.post("/ask", json={"offense": "space piracy"})
    assert response.status_code == 404
    assert "regions" in response.json()["detail"]["valid_values"]


def test_ask_validates_months(client):
    response = client.post("/ask", json={"months": 99})
    assert response.status_code == 422


def test_decisions_limit_validated(client):
    assert client.get("/decisions?limit=500").status_code == 422


def test_decision_resolves_by_id(client):
    decision_id = client.post("/ask", json={"offense": "theft"}).json()["decision_id"]
    entry = client.get(f"/decisions/{decision_id}").json()
    assert entry["decision_id"] == decision_id
    assert entry["model_name"]
    assert entry["data_sources"]


def test_unknown_decision_id_404(client):
    response = client.get("/decisions/d-doesnotexist")
    assert response.status_code == 404
    assert "d-doesnotexist" in response.json()["detail"]


def test_record_review_logged_and_counted(client):
    decision_id = client.post("/ask", json={"offense": "theft"}).json()["decision_id"]
    r = client.post(
        f"/decisions/{decision_id}/review",
        json={"reviewer": "analyst@sapol.sa.gov.au", "note": "Looks right."},
    )
    assert r.status_code == 200
    review = r.json()
    assert review["decision_category"] == "review"
    assert review["reviews_decision_id"] == decision_id
    assert review["human_reviewer"] == "analyst@sapol.sa.gov.au"
    # the review is its own audit entry, appended (log never mutated)
    assert decision_id in [d["decision_id"] for d in client.get("/decisions").json()]
    summary = client.get("/governance/summary").json()
    assert summary["reviews_recorded"] == 1
    assert summary["total_decisions"] == 1  # the review is not counted as a decision


def test_override_requires_reason(client):
    decision_id = client.post("/ask", json={"offense": "theft"}).json()["decision_id"]
    bad = client.post(
        f"/decisions/{decision_id}/review",
        json={"reviewer": "analyst", "override": True},
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/decisions/{decision_id}/review",
        json={"reviewer": "analyst", "override": True, "override_reason": "Anomaly was a data error."},
    )
    assert ok.status_code == 200
    assert ok.json()["override_applied"] is True


def test_review_unknown_decision_404(client):
    r = client.post("/decisions/d-nope/review", json={"reviewer": "analyst"})
    assert r.status_code == 404


def test_outstanding_reviews_tracked(client):
    # an anomalous query flags human review; until reviewed it is outstanding
    client.post("/ask", json={"offense": "theft", "region": "adelaide"})
    before = client.get("/governance/summary").json()
    if before["human_review_required_count"]:
        assert before["outstanding_reviews"] == before["human_review_required_count"]


def test_governance_summary(client):
    client.post("/ask", json={"offense": "theft"})
    client.post("/ask", json={"offense": "robbery"})
    summary = client.get("/governance/summary").json()
    assert summary["total_decisions"] == 2
    assert summary["by_risk_category"] == {"limited": 2}
    assert "signal-stats-v1 (deterministic)" in summary["by_model"]
    assert summary["first_decision_at"] <= summary["last_decision_at"]


def test_compare_across_regions(client):
    body = client.post("/compare", json={"offense": "theft", "months": 12}).json()
    regions = [s["region"] for s in body["series"]]
    assert "ADELAIDE" in regions
    # the folded tail and withheld-suburb buckets are not comparable places
    assert "OTHER SA AREAS" not in regions
    assert "NOT DISCLOSED" not in regions
    assert len(regions) >= 10
    # series aligned: every region covers the same window
    windows = {tuple(s["monthly_counts"].keys()) for s in body["series"]}
    assert len(windows) == 1
    assert len(next(iter(windows))) == 12
    assert body["decision_id"].startswith("d-")
    assert body["narrative"]


def test_compare_is_audit_logged(client):
    decision_id = client.post("/compare", json={"offense": "robbery"}).json()["decision_id"]
    entry = client.get(f"/decisions/{decision_id}").json()
    assert "region-comparison" in entry["tags"]


def test_compare_bad_offense_404(client):
    response = client.post("/compare", json={"offense": "space piracy"})
    assert response.status_code == 404
    assert "offenses" in response.json()["detail"]["valid_values"]


def test_ask_includes_offense_division_split(client):
    stats = client.post("/ask", json={"offense": "theft"}).json()["stats"]
    assert stats["by_offense_division"]
    assert sum(stats["by_offense_division"].values()) == stats["total_offences"]


def test_decisions_csv_export(client):
    client.post("/ask", json={"offense": "theft"})
    r = client.get("/decisions.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("decision_id,timestamp,model_name")
    assert len(lines) >= 2  # header + at least one decision


def test_dashboard_gzipped_when_accepted(client):
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert "max-age=300" in response.headers.get("cache-control", "")
