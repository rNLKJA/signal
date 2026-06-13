# Building compliance into the request path: a case study

From 15 June 2026, every Australian Public Service agency has to account for how it uses AI. The Mandatory AI Requirements ask four plain questions of any AI-assisted decision: what system was used, what was decided, what data informed it, and whether a human reviewed it. The EU AI Act adds risk classification and traceability on top, and the December 2026 amendment to the Privacy Act 1988 (Cth) adds a disclosure duty for automated decisions.

The rules are easy to agree with and hard to actually meet. Most teams treat the record as paperwork: something you assemble after the fact, when an auditor asks. That approach breaks down the moment you look closely, because the facts you need are freshest at the instant the decision is made and they decay quickly. Which model version answered? What was the exact data window? Did anyone actually check the spike before it went out? Reconstruct that a month later and you are guessing.

Signal is a small product I built to test a different idea: make the compliance record a side effect of answering, not a separate task. If the system cannot answer without writing the record, the record can never be missing.

## What it does

Signal is a governed analyst over South Australian crime data. You ask it a question, for example how theft is trending in Adelaide over the last twelve months, and it returns a plain-language summary backed by real numbers: the trend direction, the month-on-month and year-on-year change, the top offence categories, and any months unusual enough to flag for review.

Every answer carries a `decision_id`. That id resolves to a full audit entry through a public endpoint, so anyone can trace any answer back to the model that produced it, the data that informed it, and whether a human needs to look. The audit trail is not a hidden log file. It is part of the product you can see and click.

## The design

The heart of it is one module, a decision log. It defines a typed schema for an AI-assisted decision and an append-only writer that puts one decision per line in a plain text file. Nothing exotic: you can read it with `grep` or load it straight into pandas.

The important decision was where to put the logging. In Signal the analyst physically cannot return an answer without first writing the audit entry. The two steps are welded together in the request path. There is no code path that answers a user and forgets to log, because answering is logging. This is the whole point. Compliance stops being a discipline people have to remember and becomes a property of the system.

## Mapping the four APS requirements

Each mandatory requirement maps to a specific field, so the record is structured rather than a free-text note someone hopes covers everything.

- **What AI system was used.** The entry records the model name, version, and provider. When a large language model phrases the narrative, the entry names that model. When it falls back to the built-in deterministic template, it says so honestly. The log never claims a model that did not run.
- **What decision was made.** A short description of the decision and a category, so decisions can be counted and grouped later.
- **What data informed it.** The exact data sources, a summary of the input, and a summary of the output. For Signal this names the SA Police dataset and the time window, every time.
- **Whether a human reviewed it.** A review flag, a reviewer field, and, if a human overrode the result, the reason. A validation rule refuses to accept an override without a written reason, so the record cannot quietly claim oversight that did not happen.

For the EU AI Act, a risk-tier field marks each decision as minimal, limited, high, or unacceptable, which is what flags the high-risk uses for the extra oversight the Act requires. A confidence score and a UTC timestamp support the traceability obligation. For the Privacy Act, the same fields already disclose when an answer was produced by an automated process and on what basis.

Two further rules sit in the analyst itself. First, it only ever sees aggregates. The source data is already de-identified suburb-level counts, and Signal aggregates it further to monthly totals by region and offence, so no individual record and no personal information enters the system at all. Second, when a month is statistically unusual, the analyst sets the human-review flag automatically. A sudden spike should be checked by a person before anyone acts on it, and the system insists on that rather than leaving it to judgement.

## Why South Australian data

I work as a data analyst at South Australia Police, so I chose data from that same domain on purpose. It keeps the governance question concrete. Crime statistics are exactly the kind of sensitive, public-interest data where "how was this AI-assisted answer reached" is a real question with real consequences, not a hypothetical.

The data also taught me something useful. SA Police changed their offence classification partway through the period, so labels like "theft and related offences" became simply "theft". A trend that crossed that change would have fractured into two unrelated series. Handling it meant building a small harmonisation layer that maps both the old and new vocabularies onto one stable scheme, applied the same way to live data and the bundled snapshot. That kind of quiet taxonomy work is most of what real public-sector data engineering actually is.

## What I would do next

The honest limitation is reach. South Australia publishes clean, queryable monthly data through an open API. Most other states publish the same kind of information as spreadsheets, not live endpoints, so a national cross-state view would need a separate ingestion layer for each one. That is the next piece of work rather than a thing already done, and the README says so plainly.

## The takeaway

The lesson I would carry into any real agency system is simple. Do not bolt governance on at the end and hope people fill in the form. Wire it into the path the work already takes, so the record writes itself. A compliance trail you have to remember to keep is a compliance trail you will eventually forget. One that the system cannot operate without is one you can actually trust.

Signal is open source and live. The code, the design notes, and the running demo are linked from the [README](README.md).
