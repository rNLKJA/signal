"""
signalkit/data/sa_crime.py
==========================
Monthly recorded-offence aggregates from South Australia Police open data.

Source: SA Police "Crime statistics" on data.gov.au — published per financial
year as a CKAN datastore. Two financial-year resources are unioned to build a
rolling window long enough for year-on-year comparison:

  - 2024-25       (resource 4a553fc6-71fe-4dac-8096-c29abc269c76)
  - 2025-26 YTD   (resource cfce21c4-9712-454f-b32d-0f7c989accda)

Each source row is already an aggregate (an offence count for a suburb, date
and ANZSOC offence on one day) — no incident-level identifiers, no PII. Signal
aggregates further to month x region x offence x division, so what leaves the
API is doubly aggregated.

Taxonomy harmonisation
----------------------
SA Police revised their Offence Level 2 labels for 2025-26 (e.g. "THEFT AND
RELATED OFFENCES" became "THEFT"; "ACTS INTENDED TO CAUSE INJURY" became
"ASSAULT"). A trend that spans the change would otherwise fragment, so both
vocabularies are mapped onto one stable scheme (``OFFENSE_MAP`` /
``DIVISION_MAP``) applied identically here and in the snapshot builder. The
mapping is documented and conservative; unmapped future labels fall back to a
title-cased form rather than being dropped.

CKAN's SQL endpoint blocks ``CAST`` (the count column is text), so aggregation
is done client-side after a plain paginated ``datastore_search``. A real-data
snapshot is bundled at ``sample/sa_crime_monthly.json`` so the product works
offline (tests, CI, demos). Set ``SIGNAL_OFFLINE=1`` to force the snapshot;
otherwise live is attempted first with snapshot fallback.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

CKAN_BASE = "https://data.gov.au/data/api/3/action/datastore_search"
PREV_FY_RESOURCE = "4a553fc6-71fe-4dac-8096-c29abc269c76"   # 2024-25
CURRENT_FY_RESOURCE = "cfce21c4-9712-454f-b32d-0f7c989accda"  # 2025-26 YTD
SNAPSHOT_PATH = Path(__file__).parent / "sample" / "sa_crime_monthly.json"

# How many suburbs to surface as distinct regions; the long tail of small
# suburbs is folded into one bucket so the region axis stays comparable.
TOP_REGIONS = 15
OTHER_REGION = "OTHER SA AREAS"

_CACHE_TTL_SECONDS = 3600
_live_cache: tuple[float, list["MonthlyRecord"], str] | None = None
_refresh_lock = threading.Lock()
_refresh_in_flight = False

# ANZSOC Level-1 division (stable across the taxonomy revision).
DIVISION_MAP = {
    "OFFENCES AGAINST THE PERSON": "Offences against the person",
    "OFFENCES AGAINST PROPERTY": "Offences against property",
}

# Offence Level-2 harmonisation: both the pre- and post-2025 SA Police labels
# map onto one stable category so a series can span the revision.
OFFENSE_MAP = {
    # --- against the person ---
    "HOMICIDE AND RELATED OFFENCES": "Homicide and related",
    "HOMICIDE": "Homicide and related",
    "ACTS INTENDED TO CAUSE INJURY": "Assault and injury",
    "ASSAULT": "Assault and injury",
    "HARM OR ENDANGER PERSONS": "Assault and injury",
    "SEXUAL ASSAULT AND RELATED OFFENCES": "Sexual offences",
    "SEXUAL OFFENCES": "Sexual offences",
    "ROBBERY AND RELATED OFFENCES": "Robbery and extortion",
    "ROBBERY, BLACKMAIL, AND EXTORTION": "Robbery and extortion",
    "OTHER OFFENCES AGAINST THE PERSON": "Other against the person",
    "OTHER OFFENCES AGAINST THE PERSON NEC": "Other against the person",
    # --- against property ---
    "SERIOUS CRIMINAL TRESPASS": "Serious criminal trespass",
    "THEFT AND RELATED OFFENCES": "Theft",
    "THEFT": "Theft",
    "FRAUD DECEPTION AND RELATED OFFENCES": "Fraud and deception",
    "FRAUD AND RELATED OFFENCES": "Fraud and deception",
    "PROPERTY DAMAGE AND ENVIRONMENTAL": "Property damage",
    "PROPERTY DAMAGE": "Property damage",
    "OTHER OFFENCES AGAINST PROPERTY": "Other against property",
}


class DataUnavailable(Exception):
    """Raised when neither the live API nor the snapshot can provide data."""


class MonthlyRecord(BaseModel):
    """One aggregate row: offence count for a month/region/offence cell."""

    month: str = Field(description="Calendar month, YYYY-MM")
    region: str = Field(description="SA suburb, or 'OTHER SA AREAS' for the tail")
    offense: str = Field(description="Harmonised ANZSOC offence, e.g. 'Theft'")
    offense_division: str = Field(
        description="ANZSOC division: against the person / against property"
    )
    count: int = Field(ge=0)


def _norm(label: str) -> str:
    """Collapse whitespace and uppercase a raw label for map lookup."""
    return " ".join((label or "").split()).upper()


def _map_offense(raw_l2: str) -> str:
    key = _norm(raw_l2)
    if key in OFFENSE_MAP:
        return OFFENSE_MAP[key]
    # Unmapped future label: keep it rather than drop, title-cased for display.
    return key.title()


def _map_division(raw_l1: str) -> str:
    return DIVISION_MAP.get(_norm(raw_l1), _norm(raw_l1).title())


def _fetch_resource(resource_id: str, client: httpx.Client) -> list[dict]:
    """Page through one CKAN datastore resource, returning the trimmed rows."""
    fields = (
        "Reported Date,Suburb - Incident,Offence Level 1 Description,"
        "Offence Level 2 Description,Offence count"
    )
    rows: list[dict] = []
    offset = 0
    page = 20000
    while True:
        resp = client.get(
            CKAN_BASE,
            params={
                "resource_id": resource_id,
                "limit": page,
                "offset": offset,
                "fields": fields,
            },
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        batch = result["records"]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if offset >= result.get("total", offset):
            break
    return rows


def _aggregate(rows: list[dict]) -> list[MonthlyRecord]:
    """Aggregate raw rows to month x region x offence x division, with the
    long tail of small suburbs folded into one bucket."""
    cells: dict[tuple[str, str, str, str], int] = {}
    region_totals: dict[str, int] = {}
    for row in rows:
        reported = row.get("Reported Date") or ""
        if len(reported) != 10:  # expect DD/MM/YYYY
            continue
        month = f"{reported[6:10]}-{reported[3:5]}"
        suburb = (row.get("Suburb - Incident") or "").strip().upper()
        division = _map_division(row.get("Offence Level 1 Description", ""))
        offense = _map_offense(row.get("Offence Level 2 Description", ""))
        try:
            n = int(row.get("Offence count") or 0)
        except ValueError:
            continue
        if not suburb or n <= 0:
            continue
        cells[(month, suburb, offense, division)] = (
            cells.get((month, suburb, offense, division), 0) + n
        )
        region_totals[suburb] = region_totals.get(suburb, 0) + n

    keep = {
        s for s, _ in sorted(region_totals.items(), key=lambda kv: -kv[1])[:TOP_REGIONS]
    }
    folded: dict[tuple[str, str, str, str], int] = {}
    for (month, suburb, offense, division), n in cells.items():
        region = suburb if suburb in keep else OTHER_REGION
        folded[(month, region, offense, division)] = (
            folded.get((month, region, offense, division), 0) + n
        )
    return [
        MonthlyRecord(month=m, region=r, offense=o, offense_division=d, count=c)
        for (m, r, o, d), c in folded.items()
    ]


def fetch_live() -> tuple[list[MonthlyRecord], str]:
    """Fetch and aggregate the rolling window live from SA Police open data."""
    with httpx.Client(timeout=90.0, headers={"User-Agent": "signalkit"}) as client:
        rows = _fetch_resource(PREV_FY_RESOURCE, client)
        rows += _fetch_resource(CURRENT_FY_RESOURCE, client)
    records = _aggregate(rows)
    if not records:
        raise DataUnavailable("Live API returned no usable rows.")
    months = sorted({r.month for r in records})
    label = (
        "SA Police Crime statistics (data.gov.au) — FY2024-25 + FY2025-26 YTD, "
        f"live as of {months[-1]}"
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
    background thread refreshes live data for subsequent requests. Cold CKAN
    pulls aggregate ~165k rows, which must never hang an /ask request.

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
