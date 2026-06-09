# Signal

An interactive product over US public data, with an LLM analyst layer and a governance log that records every AI-assisted decision in a form built to satisfy the APS Mandatory AI Requirements and the EU AI Act.

Most data products show you a chart. Signal also shows you how the answer was reached: which model ran, what data informed it, what decision followed, and whether a human signed off. That audit trail is the part regulators and agencies now ask for, and it is the part most portfolios skip.

## The problem

From 15 June 2026, Australian Public Service agencies must document their use of AI against four mandatory requirements: what AI system was used, what decision was made, what data informed the decision, and whether a human reviewed it. The EU AI Act (2024/1689) adds risk-tier classification and traceability obligations, and the December 2026 amendment to the Privacy Act 1988 (Cth) adds a disclosure obligation for automated decision-making.

These rules are easy to nod along to and hard to actually meet, because compliance has to be captured at the moment a decision is made, not reconstructed afterwards. Signal treats that record as a first-class part of the product rather than paperwork bolted on at the end.

## Governance design

The shipped core is [`signal/governance/decision_log.py`](signal/governance/decision_log.py): a Pydantic v2 schema and an append-only JSONL logger for AI-assisted decisions. Every field maps to a specific obligation, tagged in the source as `[APS]`, `[EU]`, or `[Privacy]`.

How it maps to the four APS mandatory requirements:

| APS requirement | Field in `DecisionEntry` |
|---|---|
| What AI system was used | `model_name`, `model_version`, `model_provider` |
| What decision was made | `decision_made`, `decision_category` |
| What data informed it | `data_sources`, `input_summary`, `model_output_summary` |
| Whether a human reviewed it | `human_review_required`, `human_reviewer`, `override_applied`, `override_reason` |

For the EU AI Act, `risk_category` records the tier (`unacceptable`, `high`, `limited`, `minimal`) so high-risk uses are flagged for the logging and oversight that Annex III requires. `confidence_score` and the UTC `timestamp` support the traceability obligations. A validator enforces that any human override carries a written reason, so the record cannot claim an override without explaining it.

The log is plain JSONL: one decision per line, UTF-8, no special tooling needed to read or grep it, and `to_dicts()` hands it straight to pandas or DuckDB for analysis.

```python
from signal.governance.decision_log import DecisionEntry, DecisionLogger

logger = DecisionLogger("logs/decisions.jsonl")

logger.log(DecisionEntry(
    model_name="claude-sonnet-4-6",
    model_provider="Anthropic",
    input_summary="Summarise Q1 2026 property crime trends for SA region.",
    model_output_summary="Property offences up 12% QoQ. Hotspots: Adelaide CBD, Port Adelaide.",
    decision_made="Flagged for senior analyst review and inclusion in Q1 brief.",
    data_sources=["ABS Crime Statistics 2025"],
    confidence_score=0.91,
    human_review_required=True,
    human_reviewer="senior.analyst@example.gov.au",
    risk_category="limited",
))
```

## Status and roadmap

The governance log is the first piece to ship and is the priority artefact. The interactive front end and the LLM analyst layer that writes to this log are next.

- [x] Governance decision log (APS / EU AI Act / Privacy Act aligned)
- [ ] LLM analyst layer that logs each query as a `DecisionEntry`
- [ ] Interactive product over a US public dataset
- [ ] Live `/api` endpoint deployed to Modal

## Reproduce

Requires Python 3.10 or later.

```bash
git clone https://github.com/rNLKJA/signal.git
cd signal
pip install -r requirements.txt

# run the built-in smoke test: writes and reads back one decision entry
python -m signal.governance.decision_log
```

## Licence

MIT. See [LICENSE](LICENSE).
