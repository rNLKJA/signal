"""Data layer: snapshot integrity, offline switching, stale-while-revalidate."""

import types

import pytest

import signalkit.data.sa_crime as sa_crime
from signalkit.data.sa_crime import MonthlyRecord, get_records, load_snapshot


@pytest.fixture(autouse=True)
def reset_cache():
    sa_crime._live_cache = None
    sa_crime._refresh_in_flight = False
    yield
    sa_crime._live_cache = None
    sa_crime._refresh_in_flight = False


def test_snapshot_loads_real_records():
    records, label = load_snapshot()
    assert len(records) > 1000
    assert "snapshot" in label
    assert all(isinstance(r, MonthlyRecord) for r in records[:5])


def test_snapshot_months_are_well_formed():
    records, _ = load_snapshot()
    months = {r.month for r in records}
    assert all(len(m) == 7 and m[4] == "-" for m in months)
    # the bundled window spans 2024-07 .. 2026-03
    assert min(months) == "2024-07"
    assert max(months) == "2026-03"


def test_snapshot_divisions_are_anzsoc():
    records, _ = load_snapshot()
    divisions = {r.offense_division for r in records}
    assert divisions == {"Offences against the person", "Offences against property"}


def test_snapshot_counts_positive():
    records, _ = load_snapshot()
    assert all(r.count >= 1 for r in records)


def test_offline_env_forces_snapshot(monkeypatch):
    monkeypatch.setenv("SIGNAL_OFFLINE", "1")
    records, label = get_records()
    assert "snapshot" in label
    assert records


def test_offline_argument_overrides(monkeypatch):
    monkeypatch.delenv("SIGNAL_OFFLINE", raising=False)
    records, label = get_records(offline=True)
    assert "snapshot" in label


def test_online_serves_snapshot_immediately_while_refreshing(monkeypatch):
    """First online call must not block: snapshot now, refresh in background."""
    monkeypatch.delenv("SIGNAL_OFFLINE", raising=False)
    spawned = []
    monkeypatch.setattr(
        sa_crime.threading,
        "Thread",
        lambda **kw: type("T", (), {"start": lambda self: spawned.append(kw)})(),
    )
    records, label = get_records(offline=False)
    assert "live refresh in progress" in label
    assert records
    assert len(spawned) == 1  # exactly one refresh kicked off


def test_live_cache_served_once_populated(monkeypatch):
    monkeypatch.delenv("SIGNAL_OFFLINE", raising=False)
    fake = [
        MonthlyRecord(
            month="2026-01",
            region="ADELAIDE",
            offense="Theft",
            offense_division="Offences against property",
            count=1,
        )
    ]
    monkeypatch.setattr(sa_crime, "fetch_live", lambda: (fake, "live-test"))
    sa_crime._refresh_in_flight = True
    sa_crime._refresh_live_cache()  # run the background body synchronously
    records, label = get_records(offline=False)
    assert label == "live-test"
    assert records == fake
    assert sa_crime._refresh_in_flight is False


def test_taxonomy_harmonisation_maps_both_vocabularies():
    # Pre- and post-2025 SA Police labels must land on the same category.
    assert sa_crime._map_offense("THEFT AND RELATED OFFENCES") == "Theft"
    assert sa_crime._map_offense("THEFT") == "Theft"
    assert sa_crime._map_offense("ACTS INTENDED TO CAUSE INJURY") == "Assault and injury"
    assert sa_crime._map_offense("ASSAULT") == "Assault and injury"


def _raw(date, suburb, l1, l2, n):
    return {
        "Reported Date": date, "Suburb - Incident": suburb,
        "Offence Level 1 Description": l1, "Offence Level 2 Description": l2,
        "Offence count": str(n),
    }


def test_aggregate_harmonises_across_the_taxonomy_change():
    rows = [
        _raw("01/07/2024", "ADELAIDE", "OFFENCES AGAINST PROPERTY", "THEFT AND RELATED OFFENCES", 3),
        _raw("15/08/2025", "ADELAIDE", "OFFENCES AGAINST PROPERTY", "THEFT", 2),
    ]
    recs = sa_crime._aggregate(rows)
    # both vocabularies collapse to one offence and one division
    assert {r.offense for r in recs} == {"Theft"}
    assert {r.offense_division for r in recs} == {"Offences against property"}
    assert {r.month for r in recs} == {"2024-07", "2025-08"}


def test_fetch_resource_falls_back_when_source_empty(monkeypatch):
    """data.sa newest-FY datastore can be empty; the fallback must fill it in."""
    import httpx

    def fake_get(self, base, params=None):
        sa = base.startswith("https://data.sa.gov.au")
        records = [] if sa else [
            _raw("01/07/2024", "ADELAIDE", "OFFENCES AGAINST PROPERTY", "THEFT", 5),
        ]
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": {"records": records, "total": len(records)}},
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with httpx.Client() as client:
        rows = sa_crime._fetch_resource("res", client)
    assert len(rows) == 1  # came from the data.gov.au fallback, not empty data.sa
