# Model card — Signal analyst

A static companion to the live card at `GET /governance/model-card`, which adds
the eval results read back from the audit log.

## Overview

Signal answers questions over already-aggregated, de-identified public crime data
and audit-logs every answer. It has two narrative layers:

| Component | Type | Role |
|---|---|---|
| `signal-stats-v1 (deterministic)` | Deterministic statistics | Computes totals, month-on-month / year-on-year change, trend direction and z-score anomalies. Writes the default narrative. |
| LLM (provider-agnostic, DeepSeek by default) | LLM narrative (optional) | Phrases the narrative from the **computed aggregates only** — it never sees raw records. Its output is gated by the faithfulness eval below. |

## Narrative faithfulness eval

The LLM is asked to write the summary using only the supplied statistics. We do
not take that on trust. Before any LLM narrative reaches a user it is checked,
deterministically and without a model call:

- **No fabricated figures** — every number in the narrative must appear in (or be
  directly read from) the computed statistics. Years and months from the window,
  per-series totals, percentages and counts are all allowed; anything else is a
  fabricated figure.
- **No trend contradiction** — the sentence describing the trend must not assert
  the opposite of the computed direction.

A narrative that fails is **rejected**: the deterministic template is served
instead, and the rejection is recorded in the audit log (tag
`faithfulness-fallback`, with the reason in `notes`). Every narrated decision
carries a `faithfulness_score` in the log; the live model card aggregates the
mean score and the number of fallbacks.

This is the eval-coverage half of a governance question an auditor will always
ask: *how do you know the AI didn't make a number up?*

## Intended use

Surface trends and anomalies in aggregate public-safety data, with a governed,
inspectable audit trail. Aligned to the DTA *Policy for the responsible use of AI
in government* (v2.0), the EU AI Act (Art. 50), and the Privacy Act 1988 (Cth).

## Out of scope

- Individual-level prediction, profiling, or any decision about a person.
- Operational policing or resource-allocation decisions.
- Any use over data containing personal information.

## Limitations

- **Counts are not rates.** Differences across regions may reflect population,
  reporting, or policing intensity rather than real offending.
- Anomaly and trend thresholds are fixed heuristics, not calibrated models.
- The faithfulness eval gates figures, not tone or nuance; the deterministic
  template is always the fallback.
- Large explorer datasets are sampled at a row cap (flagged in the result).
