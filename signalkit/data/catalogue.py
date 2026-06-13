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

# Open-data portals. SA/NSW/VIC run CKAN; NYC runs Socrata. PORTAL_TYPE selects
# the adapter, and PORTALS holds the public base URL (for dataset links/labels).
PORTALS = {
    "sa": "https://data.sa.gov.au/data",
    "nsw": "https://data.nsw.gov.au/data",
    "vic": "https://discover.data.vic.gov.au",
    "nyc": "https://data.cityofnewyork.us",
}
PORTAL_TYPE = {"sa": "ckan", "nsw": "ckan", "vic": "ckan", "nyc": "socrata"}
SOCRATA_DOMAIN = {"nyc": "data.cityofnewyork.us"}
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


# --- Socrata adapter (NYC) ---

def _socrata_rows(domain: str, rid: str, params: dict) -> list[dict]:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "signalkit"}) as client:
        resp = client.get(f"https://{domain}/resource/{rid}.json", params=params)
        resp.raise_for_status()
        return resp.json()


def _socrata_search(query: str, limit: int, portal: str) -> list[DatasetSummary]:
    domain = SOCRATA_DOMAIN[portal]
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "signalkit"}) as client:
        resp = client.get(
            "https://api.us.socrata.com/api/catalog/v1",
            params={"domains": domain, "q": query, "limit": max(1, min(limit, 50)), "only": "dataset"},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    out = []
    for r in results:
        res = r.get("resource", {})
        rid = res.get("id", "")
        name = res.get("name") or rid
        notes = (res.get("description") or "").strip().replace("\r", " ").replace("\n", " ")
        out.append(DatasetSummary(
            name=rid, title=name,
            organisation=(r.get("classification") or {}).get("domain_category") or domain,
            notes=notes[:280], num_resources=1,
            datastore_resources=[ResourceRef(id=rid, name=name, format="Socrata", datastore_active=True)],
        ))
    return out


def _socrata_info(rid: str, portal: str) -> DatasetDetail | None:
    domain = SOCRATA_DOMAIN[portal]
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "signalkit"}) as client:
            resp = client.get(f"https://{domain}/api/views/{rid}.json")
            resp.raise_for_status()
            v = resp.json()
    except httpx.HTTPStatusError:
        return None
    notes = (v.get("description") or "").strip().replace("\r", " ").replace("\n", " ")
    name = v.get("name") or rid
    return DatasetDetail(
        name=rid, title=name, organisation=v.get("category") or domain, notes=notes[:800],
        url=f"https://{domain}/d/{rid}",
        resources=[ResourceRef(id=rid, name=name, format="Socrata", datastore_active=True)],
    )


def _socrata_preview(rid: str, limit: int, portal: str) -> ResourcePreview:
    domain = SOCRATA_DOMAIN[portal]
    rows = _socrata_rows(domain, rid, {"$limit": max(1, min(limit, 100))})
    fields = [{"id": k, "type": "text"} for k in (rows[0].keys() if rows else [])]
    try:
        cnt = _socrata_rows(domain, rid, {"$select": "count(*)"})
        total = int(cnt[0].get("count") or len(rows)) if cnt else len(rows)
    except Exception:
        total = len(rows)
    return ResourcePreview(
        resource_id=rid, fields=fields, records=rows, total=total, truncated=total > len(rows)
    )


def _socrata_fetch_rows(rid: str, max_rows: int, portal: str) -> tuple[list[dict], list[dict]]:
    domain = SOCRATA_DOMAIN[portal]
    rows: list[dict] = []
    offset = 0
    while len(rows) < max_rows:
        page = min(1000, max_rows - len(rows))
        batch = _socrata_rows(domain, rid, {"$limit": page, "$offset": offset})
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    fields = [{"id": k, "type": "text"} for k in (rows[0].keys() if rows else [])]
    return fields, rows


def search_datasets(query: str = "", limit: int = 20, portal: str = DEFAULT_PORTAL) -> list[DatasetSummary]:
    """Search a portal's catalogue. Blank query returns recent datasets."""
    if PORTAL_TYPE.get(portal) == "socrata":
        return _socrata_search(query, limit, portal)
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
    if PORTAL_TYPE.get(portal) == "socrata":
        return _socrata_info(name_or_id, portal)
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
    if PORTAL_TYPE.get(portal) == "socrata":
        return _socrata_fetch_rows(resource_id, max_rows, portal)
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
    if PORTAL_TYPE.get(portal) == "socrata":
        return _socrata_preview(resource_id, limit, portal)
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
