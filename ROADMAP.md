# Signal roadmap

Where Signal is going, and the reasoning behind it. The current release is documented in the [README](README.md); this file is about v2 through v5, and the path from a portfolio project to a product worth paying for.

## The product thesis

Signal began as a governed crime-data analyst. The sellable thing inside it is not the crime data. It is the **governance layer**: a way to make every AI-assisted decision logged, checked and tamper-evident on the request path, and to generate the compliance artefacts a regulator asks for straight from that log.

So the plan separates two things:

- **The product** is the governance layer, extracted into something another team can adopt: a library and a hosted control plane that give any AI system a tamper-evident audit trail, an enforced "cannot answer without logging" guarantee, narrative faithfulness checks, and live DTA Policy v2.0 and EU AI Act artefacts.
- **The crime analyst** stays as the reference implementation and the live demo. It proves the layer works on real, sensitive, public-interest data, in the author's own domain.

**Who buys it.** Australian government agencies and the vendors who sell AI into them, facing the DTA Policy mandatory requirements that commenced 15 June 2026 and the impact-assessment duty from 15 December 2026, plus regulated enterprises under the EU AI Act and the Privacy Act 1988 reforms. These buyers must produce specific records and artefacts, on a deadline, and most would rather adopt than build.

**The wedge.** Existing AI-governance tools are mostly dashboards that sit beside the system: model registries, policy libraries, questionnaires. Signal's difference is governance that runs in the code path and cannot be skipped, a tamper-evident record, and artefact generation localised to the Australian DTA policy specifically, which the large incumbents underserve.

## The spine

Every change is judged against one idea: **compliance as a property of the system, written on the request path.** A change earns its place only if it makes the governance more provable, more reusable, or more rigorously evaluated. Anything that just adds surface area does not.

## v2 — prove the governance

Make the central idea bulletproof and checkable. This fits inside the current single-container design, so it adds no infrastructure cost. It is also the groundwork for the product: a thing worth selling has to be provably correct first.

1. **A tamper-evident audit trail.** *Shipped (v1.14.0).* Each entry carries the hash of the one before it, `GET /decisions/verify` re-walks the chain, and the dashboard shows a live verification badge. Editing, deleting or reordering any past decision is detectable.
2. **The logging invariant, enforced and checked.** Make "answering is logging" hard to break by construction, and add a conformance test that drives every answer endpoint, including error paths, and confirms each writes exactly one audit entry. A deliberate bypass fails the build.
3. **A measured faithfulness eval.** A hand-labelled set, including adversarial near-misses, measures the faithfulness check's own precision and recall, reported in the live model card, with a language-model judge as an independent second signal.
4. **Governance as a package.** Extract the governance core into a standalone, documented, installable package. This is the seed of the product.

**Done when:** a sceptic can try to tamper with the log or bypass logging and the system catches it, and the faithfulness check's precision and recall can be stated as numbers.

## v3 — govern the agent, and harden for real

1. **A governed multi-step analyst.** Let the analyst break a question into linked sub-decisions, each logged and faithfulness-checked, so accountability survives multi-step reasoning. This is the agentic-governance problem named as hardest in the Five Eyes guidance, answered rather than chased.
2. **A real deployment.** A durable audit store keeping the same schema, real authentication and role-based access, and removing the single-container correctness pin properly. For a product these stop being optional and become the baseline.

**Done when:** a compound question returns a traceable tree of governed decisions, and the system runs across more than one container without weakening the audit guarantee.

## v4 — productise

The leap from a project someone admires to a product someone adopts.

1. **A drop-in SDK.** The governance package becomes middleware any FastAPI or OpenAI-compatible app can add in a few lines to get a tamper-evident audit trail, the logging guarantee, faithfulness checks and the generated artefacts, without rewriting around Signal.
2. **A hosted control plane.** A multi-tenant service where a compliance officer, not an engineer, can see the live register, transparency statement, impact assessment and chain-verification across their organisation's AI use cases, and export an auditor-ready report.
3. **Onboarding and docs.** A quickstart that takes a new team from install to their first governed, verifiable decision quickly, with worked examples beyond crime data so the layer reads as domain-agnostic.
4. **A pricing model and a design partner.** An open-core split (the SDK stays open to earn trust and adoption; the control plane, support and certified reports are paid), and one real design partner using it on a genuine use case.

**Done when:** a team other than Signal's own runs the SDK in their app and a non-engineer reads their compliance posture from the control plane.

## v5 — sell it

The things that survive a procurement and security review, plus the commercial assets to close a sale.

1. **Procurement-grade controls.** Single sign-on, organisation and role administration, data residency, and a documented security posture. A buyer's security team has to be able to say yes.
2. **A compliance report a buyer can hand an auditor.** Map the audit log and artefacts to recognised frameworks, ISO/IEC 42001, the NIST AI Risk Management Framework, and the specific DTA mandatory requirements, generated as a report rather than written by hand.
3. **Commercial operations.** Billing and plans, service levels, and a deployment a customer can run in their own tenancy if they require it.
4. **Go-to-market assets.** A landing page, clear pricing, a security and trust page, and a reference case study with the design partner. The honest version of "for sale", not a hope of it.

**Done when:** a buyer can evaluate, trust, deploy and pay for Signal without a custom engagement for each step.

## Commercial reality

Said plainly, so the plan is not wishful.

- **The market is real but specific.** The AU government AI-governance mandates created a live procurement need with deadlines. That is the beachhead. It is not a mass market, and it rewards depth in one jurisdiction over breadth across many.
- **The competition is real too.** Larger governance platforms exist. Signal does not win on breadth of features. It wins, if it wins, on enforced-in-the-code-path governance, tamper-evidence, and being genuinely tuned to the Australian policy rather than a global checklist.
- **Open-core is the likely model.** Governance tooling has to be inspectable to be trusted, so the core stays open. Revenue comes from the hosted control plane, support and service levels, certified reports, and managed or in-tenancy deployment.
- **The first dollar is probably a pilot, not self-serve.** The realistic first revenue is a paid pilot with one agency or one vendor selling into government, not a credit-card signup. v4's design partner is the bridge to it.
- **The honest risks.** A single maintainer cannot carry enterprise sales and support alone for long; the licensing split needs care so the open core still drives adoption; and the buyer's procurement cycle is slow. None of these is a reason not to build it. They are reasons to sequence it so the product is provably correct and adoptable before the commercial machinery is built around it.

## How the order was chosen

Prove, then harden, then productise, then sell. Each stage rests on the one before it. There is no point building billing and single sign-on around an audit trail that cannot yet prove its own integrity, and no point pitching a buyer a control plane that does not exist. v2 and v3 make the thing correct and real; v4 makes it adoptable; v5 makes it purchasable.

## Not doing

- Competing on breadth of governance features against the incumbents.
- Adding datasets for their own sake. A new source earns its place only when it stresses the governance machinery in a new way.
- Building commercial machinery, billing, single sign-on, sales pages, before the product is provably correct and someone outside the project is using it.
- Rewriting the dashboard in a framework. The single self-contained page with no build step stays.
