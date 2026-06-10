"""
signalkit/api.py
================
The Signal HTTP API.

Endpoints:
  GET  /           — interactive dashboard (single self-contained page)
  GET  /api        — service index (JSON)
  GET  /health     — liveness + version
  POST /ask        — ask the analyst; response includes the governance decision_id
  GET  /decisions  — read back the audit trail (the governance log, live)

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

    return app


app = create_app()
