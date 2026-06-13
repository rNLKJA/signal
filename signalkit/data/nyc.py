"""
signalkit/data/nyc.py
=====================
Monthly complaint aggregates from NYC Open Data (NYPD complaint datasets),
re-added alongside the SA Police layer as a selectable source.

Two Socrata datasets are unioned into a rolling window (previous full year +
current year to date):

  - NYPD Complaint Data Historic   (qgea-i56i) — closed prior years
  - NYPD Complaint Data Current YTD (5uac-w243) — current year

Aggregation is server-side SoQL (count by month x borough x offence x law
category), so no raw incident rows leave the API. Records use the same
``MonthlyRecord`` shape as the SA layer — borough maps to ``region`` and the
NYPD law category maps to ``offense_division`` — so the analyst, API, dashboard
and map all work unchanged across both sources.

A real-data snapshot is bundled at ``sample/nyc_monthly.json``; set
``SIGNAL_OFFLINE=1`` to force it. The source contains complaint dates typo'd as
far back as year 1012, so every query bounds the date range explicitly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date

import httpx

from signalkit.data.sa_crime import DataUnavailable, MonthlyRecord
from signalkit.data.sa_crime import SNAPSHOT_PATH as _SA_SNAPSHOT_PATH

AGENCY = "NYPD"
HISTORIC_URL = "https://data.cityofnewyork.us/resource/qgea-i56i.json"
YTD_URL = "https://data.cityofnewyork.us/resource/5uac-w243.json"
SNAPSHOT_PATH = _SA_SNAPSHOT_PATH.parent / "nyc_monthly.json"
TOP_OFFENSES = 25
OTHER_OFFENSE = "OTHER OFFENSES"

_CACHE_TTL_SECONDS = 3600
_live_cache: tuple[float, list["MonthlyRecord"], str] | None = None
_refresh_lock = threading.Lock()
_refresh_in_flight = False

_LAW_CAT = {"FELONY": "Felony", "MISDEMEANOR": "Misdemeanor", "VIOLATION": "Violation"}


def _soql_fetch(base_url: str, start: date, end: date, client: httpx.Client) -> list[dict]:
    params = {
        "$select": (
            "date_trunc_ym(cmplnt_fr_dt) AS month, boro_nm, ofns_desc, "
            "law_cat_cd, count(*) AS n"
        ),
        "$where": (
            f"cmplnt_fr_dt >= '{start.isoformat()}T00:00:00' "
            f"AND cmplnt_fr_dt <= '{end.isoformat()}T23:59:59'"
        ),
        "$group": "month, boro_nm, ofns_desc, law_cat_cd",
        "$limit": "50000",
    }
    resp = client.get(base_url, params=params)
    resp.raise_for_status()
    return resp.json()


def _aggregate(raw: list[dict]) -> list[MonthlyRecord]:
    cells: dict[tuple[str, str, str, str], int] = {}
    offense_totals: dict[str, int] = {}
    for row in raw:
        if not all(row.get(k) for k in ("month", "boro_nm", "ofns_desc", "law_cat_cd", "n")):
            continue
        month = row["month"][:7]
        region = row["boro_nm"]
        offense = row["ofns_desc"]
        division = _LAW_CAT.get(row["law_cat_cd"], row["law_cat_cd"].title())
        n = int(row["n"])
        cells[(month, region, offense, division)] = cells.get((month, region, offense, division), 0) + n
        offense_totals[offense] = offense_totals.get(offense, 0) + n

    keep = {o for o, _ in sorted(offense_totals.items(), key=lambda kv: -kv[1])[:TOP_OFFENSES]}
    folded: dict[tuple[str, str, str, str], int] = {}
    for (month, region, offense, division), n in cells.items():
        off = offense if offense in keep else OTHER_OFFENSE
        folded[(month, region, off, division)] = folded.get((month, region, off, division), 0) + n
    return [
        MonthlyRecord(month=m, region=r, offense=o, offense_division=d, count=c)
        for (m, r, o, d), c in folded.items()
    ]


def fetch_live(today: date | None = None) -> tuple[list[MonthlyRecord], str]:
    today = today or date.today()
    prev_year_start = date(today.year - 1, 1, 1)
    prev_year_end = date(today.year - 1, 12, 31)
    ytd_start = date(today.year, 1, 1)
    with httpx.Client(timeout=90.0, headers={"User-Agent": "signalkit"}) as client:
        raw = _soql_fetch(HISTORIC_URL, prev_year_start, prev_year_end, client)
        raw += _soql_fetch(YTD_URL, ytd_start, today, client)
    records = _aggregate(raw)
    if not records:
        raise DataUnavailable("Live API returned no usable rows.")
    label = (
        "NYC Open Data — NYPD Complaint Data Historic (qgea-i56i) + Current YTD "
        f"(5uac-w243), live as of {today.isoformat()}"
    )
    return records, label


def load_snapshot() -> tuple[list[MonthlyRecord], str]:
    with SNAPSHOT_PATH.open() as f:
        payload = json.load(f)
    records = [MonthlyRecord(**row) for row in payload["records"]]
    meta = payload["meta"]
    label = f"{meta['source']} — bundled snapshot fetched {meta['fetched_at']}"
    return records, label


def _refresh_live_cache() -> None:
    global _live_cache, _refresh_in_flight
    try:
        records, label = fetch_live()
        _live_cache = (time.monotonic(), records, label)
    except Exception:
        pass
    finally:
        with _refresh_lock:
            _refresh_in_flight = False


def get_records(offline: bool | None = None) -> tuple[list[MonthlyRecord], str]:
    """Return (records, source_label) without ever blocking on the network.

    Same stale-while-revalidate contract as the SA layer: snapshot (or stale
    cache) served instantly while a background thread refreshes live data.
    """
    global _refresh_in_flight
    if offline is None:
        offline = os.environ.get("SIGNAL_OFFLINE", "").strip() in {"1", "true", "yes"}
    if offline:
        return load_snapshot()

    if _live_cache is not None and time.monotonic() - _live_cache[0] < _CACHE_TTL_SECONDS:
        return _live_cache[1], _live_cache[2]

    should_start = False
    with _refresh_lock:
        if not _refresh_in_flight:
            _refresh_in_flight = True
            should_start = True
    if should_start:
        threading.Thread(target=_refresh_live_cache, daemon=True).start()

    if _live_cache is not None:
        return _live_cache[1], _live_cache[2] + " (stale, live refresh in progress)"
    try:
        records, label = load_snapshot()
    except Exception as snapshot_error:  # pragma: no cover - catastrophic path
        raise DataUnavailable("Live data not yet cached and snapshot missing.") from snapshot_error
    return records, label + " (live refresh in progress)"
