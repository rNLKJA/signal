"""
signalkit/api.py
================
The Signal HTTP API.

Endpoints:
  GET  /                       — interactive dashboard (single self-contained page)
  GET  /api                    — service index (JSON)
  GET  /health                 — liveness + version
  POST /ask                    — ask the analyst; response carries the decision_id
  POST /compare                — one offence scope across SA regions
  GET  /decisions              — read back the audit trail (the governance log, live)
  GET  /decisions/{decision_id} — resolve one decision_id to its full audit entry
  POST /decisions/{decision_id}/review — record a human review/override of a decision
  GET  /governance/summary     — aggregate governance posture (review rate, risk tiers)
  GET  /datasets               — search the data.sa.gov.au catalogue
  GET  /datasets/{name}        — metadata for one data.sa dataset
  GET  /resources/{id}/preview — preview a datastore resource (audit-logged)

Run locally:
    uvicorn signalkit.api:app --reload

The /decisions endpoint is deliberately public in this product: the point of
Signal is that every AI-assisted answer is traceable, so the audit trail is
part of the user-facing surface, not a hidden ops file.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse

import signalkit
from signalkit.data import catalogue
from signalkit.analyst.core import (
    Analyst,
    AnalystQuery,
    CompareQuery,
    NoDataError,
    ReviewRequest,
)
from signalkit.data.sa_crime import DataUnavailable, snapshot_meta
from signalkit.ratelimit import RateLimiter

DASHBOARD_PATH = Path(__file__).parent / "static" / "index.html"


def _client_key(request: Request) -> str:
    """Identify the caller. Behind Modal's proxy the real address arrives
    in X-Forwarded-For; fall back to the socket peer locally."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(analyst: Analyst | None = None, rate_limiter: RateLimiter | None = None) -> FastAPI:
    app = FastAPI(
        title="Signal",
        version=signalkit.__version__,
        description=(
            "Interactive South Australian crime-data product with a governed analyst "
            "layer. Every answer is logged to an APS / EU-AI-Act aligned decision log "
            "and returns its decision_id."
        ),
    )
    app.state.analyst = analyst or Analyst()
    app.state.rate_limiter = rate_limiter or RateLimiter()
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    def dashboard(response: Response) -> str:
        # The page only changes on deploy; let browsers keep it for 5 minutes.
        response.headers["Cache-Control"] = "public, max-age=300"
        return DASHBOARD_PATH.read_text(encoding="utf-8")

    @app.get("/api")
    def index() -> dict:
        try:
            meta = snapshot_meta()
            data = {"source": meta.get("source"), "window": meta.get("window"),
                    "fetched_at": meta.get("fetched_at")}
        except Exception:
            data = None
        return {
            "service": "signal",
            "version": signalkit.__version__,
            "docs": "/docs",
            "endpoints": ["/health", "/ask (POST)", "/decisions"],
            "data": data,
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": signalkit.__version__}

    def _enforce_rate_limit(request: Request, response: Response) -> None:
        limiter = app.state.rate_limiter
        key = _client_key(request)
        # Operational visibility (Modal logs): who is the limiter keying on,
        # and which container served this? Diagnoses proxy/scale-out effects.
        print(
            f"rate-limit key={key} remaining={limiter.remaining(key)} "
            f"container={os.environ.get('MODAL_TASK_ID', 'local')}"
        )
        retry_after = limiter.check(key)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit reached. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        response.headers["X-RateLimit-Limit"] = str(limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(limiter.remaining(key))

    @app.post("/ask")
    def ask(query: AnalystQuery, request: Request, response: Response) -> dict:
        _enforce_rate_limit(request, response)
        try:
            answer = app.state.analyst.ask(query)
        except NoDataError as e:
            raise HTTPException(
                status_code=404,
                detail={"message": str(e), "valid_values": e.suggestions},
            ) from e
        except DataUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return answer.model_dump(mode="json")

    @app.post("/compare")
    def compare(query: CompareQuery, request: Request, response: Response) -> dict:
        _enforce_rate_limit(request, response)
        try:
            result = app.state.analyst.compare(query)
        except NoDataError as e:
            raise HTTPException(
                status_code=404,
                detail={"message": str(e), "valid_values": e.suggestions},
            ) from e
        except DataUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return result.model_dump(mode="json")

    @app.get("/decisions")
    def decisions(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
        entries = app.state.analyst.recent_decisions(limit)
        return [e.model_dump(mode="json") for e in entries]

    @app.get("/decisions/{decision_id}")
    def decision_by_id(decision_id: str) -> dict:
        entry = app.state.analyst.get_decision(decision_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"No decision '{decision_id}' in the audit log.",
            )
        return entry.model_dump(mode="json")

    @app.post("/decisions/{decision_id}/review")
    def record_review(decision_id: str, review: ReviewRequest) -> dict:
        entry = app.state.analyst.record_review(decision_id, review)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"No decision '{decision_id}' in the audit log.",
            )
        return entry.model_dump(mode="json")

    @app.get("/governance/summary")
    def governance_summary() -> dict:
        return app.state.analyst.governance_summary().model_dump(mode="json")

    @app.get("/decisions.csv")
    def decisions_csv(limit: int = Query(default=1000, ge=1, le=10000)) -> Response:
        cols = [
            "decision_id", "timestamp", "model_name", "model_provider",
            "decision_category", "decision_made", "input_summary",
            "model_output_summary", "data_sources", "confidence_score",
            "human_review_required", "human_reviewer", "override_applied",
            "override_reason", "reviews_decision_id", "risk_category",
            "legislative_basis", "tags",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for entry in app.state.analyst.recent_decisions(limit):
            row = entry.model_dump(mode="json")
            row["data_sources"] = "; ".join(row.get("data_sources") or [])
            row["tags"] = "; ".join(row.get("tags") or [])
            writer.writerow(row)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=signal-decisions.csv"},
        )

    # --- data.sa.gov.au catalogue explorer ---

    def _check_portal(portal: str) -> None:
        if portal not in catalogue.PORTALS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown portal '{portal}'. Choose: {', '.join(catalogue.PORTALS)}.",
            )

    @app.get("/datasets")
    def datasets(
        q: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=50),
        portal: str = Query(default="sa"),
    ) -> list[dict]:
        _check_portal(portal)
        try:
            return [d.model_dump() for d in catalogue.search_datasets(q, limit, portal)]
        except Exception as e:  # the portal is a live external dependency
            raise HTTPException(status_code=502, detail=f"{portal} catalogue unavailable: {e}") from e

    @app.get("/datasets/{name}")
    def dataset_detail(name: str, portal: str = Query(default="sa")) -> dict:
        _check_portal(portal)
        try:
            info = catalogue.dataset_info(name, portal)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{portal} catalogue unavailable: {e}") from e
        if info is None:
            raise HTTPException(status_code=404, detail=f"No dataset '{name}' on {portal}.")
        return info.model_dump()

    @app.get("/resources/{resource_id}/preview")
    def resource_preview(
        resource_id: str,
        title: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=100),
        portal: str = Query(default="sa"),
    ) -> dict:
        _check_portal(portal)
        try:
            return app.state.analyst.preview_dataset(resource_id, title, limit, portal)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Preview unavailable: {e}") from e

    @app.get("/resources/{resource_id}/analyse")
    def resource_analyse(
        resource_id: str,
        title: str = Query(default=""),
        date_field: str = Query(default=""),
        value_field: str = Query(default=""),
        portal: str = Query(default="sa"),
    ) -> dict:
        _check_portal(portal)
        try:
            return app.state.analyst.analyse_resource(
                resource_id, title, date_field or None, value_field or None, portal=portal
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Analysis unavailable: {e}") from e

    return app


app = create_app()
