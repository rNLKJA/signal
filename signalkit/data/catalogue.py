"""
signalkit/data/catalogue.py
===========================
A thin, governed client over the data.sa.gov.au CKAN catalogue.

Signal's flagship analyst is the SA Police crime data, but the same portal
(data.sa.gov.au) publishes ~1,900 datasets. This module lets the product
search the catalogue, show a dataset's metadata, and preview any resource that
has a live datastore — turning Signal into a governed explorer over the whole
portal, not just one dataset.

Only metadata and aggregate-shaped previews leave the portal here; previews are
row-capped. The analyst layer logs a governance entry for every preview, so
even ad-hoc data lookups are traceable.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

# Australian open-data portals, all running CKAN with the same action API.
# data.sa is the flagship; NSW and VIC give the explorer national reach.
PORTALS = {
    "sa": "https://data.sa.gov.au/data",
    "nsw": "https://data.nsw.gov.au/data",
    "vic": "https://discover.data.vic.gov.au",
}
DEFAULT_PORTAL = "sa"
_TIMEOUT = 30.0


def _portal_base(portal: str) -> str:
    base = PORTALS.get(portal)
    if base is None:
        raise ValueError(f"Unknown portal '{portal}'. Choose one of: {', '.join(PORTALS)}.")
    return base


class ResourceRef(BaseModel):
    id: str
    name: str
    format: str
    datastore_active: bool


class DatasetSummary(BaseModel):
    name: str
    title: str
    organisation: str
    notes: str
    num_resources: int
    datastore_resources: list[ResourceRef]


class DatasetDetail(BaseModel):
    name: str
    title: str
    organisation: str
    notes: str
    url: str
    resources: list[ResourceRef]


class ResourcePreview(BaseModel):
    resource_id: str
    fields: list[dict]
    records: list[dict]
    total: int
    truncated: bool


def _get(portal: str, action: str, params: dict) -> dict:
    base = _portal_base(portal)
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "signalkit"}) as client:
        resp = client.get(f"{base}/api/3/action/{action}", params=params)
        resp.raise_for_status()
        payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"CKAN {action} failed")
    return payload["result"]


def _resources(pkg: dict) -> list[ResourceRef]:
    return [
        ResourceRef(
            id=r.get("id", ""),
            name=r.get("name") or "(unnamed)",
            format=(r.get("format") or "").upper(),
            datastore_active=bool(r.get("datastore_active")),
        )
        for r in pkg.get("resources", [])
    ]


def search_datasets(query: str = "", limit: int = 20, portal: str = DEFAULT_PORTAL) -> list[DatasetSummary]:
    """Search a portal's catalogue. Blank query returns recent datasets."""
    result = _get(portal, "package_search", {"q": query, "rows": max(1, min(limit, 50))})
    summaries = []
    for pkg in result.get("results", []):
        resources = _resources(pkg)
        notes = (pkg.get("notes") or "").strip().replace("\r", " ").replace("\n", " ")
        summaries.append(
            DatasetSummary(
                name=pkg.get("name", ""),
                title=pkg.get("title") or pkg.get("name", ""),
                organisation=(pkg.get("organization") or {}).get("title", ""),
                notes=notes[:280],
                num_resources=len(resources),
                datastore_resources=[r for r in resources if r.datastore_active],
            )
        )
    return summaries


def dataset_info(name_or_id: str, portal: str = DEFAULT_PORTAL) -> DatasetDetail | None:
    """Full metadata for one dataset, or None if it does not exist."""
    try:
        pkg = _get(portal, "package_show", {"id": name_or_id})
    except httpx.HTTPStatusError:
        return None
    notes = (pkg.get("notes") or "").strip().replace("\r", " ").replace("\n", " ")
    return DatasetDetail(
        name=pkg.get("name", ""),
        title=pkg.get("title") or pkg.get("name", ""),
        organisation=(pkg.get("organization") or {}).get("title", ""),
        notes=notes[:800],
        url=f"{_portal_base(portal)}/dataset/{pkg.get('name', '')}",
        resources=_resources(pkg),
    )


def fetch_rows(
    resource_id: str, max_rows: int = 5000, portal: str = DEFAULT_PORTAL
) -> tuple[list[dict], list[dict]]:
    """Fetch up to max_rows from a datastore resource for analysis.

    Returns (fields, rows) with the internal _id stripped. Paged in 1,000-row
    requests; capped so an arbitrary dataset can never pull an unbounded set.
    """
    rows: list[dict] = []
    fields: list[dict] = []
    offset = 0
    while len(rows) < max_rows:
        page = min(1000, max_rows - len(rows))
        result = _get(
            portal,
            "datastore_search",
            {"resource_id": resource_id, "limit": page, "offset": offset},
        )
        if not fields:
            fields = [f for f in result.get("fields", []) if f.get("id") != "_id"]
        batch = [{k: v for k, v in r.items() if k != "_id"} for r in result.get("records", [])]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if offset >= result.get("total", offset):
            break
    return fields, rows


def preview_resource(resource_id: str, limit: int = 20, portal: str = DEFAULT_PORTAL) -> ResourcePreview:
    """Preview the first rows of a datastore-backed resource."""
    capped = max(1, min(limit, 100))
    result = _get(portal, "datastore_search", {"resource_id": resource_id, "limit": capped})
    fields = [f for f in result.get("fields", []) if f.get("id") != "_id"]
    records = [{k: v for k, v in row.items() if k != "_id"} for row in result.get("records", [])]
    total = result.get("total", len(records))
    return ResourcePreview(
        resource_id=resource_id,
        fields=fields,
        records=records,
        total=total,
        truncated=total > len(records),
    )
