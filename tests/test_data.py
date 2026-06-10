"""Data layer: snapshot integrity, offline switching, stale-while-revalidate."""

import pytest

import signalkit.data.nypd as nypd
from signalkit.data.nypd import MonthlyRecord, get_records, load_snapshot


@pytest.fixture(autouse=True)
def reset_cache():
    nypd._live_cache = None
    nypd._refresh_in_flight = False
    yield
    nypd._live_cache = None
    nypd._refresh_in_flight = False


def test_snapshot_loads_real_records():
    records, label = load_snapshot()
    assert len(records) > 1000
    assert "snapshot" in label
    assert all(isinstance(r, MonthlyRecord) for r in records[:5])


def test_snapshot_months_are_well_formed():
    records, _ = load_snapshot()
    months = {r.month for r in records}
    assert all(len(m) == 7 and m[4] == "-" for m in months)
    # the bundled window spans 2025-01 .. 2026-03
    assert min(months) == "2025-01"
    assert max(months) == "2026-03"


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
        nypd.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: spawned.append(kw)})()
    )
    records, label = get_records(offline=False)
    assert "live refresh in progress" in label
    assert records
    assert len(spawned) == 1  # exactly one refresh kicked off


def test_live_cache_served_once_populated(monkeypatch):
    monkeypatch.delenv("SIGNAL_OFFLINE", raising=False)
    fake = [MonthlyRecord(month="2026-01", borough="X", offense="Y", law_category="FELONY", count=1)]
    monkeypatch.setattr(nypd, "fetch_live", lambda: (fake, "live-test"))
    nypd._refresh_in_flight = True
    nypd._refresh_live_cache()  # run the background body synchronously
    records, label = get_records(offline=False)
    assert label == "live-test"
    assert records == fake
    assert nypd._refresh_in_flight is False
