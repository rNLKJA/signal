# signalkit.governance

A small, standalone toolkit for **governed AI decisions**. It gives any AI system a typed, tamper-evident audit trail and generates the compliance artefacts a regulator asks for, straight from that trail.

This is the reusable core of [Signal](../../README.md). The crime-data analyst in the rest of the repo is one application of it; this package has no dependency on that app. It needs only Pydantic and the standard library.

## Why it exists

Most AI-governance tooling sits beside the system as a dashboard you fill in later. This package is built to sit **on the request path**: logging is meant to happen as part of answering, so the system cannot return a result without first writing the record. A record you have to remember to keep is one you will eventually forget; one the system cannot operate without is one you can trust, and show an auditor.

It is aligned to the Australian Government's [Policy for the responsible use of AI in government](https://www.digital.gov.au/ai/ai-in-government-policy) (DTA, v2.0), the EU AI Act traceability obligations, and the Privacy Act 1988 (Cth) automated-decision disclosure.

## A governed decision in a few lines

```python
from signalkit.governance import DecisionEntry, DecisionLogger, RiskCategory

log = DecisionLogger("decisions.jsonl")

log.log(DecisionEntry(
    model_name="my-model-v1",
    input_summary="user asked X",
    model_output_summary="answered Y",
    decision_made="Returned Y to the user.",
    risk_category=RiskCategory.limited,
    human_review_required=False,
))

assert log.verify().valid   # the hash chain is intact
```

The log is plain JSONL, one decision per line, UTF-8, grep-able and loadable straight into pandas or DuckDB.

## What you get

| Capability | API |
|---|---|
| Typed audit entry for an AI decision | `DecisionEntry`, `DecisionCategory`, `RiskCategory` |
| Append-only logger that hash-chains each entry | `DecisionLogger` (`.log`, `.read_all`, `.verify`) |
| Verify the chain is intact (detect any edit/deletion) | `verify_chain` → `ChainVerification` |
| Governance summary (review rate, risk tiers, models) | `summarise` → `GovernanceSummary` |
| DTA register of in-scope AI use cases | `register` → `UseCaseRegister` |
| DTA AI transparency statement | `transparency_statement` → `TransparencyStatement` |
| DTA AI use-case impact assessment | `impact_assessment` → `ImpactAssessment` |
| Model card (with live faithfulness results) | `model_card` → `ModelCard` |

Every artefact is generated from the same log the system writes, so it can never drift from what the system actually does.

## Tamper-evidence

Each entry stores the hash of the entry before it (`prev_hash`) and a hash of its own content (`entry_hash`), forming a chain. `verify_chain` re-walks it and reports `valid`, the `head_hash` digest, and the `decision_id` of any break. Editing, deleting or reordering a past decision is detectable. Entries written before a chain begins are reported as `legacy` rather than failing the check, so the package can be adopted over an existing log.

## Generating the artefacts

```python
from signalkit.governance import summarise, register, transparency_statement, impact_assessment

entries = log.read_all()

summarise(entries)                                            # posture at a glance
register(entries, agency="My Agency", accountable_official="Jane Doe")
transparency_statement(entries, agency="My Agency", accountable_official="Jane Doe")
impact_assessment(entries, agency="My Agency", accountable_official="Jane Doe")
```

## Design notes

- **Dependencies:** Pydantic v2 and the standard library only. No web framework, no database.
- **Storage is pluggable.** `DecisionLogger` keeps the governance logic (hash-chaining, verification); *where* the lines are kept is an `AuditStore` — a three-method interface (`append`, `read_lines`, `last_line`). The default is `JsonlAuditStore` (a JSONL file); `InMemoryAuditStore` is handy for tests. A durable backend (Postgres, or object storage with an append log) implements the same interface, so the tamper-evidence and the "log on the request path" guarantee hold whatever the storage.

  ```python
  from signalkit.governance import (
      DecisionLogger, JsonlAuditStore, SqliteAuditStore, InMemoryAuditStore,
  )

  DecisionLogger("decisions.jsonl")          # JSONL file (default)
  DecisionLogger(SqliteAuditStore("audit.db"))  # a real, durable SQL database, no server
  DecisionLogger(InMemoryAuditStore())       # ephemeral, for tests
  DecisionLogger(MyPostgresStore(dsn))       # any object with append/read_lines/last_line
  ```

  `SqliteAuditStore` is a transactional, durable backend with nothing to provision — and it has the same shape as a Postgres store, so it de-risks moving to one. The tamper-evidence is identical across all of them, because it lives in `DecisionLogger`, not the storage.
- **Accountability fields:** `agency`, `officer_id`, `human_reviewer` and a `legislative_basis` are configurable per deployment, so a real agency stamps its own names on every record.

## Multi-tenancy

One deployment can serve many organisations without their records ever mixing. `TenantLog` gives each tenant its own `DecisionLogger` over its own storage, so each has an **independent tamper-evident chain**. Isolation is structural: a tenant has no handle to another's log, and `tenant_id` is part of the signed content, so a record cannot be moved between tenants without breaking its hash.

```python
from signalkit.governance import TenantLog, tenant_for_api_key, parse_tenant_keys

log = TenantLog.sqlite_dir("/var/lib/signal")      # a database per tenant
keys = parse_tenant_keys(os.environ.get("SIGNAL_TENANT_KEYS"))  # "k-acme:acme,k-globex:globex"

tenant = tenant_for_api_key(request_api_key, keys)  # defaults to "public" when unmapped
log.log(entry, tenant_id=tenant)
log.verify(tenant)                                  # only this tenant's chain
log.register(tenant, agency="Acme", accountable_official="Jane")
```

A single-tenant deployment ignores all of this and uses `DecisionLogger` directly; `tenant_id` stays unset.

## Where this is heading

This package is the seed of a product: a drop-in governance layer other AI systems can adopt, with a hosted control plane for compliance officers on top. See the repository [ROADMAP.md](../../ROADMAP.md).
