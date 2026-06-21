# Building compliance into the request path: a case study

From 15 June 2026, AI governance becomes mandatory across the Australian Public Service. The Digital Transformation Agency's Policy for the responsible use of AI in government (Version 2.0) requires every agency to designate accountable officials, keep a register of in-scope AI use cases, publish AI transparency statements, and run AI use-case impact assessments. The first requirements commence on 15 June 2026 and the mandatory impact assessments follow by 15 December 2026. The EU AI Act adds risk classification and traceability on top, and the Privacy Act 1988 (Cth) reforms add a disclosure duty for automated decisions.

The rules are easy to agree with and hard to actually meet. Most teams treat the record as paperwork: something you assemble after the fact, when an auditor asks. That approach breaks down the moment you look closely, because the facts you need are freshest at the instant the decision is made and they decay quickly. Which model version answered? What was the exact data window? Did anyone actually check the spike before it went out? Reconstruct that a month later and you are guessing.

Signal is a small product I built to test a different idea: make the compliance record a side effect of answering, not a separate task. If the system cannot answer without writing the record, the record can never be missing.

## What it does

Signal is a governed analyst over crime data and a governed explorer over open data. You ask it a question, for example how theft is trending in Adelaide over the last twelve months, and it returns a plain-language summary backed by real numbers: the trend direction, the month-on-month and year-on-year change, the top offence categories, and any months unusual enough to flag for review. It runs over two jurisdictions, South Australia Police and the New York City Police Department, on the same governed path.

Beyond the crime data, the same portal behind the SA figures publishes around 1,900 open datasets, so Signal also lets you search and analyse the data.sa.gov.au, data.nsw.gov.au, data.vic.gov.au and NYC Open Data catalogues. Any dataset with a date and a number can be trended, and any dataset with coordinates is plotted on a map. Every one of those lookups is governed too.

Every answer carries a decision id. That id resolves to a full audit entry through a public endpoint, so anyone can trace any answer back to the model that produced it, the data that informed it, and whether a human needs to look. The audit trail is not a hidden log file. It is part of the product you can see and click.

## The design

The heart of it is one module, a decision log. It defines a typed schema for an AI-assisted decision and an append-only writer that puts one decision per line in a plain text file. Nothing exotic: you can read it with grep or load it straight into pandas.

The important decision was where to put the logging. In Signal the analyst physically cannot return an answer without first writing the audit entry. The two steps are welded together in the request path. There is no code path that answers a user and forgets to log, because answering is logging. This is the whole point. Compliance stops being a discipline people have to remember and becomes a property of the system.

## Checking the AI, not just logging it

The summaries are phrased by a language model from the computed figures, never from the raw data. That raises the question an auditor asks first: how do you know the model did not make a number up? Signal answers it by checking every summary against the statistics before it reaches anyone. The check is deterministic and runs without calling the model again. Every figure in the summary has to appear in the computed numbers, and the sentence describing the trend cannot contradict the computed direction. A summary that fails is rejected, the plain deterministic version is sent in its place, and the rejection is written to the same audit log. Each answer also carries a faithfulness score you can see on the result and in the audit trail, and a live model card reports the average score and how often the model was overruled. The model is allowed to phrase the answer. It is never trusted to invent one.

## Mapping to the DTA policy

The DTA policy does not ask for free text. It asks for specific artifacts, and Signal produces each one from the same log rather than as separate paperwork.

- **Accountable official and use-case owner.** Each entry records who is accountable: the reviewer, the officer, and the agency. These are configured per deployment, so a real agency would see its own names here.
- **The register of in-scope AI use cases.** The log is the register. A live endpoint rolls it up by use case, so you can see each named use, how many decisions it covers, its risk tier, the share that needed human review, and who the accountable reviewers were. The register is never out of date, because it is computed from the same decisions the product is making.
- **The AI transparency statement.** Another endpoint generates a transparency statement straight from the log: what AI is in use, what it is used for, what data informs it, the risk classification, the human oversight in place, and how the public can trace any answer. It is generated, not hand-written, so it cannot drift from what the system actually does.
- **The AI use-case impact assessment.** The policy makes this mandatory by December 2026, and Signal generates it now, one assessment per use case, from the same log. Each one sets out who is affected, the risks, the safeguards in place, the fairness considerations, and the residual risk. The safeguards are not boilerplate: they cite the live numbers, the faithfulness score and how often a result was held for human review, so the assessment reflects what the system is actually doing.

For the EU AI Act, a risk-tier field marks each decision as minimal, limited, high, or unacceptable, which flags the high-risk uses for the extra oversight the Act requires. A confidence score and a UTC timestamp support the traceability obligation. For the Privacy Act, the same fields already disclose when an answer was produced by an automated process and on what basis.

Three further rules sit in the analyst itself. First, it only ever sees aggregates. The source data is already de-identified, and Signal aggregates it further to monthly totals by region and offence, so no individual record and no personal information enters the system. Second, when a month is statistically unusual, the analyst sets the human-review flag automatically. A sudden spike should be checked by a person before anyone acts on it, and the system insists on that rather than leaving it to judgement. A reviewer can then record the review or an override, and an override will not save without a written reason. Third, every comparison between regions carries a plain fairness note: these are raw counts, not rates, and a gap between two places can reflect population size, how readily offences are reported, or how heavily an area is policed, as much as real offending. The figures are a starting point for questions, not a ranking, and not a reason to target a place or a group.

## Why this data

I work as a data analyst at South Australia Police, so I chose data from that same domain on purpose. It keeps the governance question concrete. Crime statistics are exactly the kind of sensitive, public-interest data where "how was this AI-assisted answer reached" is a real question with real consequences, not a hypothetical.

The data also taught me something useful. SA Police changed their offence classification partway through the period, so labels like "theft and related offences" became simply "theft". A trend that crossed that change would have fractured into two unrelated series. Handling it meant building a small harmonisation layer that maps both the old and new vocabularies onto one stable scheme, applied the same way to live data and the bundled snapshot. That kind of quiet taxonomy work is most of what real public-sector data engineering actually is.

## What I would do next

The honest limit is full agency hardening: authentication, a durable audit store, and a real owner behind the accountable-official field rather than a configurable placeholder. The explorer also samples very large datasets at a row cap rather than scanning them whole, which the product states plainly in the result. These are the next pieces of work rather than things already done.

## The takeaway

The lesson I would carry into any real agency system is simple. Do not bolt governance on at the end and hope people fill in the form. Wire it into the path the work already takes, so the record writes itself, and let the register and the transparency statement fall out of that same record. A compliance trail you have to remember to keep is a compliance trail you will eventually forget. One that the system cannot operate without is one you can actually trust, and one you can show a regulator on the day the rules commence.

Signal is open source and live. The code, the design notes, and the running demo are linked from the [README](README.md).

---

*Sources: [Policy for the responsible use of AI in government (DTA)](https://www.digital.gov.au/ai/ai-in-government-policy); [AI Policy Update: Strengthening responsible use across government (DTA)](https://www.dta.gov.au/articles/ai-policy-update-strengthening-responsible-use-across-government); [AI transparency statement (DTA)](https://www.dta.gov.au/ai-transparency-statement).*
