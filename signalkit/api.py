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

Run locally:
    uvicorn signalkit.api:app --reload

The /decisions endpoint is deliberately public in this product: the point of
Signal is that every AI-assisted answer is traceable, so the audit trail is
part of the user-facing surface, not a hidden ops file.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse

import signalkit
from signalkit.analyst.core import (
    Analyst,
    AnalystQuery,
    CompareQuery,
    NoDataError,
    ReviewRequest,
)
from signalkit.data.sa_crime import DataUnavailable
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
        return {
            "service": "signal",
            "version": signalkit.__version__,
            "docs": "/docs",
            "endpoints": ["/health", "/ask (POST)", "/decisions"],
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

    return app


app = create_app()
