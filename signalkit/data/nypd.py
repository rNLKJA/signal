"""
signalkit/data/nypd.py
======================
Monthly complaint aggregates from NYC Open Data (NYPD complaint datasets).

Two sources are unioned to build a rolling window that spans the previous
full year plus the current year to date:

  - NYPD Complaint Data Historic  (qgea-i56i) — closed prior years
  - NYPD Complaint Data Current YTD (5uac-w243) — current year

Queries are server-side SoQL aggregates (count by month x borough x offence
x law category), so no raw incident rows — and no PII — ever leave the API.
Both datasets are keyless for modest request volumes.

A real-data snapshot is bundled at ``sample/nypd_monthly.json`` so the
product works offline (tests, CI, demos). Set ``SIGNAL_OFFLINE=1`` to force
the snapshot; otherwise live is attempted first with snapshot fallback.

Known data quality issue: the source contains typo'd complaint dates as far
back as year 1012, so every query bounds ``cmplnt_fr_dt`` explicitly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

HISTORIC_URL = "https://data.cityofnewyork.us/resource/qgea-i56i.json"
YTD_URL = "https://data.cityofnewyork.us/resource/5uac-w243.json"
SNAPSHOT_PATH = Path(__file__).parent / "sample" / "nypd_monthly.json"

_CACHE_TTL_SECONDS = 3600
_live_cache: tuple[float, list["MonthlyRecord"], str] | None = None
_refresh_lock = threading.Lock()
_refresh_in_flight = False


class DataUnavailable(Exception):
    """Raised when neither the live API nor the snapshot can provide data."""


class MonthlyRecord(BaseModel):
    """One aggregate row: complaint count for a month/borough/offence cell."""

    month: str = Field(description="Calendar month, YYYY-MM")
    borough: str
    offense: str = Field(description="NYPD offence description, e.g. 'BURGLARY'")
    law_category: str = Field(description="FELONY, MISDEMEANOR or VIOLATION")
    count: int = Field(ge=0)


def _soql_fetch(base_url: str, start: date, end: date) -> list[dict]:
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
    # Cold grouped queries on the historic dataset can take Socrata >30s;
    # callers never block on this (see get_records), so a generous timeout
    # is fine here.
    response = httpx.get(base_url, params=params, timeout=90.0)
    response.raise_for_status()
    return response.json()


def _parse_rows(raw: list[dict]) -> list[MonthlyRecord]:
    records = []
    for row in raw:
        # Rows with null grouping keys (unrecorded borough etc.) are dropped.
        if not all(row.get(k) for k in ("month", "boro_nm", "ofns_desc", "law_cat_cd", "n")):
            continue
        records.append(
            MonthlyRecord(
                month=row["month"][:7],
                borough=row["boro_nm"],
                offense=row["ofns_desc"],
                law_category=row["law_cat_cd"],
                count=int(row["n"]),
            )
        )
    return records


def fetch_live(today: date | None = None) -> tuple[list[MonthlyRecord], str]:
    """Fetch the rolling window live: previous full year + current YTD."""
    today = today or date.today()
    prev_year_start = date(today.year - 1, 1, 1)
    prev_year_end = date(today.year - 1, 12, 31)
    ytd_start = date(today.year, 1, 1)

    raw = _soql_fetch(HISTORIC_URL, prev_year_start, prev_year_end)
    raw += _soql_fetch(YTD_URL, ytd_start, today)
    records = _parse_rows(raw)
    if not records:
        raise DataUnavailable("Live API returned no usable rows.")
    label = (
        "NYC Open Data — NYPD Complaint Data Historic (qgea-i56i) + "
        f"Current YTD (5uac-w243), live as of {today.isoformat()}"
    )
    return records, label


def load_snapshot() -> tuple[list[MonthlyRecord], str]:
    """Load the bundled real-data snapshot (works offline)."""
    with SNAPSHOT_PATH.open() as f:
        payload = json.load(f)
    records = [MonthlyRecord(**row) for row in payload["records"]]
    meta = payload["meta"]
    label = f"{meta['source']} — bundled snapshot fetched {meta['fetched_at']}"
    return records, label


def _refresh_live_cache() -> None:
    """Populate the live cache in the background. Failures leave it untouched."""
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

    Stale-while-revalidate: a fresh live cache is served directly; otherwise
    the caller gets the snapshot (or stale cache) immediately while a
    background thread refreshes live data for subsequent requests. Cold
    Socrata aggregate queries can take over a minute, which must never hang
    an /ask request.

    ``offline=True`` (or env SIGNAL_OFFLINE=1) skips the network entirely.
    The source label is recorded in every governance log entry, so the audit
    trail always states exactly which data informed an answer.
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
