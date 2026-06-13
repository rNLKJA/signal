"""Catalogue explorer: endpoints and governed previews, against a faked portal
(no network — the data.sa.gov.au calls are monkeypatched)."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst
from signalkit.api import create_app
from signalkit.data import catalogue


@pytest.fixture()
def client(tmp_path, monkeypatch):
    def fake_search(query="", limit=20, portal="sa"):
        return [
            catalogue.DatasetSummary(
                name="crime-statistics",
                title="Crime statistics",
                organisation="South Australia Police",
                notes="Recorded offences by suburb.",
                num_resources=2,
                datastore_resources=[
                    catalogue.ResourceRef(
                        id="res-1", name="2024-25", format="CSV", datastore_active=True
                    )
                ],
            )
        ]

    def fake_info(name_or_id, portal="sa"):
        if name_or_id != "crime-statistics":
            return None
        return catalogue.DatasetDetail(
            name="crime-statistics",
            title="Crime statistics",
            organisation="South Australia Police",
            notes="Recorded offences.",
            url="https://data.sa.gov.au/data/dataset/crime-statistics",
            resources=[
                catalogue.ResourceRef(
                    id="res-1", name="2024-25", format="CSV", datastore_active=True
                )
            ],
        )

    def fake_preview(resource_id, limit=20, portal="sa"):
        return catalogue.ResourcePreview(
            resource_id=resource_id,
            fields=[{"id": "Suburb", "type": "text"}, {"id": "Count", "type": "numeric"}],
            records=[{"Suburb": "ADELAIDE", "Count": "10"}],
            total=95703,
            truncated=True,
        )

    monkeypatch.setattr(catalogue, "search_datasets", fake_search)
    monkeypatch.setattr(catalogue, "dataset_info", fake_info)
    monkeypatch.setattr(catalogue, "preview_resource", fake_preview)
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


def test_search_datasets(client):
    body = client.get("/datasets?q=crime").json()
    assert body[0]["name"] == "crime-statistics"
    assert body[0]["datastore_resources"][0]["id"] == "res-1"


def test_dataset_detail(client):
    body = client.get("/datasets/crime-statistics").json()
    assert body["organisation"] == "South Australia Police"
    assert body["url"].startswith("https://data.sa.gov.au/")


def test_unknown_dataset_404(client):
    assert client.get("/datasets/does-not-exist").status_code == 404


def _rows_over_months(months, per_month=3):
    fields = [{"id": "Date", "type": "text"}, {"id": "Reading", "type": "numeric"}]
    rows = []
    for i, mth in enumerate(months):
        for k in range(per_month):
            rows.append({"Date": f"{mth}-15", "Reading": str(10 + i + k)})
    return fields, rows


def test_generic_analysis_runs_when_date_and_numeric_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalogue, "fetch_rows",
        lambda rid, max_rows=5000, portal="sa": _rows_over_months(["2025-01", "2025-02", "2025-03", "2025-04"]),
    )
    analyst = Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)
    result = analyst.analyse_resource("res-x", title="Sensor data")
    assert result["analysable"] is True
    assert result["stats"]["value_field"] == "Reading"
    assert result["stats"]["window_start"] == "2025-01"
    assert result["decision_id"].startswith("d-")
    logged = analyst.recent_decisions()[-1]
    assert "generic-analysis" in logged.tags


def test_generic_analysis_falls_back_when_too_short(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalogue, "fetch_rows", lambda rid, max_rows=5000, portal="sa": _rows_over_months(["2025-01"]),
    )
    analyst = Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)
    result = analyst.analyse_resource("res-x")
    assert result["analysable"] is False
    assert "month" in result["reason"].lower()


def test_generic_analysis_falls_back_when_no_numeric(tmp_path, monkeypatch):
    fields = [{"id": "Name", "type": "text"}, {"id": "Suburb", "type": "text"}]
    rows = [{"Name": "a", "Suburb": "ADELAIDE"}] * 10
    monkeypatch.setattr(catalogue, "fetch_rows", lambda rid, max_rows=5000, portal="sa": (fields, rows))
    analyst = Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)
    result = analyst.analyse_resource("res-x")
    assert result["analysable"] is False


def test_preview_is_governed(client):
    body = client.get("/resources/res-1/preview?title=Crime statistics").json()
    assert body["total"] == 95703
    assert body["truncated"] is True
    assert body["decision_id"].startswith("d-")
    # the preview must leave an audit entry (data provenance)
    entry = client.get(f"/decisions/{body['decision_id']}").json()
    assert entry["decision_category"] == "retrieval"
    assert "sa" in entry["tags"]
    assert "res-1" in entry["data_sources"][0]


def test_unknown_portal_rejected(client):
    assert client.get("/datasets?q=x&portal=zz").status_code == 400


def test_portal_threaded_to_catalogue(tmp_path, monkeypatch):
    seen = {}

    def fake_search(query="", limit=20, portal="sa"):
        seen["portal"] = portal
        return []

    monkeypatch.setattr(catalogue, "search_datasets", fake_search)
    c = TestClient(create_app(Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)))
    c.get("/datasets?q=crime&portal=nsw")
    assert seen["portal"] == "nsw"
