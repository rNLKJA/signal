# Signal roadmap

Where Signal is going, and the reasoning behind it. The current release is documented in the [README](README.md); this file is about v2 and v3.

## The spine

Signal is about one idea: **compliance as a property of the system, written on the request path.** The analyst cannot answer without first writing the audit entry, so the record can never go missing. The crime data is a vehicle for making that idea concrete, and the language model is a feature on top of it.

Every future change is judged against that spine. A change earns its place only if it makes the governance more **provable**, more **reusable**, or more **rigorously evaluated**. Anything that just adds surface area does not.

## v2 — prove the governance

The honest state of v1 is that it is a working demonstration, not a hardened system: a single container, a single-writer JSONL log, a configurable placeholder for the accountable official. The most credible next step is not to bolt on production plumbing that every backend already has. It is to make the central idea bulletproof and checkable. This fits inside the current single-container design, so it adds no infrastructure cost.

1. **A tamper-evident audit trail.** In a policing context the log is evidence, so the question an auditor asks is whether it could have been altered after the fact. Each decision entry will carry the hash of the entry before it, forming a chain, and a new `GET /decisions/verify` endpoint will re-walk the chain and confirm nothing has been edited or removed. A daily digest hash means even the whole file cannot be quietly rewound. This moves the traceability claim from "we wrote it down" to "we can show it was not changed."

2. **The logging invariant, enforced and checked.** Today the analyst is built so that answering is logging. v2 makes that harder to break by construction, and adds a conformance test that drives every answer-producing endpoint, including the error paths, and confirms each one writes exactly one audit entry. A deliberately introduced bypass should fail the build.

3. **A measured faithfulness eval.** The faithfulness check that guards the language model is necessary, but its own accuracy is currently unmeasured. v2 adds a small hand-labelled set of narratives, including adversarial near-misses, measures the check's precision and recall against it, and reports those numbers in the live model card. A language-model judge runs as an independent second opinion, and the two signals are compared. "We check faithfulness" becomes "here is how well the check actually performs."

4. **Governance as a reusable package.** The governance core is extracted into a standalone, documented, installable package so the story becomes a governance toolkit demonstrated on crime data, rather than a crime dashboard with a log attached.

**Done when:** someone can try to tamper with the log or bypass logging and the system catches it, and the faithfulness check's precision and recall can be stated as numbers.

## v3 — govern the agent, then harden for real

1. **A governed multi-step analyst.** The current language-model layer phrases pre-computed figures. The frontier, and the harder governance problem, is multi-step reasoning. v3 lets the analyst break a question into several sub-queries, each logged as its own decision linked to the parent, with the faithfulness check applied at every step and a final check on the synthesis. Per-step accountability for agentic systems is exactly the problem named as hardest in the Five Eyes guidance on agentic AI, so this demonstrates an answer to it rather than chasing the trend. It depends on the v2 tamper-evidence and linked-decision work being in place first.

2. **A real deployment.** Once the idea is proven, the production engineering is worth doing: a durable audit store (Postgres or object storage with an append log) keeping the same schema, real authentication and role-based access, and removing the single-container correctness pin properly rather than relying on it.

**Done when:** a compound question returns a traceable tree of governed decisions, and the system runs across more than one container without weakening the audit guarantee.

## Not doing

- Adding datasets for their own sake. Breadth earns a place only when a new source stresses the governance machinery in a new way, such as a new offence taxonomy or a new privacy regime.
- Becoming a general business-intelligence tool.
- Model or provider features that do not strengthen traceability.
- Rewriting the dashboard in a framework. The single self-contained page with no build step is a feature, not a limitation.

## How the order was chosen

Prove before producing. The thing that makes Signal worth looking at is the governance idea, so the highest-value work is making that idea verifiable and reusable, which v2 does without new infrastructure. Production hardening and an agentic layer are valuable, but they rest on the guarantees being solid first, which is why they sit in v3.
