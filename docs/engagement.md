# Engagement note: ask-your-data for Larkfield Coffee Co.

Larkfield is a fictional customer; everything here is simulated.

## Discovery

**Who asked.** Larkfield's ops and marketing leads, through the one analyst they share. The analyst answers the same shapes of question all day ("returns rate for grinders last quarter?", "top 5 beans by revenue in Bengaluru?") and is the bottleneck.

**Problem.** Routine, answerable-from-the-warehouse questions queue behind the analyst. Leads will not learn SQL, and will not accept a tool that changes data or exposes customers.

**Success metric agreed.**
- Primary: execution accuracy on a 40-question gold set written from the data before any agent run (25 simple, 10 complex, 5 must-refuse), measured against a baseline of the same model given only table names.
- Safety, not negotiable: zero writes, zero personal data returned. Every must-refuse case passes; the database role enforces it even if the model and the parser fail.
- Trust: every answer shows its SQL; every number in the written summary exists in the rows, or the summary is dropped.
- Operational: p50 and p95 latency and tokens per question are reported, on a local model at no recurring cost.

The target accuracy was not set in advance; the measured result is in the Results section below. Comparisons use two baselines on the same model: the raw DDL plus today's date (what a team gets by pasting its schema in) and table names only, and every figure carries a Wilson 95% interval because 40 questions is a small sample.

**How to read `schema_docs.yaml`.** It is an agreed semantic layer, not a prompt trick: the definitions (revenue, return rate, repeat customer, fiscal quarter, "last quarter") were written down with the analysts' question types in mind and are meant to be signed off by them. The gold set was written from the same phrasing, so it measures how the layer and the model cope with questions the layer was designed for.

**Planned, not built: a held-out set.** After the docs are frozen, collect 10-15 fresh questions from the leads (never shown to whoever edits the docs), score them once with the same scorer, and report them separately from the 40. A large gap between the two sets would mean the layer is overfitted to the gold phrasing.

**Assumptions made.**
- "Today" is 2026-11-10 and the warehouse is a month-end snapshot through 2026-10-31. Fiscal year is April-March.
- "Last quarter" means the last completed fiscal quarter (Jul-Sep 2026); "this year" means the fiscal year to date. These and the other vague time words are defined in `schema_docs.yaml`, which the agent reads.
- Revenue means amount paid for orders (cancelled excluded) and line totals for products; return rate means returned lines over lines sold on delivered orders. Written down in the same file so the analyst can change a definition in one place.
- Leads want counts and aggregates by city, state, channel and product, not individual customers.
- Hinglish phrasing is common enough to test (three gold questions).
- Local-first: a local model, a local Postgres, no external calls.

**Left out, on purpose.**
- Writes of any kind, including "fix this price".
- Customer-level data: names, emails, phones, addresses. A `dim_customer_safe` view is the only customer table the analyst role can read.
- Charts, scheduled reports, follow-up conversation memory: each question stands alone.
- Questions about marketing spend, CAC and profit after overheads: the warehouse has no such tables, and the agent says what is missing instead of guessing.
- Data after 2026-10-31 (November questions are declined as not loaded).
- Multi-tenant access control and per-user row filters.

## Results

Final live run (run 2, 2026-11-10, local `fusion27b:iq3`, 40 gold questions; full tables and failures in `README.md` and `evals/report.md`):

| Variant | Accuracy (95% Wilson CI) |
|---|---|
| Agent (schema docs, conventions, samples, examples) | 36/40 = 90% (77-96%) |
| DDL baseline (raw CREATE TABLE plus the date) | 17/40 = 42% (29-58%) |
| Floor (table names only) | 7/40 = 18% (9-32%) |

- By category the agent scored 24/25 simple, 7/10 complex and 5/5 must-refuse. The DDL baseline scored 13/25, 0/10 and 4/5.
- The gap comes from the semantic layer: the DDL baseline has the columns but not the definitions (revenue, return rate, fiscal quarters, "last quarter"), and got no complex question right.
- Safety held: no write ran, no personal data was returned; the parser and database were never needed to stop an agent query. The agent's 5 refusals came from the pre-filter (3) and the model (2).
- Cost: the agent used about 3,245 tokens per question at p50 11.7 s and p95 19.7 s on the local model; the DDL baseline about 1,147 tokens.
- Four agent misses are genuine SQL errors (G11 cancelled orders included, G28 units instead of order lines, G32 wrong month attribution, G35 first month without a baseline). They stay in the report.
- Run 1 scored the agent 31/40; the rise to 36 is entirely a scorer change (supporting counts beside a label or a rate are now accepted), not a better agent. See "Runs" in the README.
- Not measured: questions nobody anticipated. The held-out set below is still planned, not built.

## What was built

A text-to-SQL agent over a seeded 12-month star schema (orders, items, returns, tickets, customers, products, dates), with a schema-docs prompt, a guardrail layer, a least-privilege database role, one self-correction round, a grounded summary, request logging, a local page, and an evaluation harness with a baseline. See `README.md` for structure and `docs/runbook.md` to run it.

## Risks and how they are covered

| Risk | Cover |
|---|---|
| Model writes a destructive or PII query | Parser rejects it; the role has no privilege to run it; test proves INSERT, DELETE, CREATE and `SELECT email FROM dim_customer` fail at the database |
| Slow or runaway query | `statement_timeout` on the role and per session; row cap by server-side cursor |
| Plausible but wrong SQL | SQL always shown; gold-set accuracy reported honestly with failed questions listed |
| Summary invents a figure | Number check against the rows; summary dropped and logged on failure |
| Docs drift from the database | A test compares `schema_docs.yaml` with the live columns and allowed values |
| Gold set gamed | Gold written first; never edited after seeing a run (the 2026-11-10 review changes were made before any live result and are recorded in the items' notes); few-shot examples are checked not to be gold; the app never reads `evals/`; scorer tests include wrong answers that must fail |
| Credentials or ports exposed | Database and API bind to 127.0.0.1; passwords only in `.env`; the loader is a non-superuser; PUBLIC has no database privileges (no TEMP) |

## Rollout suggestion

1. Shadow the analyst for two weeks: leads ask the agent, the analyst spot-checks the SQL.
2. Add the failing question patterns to `schema_docs.yaml` (a definition or an example), re-run the eval, keep the table as a regression suite.
3. Open to all leads once the must-refuse set stays at 100% and accuracy on real questions is agreed.
