# Signal

[![CI](https://github.com/rNLKJA/signal/actions/workflows/ci.yml/badge.svg)](https://github.com/rNLKJA/signal/actions/workflows/ci.yml)

An interactive product over US public-safety data, with an analyst layer and a governance log that records every AI-assisted answer in a form built to satisfy the APS Mandatory AI Requirements and the EU AI Act.

**Live demo: https://rnlkja--signal-api-api.modal.run** — ask it something and watch the audit trail fill in.

Most data products show you a chart. Signal also shows you how the answer was reached: which model ran, what data informed it, what decision followed, and whether a human signed off. Every API response carries a `decision_id`, and the audit trail is itself a public endpoint — traceability is part of the product surface, not an ops file.

```bash
curl -X POST https://rnlkja--signal-api-api.modal.run/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "How is burglary trending in Brooklyn?", "offense": "burglary", "borough": "brooklyn"}'
```

```json
{
  "narrative": "Between 2025-04 and 2026-03, NYPD recorded 3,087 complaints for offence matching 'burglary' in BROOKLYN. The trend over the window is falling. The latest month is down 7.0% on the month before. Year on year, the latest month is down 15.6%.",
  "stats": { "trend_direction": "falling", "yoy_change_pct": -15.6, "...": "..." },
  "decision_id": "d-328069d6",
  "data_source": "NYC Open Data — NYPD Complaint Data Historic (qgea-i56i) + Current YTD (5uac-w243)",
  "model_used": "signal-stats-v1 (deterministic)",
  "human_review_required": false
}
```

That `decision_id` resolves at `GET /decisions` to the full audit entry: model provenance, data sources, decision category, confidence, risk tier, and the human-review flag.

## The problem

From 15 June 2026, Australian Public Service agencies must document their use of AI against four mandatory requirements: what AI system was used, what decision was made, what data informed the decision, and whether a human reviewed it. The EU AI Act (2024/1689) adds risk-tier classification and traceability obligations, and the December 2026 amendment to the Privacy Act 1988 (Cth) adds a disclosure obligation for automated decision-making.

These rules are easy to nod along to and hard to actually meet, because compliance has to be captured at the moment a decision is made, not reconstructed afterwards. Signal wires the record into the request path: the analyst cannot answer without logging.

## Governance design

The core is [`signalkit/governance/decision_log.py`](signalkit/governance/decision_log.py): a Pydantic v2 schema and an append-only JSONL logger for AI-assisted decisions. Every field maps to a specific obligation, tagged in the source as `[APS]`, `[EU]`, or `[Privacy]`.

How it maps to the four APS mandatory requirements:

| APS requirement | Field in `DecisionEntry` |
|---|---|
| What AI system was used | `model_name`, `model_version`, `model_provider` |
| What decision was made | `decision_made`, `decision_category` |
| What data informed it | `data_sources`, `input_summary`, `model_output_summary` |
| Whether a human reviewed it | `human_review_required`, `human_reviewer`, `override_applied`, `override_reason` |

For the EU AI Act, `risk_category` records the tier (`unacceptable`, `high`, `limited`, `minimal`) so high-risk uses are flagged for the logging and oversight that Annex III requires. `confidence_score` and the UTC `timestamp` support the traceability obligations. A validator enforces that any human override carries a written reason, so the record cannot claim an override without explaining it.

The analyst layer ([`signalkit/analyst/core.py`](signalkit/analyst/core.py)) applies three governance rules of its own:

- **Aggregates only.** The statistics are computed server-side by the data source (SoQL group-bys); no raw incident rows, and no PII, ever enter the system.
- **The LLM is optional, sandboxed, and provider-agnostic.** Without an API key, narratives come from a deterministic template (`signal-stats-v1`). With `SIGNAL_LLM_API_KEY` set, any OpenAI-compatible model (DeepSeek by default) phrases the narrative — but it receives only the computed statistics, never the underlying data, and the audit entry records which provider and model actually produced the words. Swapping the model never weakens the trail; see `.env.example`.
- **Anomalies trigger human review.** Months with a z-score at or beyond 2 set `human_review_required=True` in both the response and the log. A spike should be checked by a person before anyone acts on it.

The log is plain JSONL: one decision per line, UTF-8, no special tooling needed to read or grep it, and `to_dicts()` hands it straight to pandas or DuckDB for analysis.

## The data

Live monthly complaint aggregates from NYC Open Data — NYPD Complaint Data [Historic](https://data.cityofnewyork.us/Public-Safety/NYPD-Complaint-Data-Historic/qgea-i56i) plus [Current YTD](https://data.cityofnewyork.us/Public-Safety/NYPD-Complaint-Data-Current-Year-To-Date-/5uac-w243), unioned into a rolling window of the previous full year plus the current year to date.

Cold aggregate queries on the historic dataset can take Socrata over a minute, so the data layer is stale-while-revalidate: requests are answered instantly from a bundled real-data snapshot (or the last live cache) while a background thread refreshes live data for subsequent calls. The `data_source` field in every response and audit entry states exactly which was used. The source data also contains complaint dates typo'd as far back as year 1012, so every query bounds the date range explicitly.

## API

| Endpoint | What it does |
|---|---|
| `GET /` | Interactive dashboard — ask questions, see the chart, watch the audit trail fill in. One self-contained page, no frameworks, no CDN. |
| `POST /ask` | Ask the analyst. Filters: `offense`, `borough` (substring), `months` (2–24). Returns narrative, stats (trend, anomalies, top offences, law-category split), and the `decision_id`. Rate-limited per client (default 20/min, `429` + `Retry-After` beyond that). |
| `POST /compare` | One offence scope across all five boroughs: aligned monthly series, totals, YoY, trend per borough. Audit-logged and rate-limited like `/ask`. |
| `GET /decisions` | The governance log, live. Most recent entries, `limit` up to 100. |
| `GET /decisions/{decision_id}` | Resolve any `decision_id` from an answer to its full audit entry. |
| `GET /governance/summary` | The governance posture, quantified: review rate, risk tiers, model breakdown. |
| `GET /health` | Liveness and version. |
| `GET /docs` | OpenAPI docs. |

## Status and roadmap

- [x] Governance decision log (APS / EU AI Act / Privacy Act aligned)
- [x] Data layer over NYC Open Data with offline snapshot and stale-while-revalidate
- [x] Analyst layer: trend stats, anomaly detection, every answer audit-logged
- [x] FastAPI service with the audit trail as a public endpoint
- [x] Tests and CI
- [x] Interactive dashboard at `/` (vanilla, self-contained, dark-mode aware)
- [x] Decision deep-links and governance analytics (`/decisions/{id}`, `/governance/summary`)
- [x] LLM path under test (mocked client; aggregates-only prompt enforced)
- [x] Deployed to Modal — live at https://rnlkja--signal-api-api.modal.run
- [x] Decision log persisted to a Modal Volume — the audit trail survives cold starts
- [x] LLM narrative live in deployment — DeepSeek via the provider-agnostic layer, attributed per-decision in the audit log
- [x] Visual suite: bar/line toggle, borough comparison (multi-series), top offences, law-category split — all hand-rolled SVG
- [x] Perf: LLM narrative cache (identical queries never re-spend tokens), gzip, dashboard cache headers

## Reproduce

Requires Python 3.10 or later.

```bash
git clone https://github.com/rNLKJA/signal.git
cd signal
pip install -e ".[dev]"

pytest                                  # all offline, no network needed
uvicorn signalkit.api:app --reload     # then open http://127.0.0.1:8000/
```

Ask it something:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"offense": "grand larceny", "borough": "manhattan", "months": 12}'
```

Then read the audit trail at `http://127.0.0.1:8000/decisions`.

Or run it in Docker (image not yet CI-verified):

```bash
docker build -t signal . && docker run -p 8000:8000 signal
```

> Why `signalkit` and not `signal`? A top-level Python package named `signal` shadows the standard-library `signal` module and breaks anything that imports it (asyncio, uvicorn). The repo keeps the product name; the package keeps out of the stdlib's way.

## Licence

MIT. See [LICENSE](LICENSE).
