"""
signalkit/api.py
================
The Signal HTTP API.

Endpoints:
  GET  /                       — interactive dashboard (single self-contained page)
  GET  /api                    — service index (JSON)
  GET  /health                 — liveness + version
  POST /ask                    — ask the analyst; response carries the decision_id
  GET  /decisions              — read back the audit trail (the governance log, live)
  GET  /decisions/{decision_id} — resolve one decision_id to its full audit entry
  GET  /governance/summary     — aggregate governance posture (review rate, risk tiers)

Run locally:
    uvicorn signalkit.api:app --reload

The /decisions endpoint is deliberately public in this product: the point of
Signal is that every AI-assisted answer is traceable, so the audit trail is
part of the user-facing surface, not a hidden ops file.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

import signalkit
from signalkit.analyst.core import Analyst, AnalystQuery, NoDataError
from signalkit.data.nypd import DataUnavailable

DASHBOARD_PATH = Path(__file__).parent / "static" / "index.html"


def create_app(analyst: Analyst | None = None) -> FastAPI:
    app = FastAPI(
        title="Signal",
        version=signalkit.__version__,
        description=(
            "Interactive US public-safety data product with a governed analyst layer. "
            "Every answer is logged to an APS / EU-AI-Act aligned decision log and "
            "returns its decision_id."
        ),
    )
    app.state.analyst = analyst or Analyst()

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
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

    @app.post("/ask")
    def ask(query: AnalystQuery) -> dict:
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

    @app.get("/governance/summary")
    def governance_summary() -> dict:
        return app.state.analyst.governance_summary().model_dump(mode="json")

    return app


app = create_app()
