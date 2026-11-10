# Runbook

All commands from the repo root. `PY` = `python`. Docker CLI: `docker`.

## Ports and names

| Thing | Value |
|---|---|
| Postgres (compose project `fde-bi`, volume `fde-bi_bi_pgdata`) | host port 5435 |
| API and page | 8400 |
| Database | `larkfield_bi`; admin `bi_admin` (bootstrap only), owner `bi_owner` (loads, not a superuser), analyst `analyst_ro` (read-only); passwords in `.env` |
| LLM | Ollama `http://localhost:11434`, model `OLLAMA_MODEL` (default `fusion27b:iq3`), read on every call |

Other containers are not touched: 5433 is project 1, 5434 is project 2.

## First run

0. Copy `.env.example` to `.env` and set the three passwords (and the matching DSNs). `.env` is git-ignored. Passwords are fixed when the database volume is first created; to change them later: `docker compose down -v`, `up -d db`, reload.
1. `docker compose up -d db` and wait for healthy. Postgres listens on 127.0.0.1:5435 only.
2. `PY -m warehouse.load` bootstraps the roles as `bi_admin`, then creates the schema, grants and data as the non-superuser `bi_owner`, regenerating the CSVs (seeded) first. Re-running rebuilds everything. Expect: dim_date 365, dim_product 50, dim_customer 2000, fact_orders 8034, fact_order_items 13596, fact_returns 564, fact_tickets 1456.
3. `PY -m evals.validate_gold` checks that every gold SQL passes the guardrails, runs and returns rows.
4. `PY -m ruff check .` and `PY -m pytest -q`. `-m "not db and not llm"` skips the database tests (what CI runs).
5. `PY -m uvicorn app.main:app --port 8400`, open `http://localhost:8400`. Or `docker compose up -d` for the API in a container (reaches Ollama at `host.docker.internal`).

## The live eval (the orchestrator runs this)

One line, from this folder, with the warehouse loaded and Ollama serving `fusion27b:iq3`:

```
python -m evals.run --variant all
```

- Runs the agent, then the `ddl` baseline (raw CREATE TABLE text plus today's date), then the `floor` (table names only), 40 questions each; estimate 45-90 minutes, GPU-bound. Run one eval at a time and not while another eval is using the GPU.
- Writes `evals/report.md` and raw rows to `evals/results/live/` (git-ignored). Before a new run, archive the last one: copy `evals/report.md` to `evals/report-liveN-<date>.md` and move `evals/results/live` to `evals/results/live-runN`. Then copy the numbers into the README table.
- Published history: run 1 (`evals/report-live1-2026-11-10.md`, results in `evals/results/live-run1`) and run 2, the final run (`evals/report-live2-2026-11-10.md`, same as `evals/report.md`). Iteration stopped after run 2; do not edit the prompt or docs to fix the four remaining gold failures.
- Only a full `--variant all` run writes `evals/report.md`. Partial runs (`--variant agent`, `--ids G01,G07`, `--limit 5`) write `evals/results/live/report.md` instead.
- Pipeline check without the GPU: `PY -m evals.run --mock` (oracle LLM; never writes `evals/report.md`).
- Never edit a gold item after seeing the agent's output in that run. A rule change goes in the item's `notes` before the next run. Re-run the eval after any prompt, schema-docs, guardrail or model change.

## Reading a result

`evals/results/live/agent.jsonl`: one row per question with the agent's full result (SQL, attempts, guardrail blocks, tokens, latency) and the score (`checks`, `reasons`). `evals/report.md` lists each failed question with the agent SQL and the gold SQL side by side.

## Common problems

| Symptom | Cause and fix |
|---|---|
| DB tests skipped | Warehouse not up or not loaded, or `.env` missing: `docker compose up -d db`, then `PY -m warehouse.load` |
| `password authentication failed` or "no password in the owner DSN" | `.env` missing or its passwords differ from the volume's: fix `.env`, or `docker compose down -v`, `up -d db`, load. Only this project's volume is removed |
| Every question returns `error: model unavailable` | Ollama is down or the model is not pulled; `GET /health` shows `ollama: false` |
| HTTP 500 from Ollama | A second system message in a request. `llm.check_messages` guards against it; keep one system message |
| Answers look cut off or ignore the schema | Context smaller than the 3k-token prompt; the model needs an 8k context |
| `summary` is null with `summary_faithful: false` | The summary quoted a number not in the rows, or named the wrong row as lowest or highest; it was dropped on purpose and logged |
| Question refused as "write_request" or "pii_request" | The pre-filter (`screen_question`) or the model declined; check `refused_reason` in `logs/requests.jsonl` |

## Changing things safely

- Data or schema change: edit `warehouse/generate.py` or `schema.sql`, update `schema_docs.yaml` (a test compares it with the database), reload, re-run `evals.validate_gold`, then re-run the eval.
- New table for the agent: add it to `schema_docs.yaml` and to `ALLOWED_TABLES` in `app/guardrails.py` (a test checks they match), and grant SELECT to `analyst_ro` in `schema.sql`. Never grant on `dim_customer`.
- Switch provider: `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`; local stays the default.

## Cleanup

`docker compose down` stops the database and keeps the volume; `docker compose down -v` deletes the volume. `logs/` and `evals/results/` are git-ignored and safe to delete.
