"""Run the gold set against the agent and two baselines, and write evals/report.md.

    python -m evals.run --variant all            # live model: agent, ddl, floor (GPU-bound, about an hour or more)
    python -m evals.run --variant agent --ids G01,G07
    python -m evals.run --mock                   # oracle LLM, no GPU: checks the pipeline, never writes report.md

Variants: agent = schema docs + sample values + examples; ddl = raw CREATE TABLE text and today's date (the
standard comparison); floor = table names only.

Gold items are never edited after a run has seen the agent's output; a rule change goes in the item's
`notes` before the next run.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import psycopg

from app import agent, llm
from app.config import settings
from evals import scoring
from evals.mock_llm import OracleLLM

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "gold.jsonl"
RESULTS_DIR = HERE / "results"
REPORT_PATH = HERE / "report.md"
CATEGORIES = ("simple", "complex", "refuse")
VARIANTS = ("agent", "ddl", "floor")
REFUSAL_SOURCES = ("prefilter", "model", "parser", "db")


def load_gold(path: Path = GOLD_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (95% by default)."""
    if total == 0:
        return (0.0, 0.0)
    p = passed / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def run_variant(variant: str, cases: list[dict], conn, chat_fn=None, log_path=None, progress=True) -> list[dict]:
    out = []
    for n, case in enumerate(cases, start=1):
        started = time.monotonic()
        result = agent.ask(case["question"], variant=variant, chat_fn=chat_fn, log_requests=log_path is not None,
                           log_path=log_path)
        score = scoring.score_case(case, result, conn)
        out.append({"id": case["id"], "category": case["category"], "question": case["question"],
                    "result": result, "score": score})
        if progress:
            flag = "PASS" if score["passed"] else "FAIL"
            print(f"[{variant}] {n}/{len(cases)} {case['id']} {flag} ({time.monotonic() - started:.1f}s)", flush=True)
    return out


def summarise(rows: list[dict]) -> dict:
    by_cat = defaultdict(lambda: [0, 0])
    for r in rows:
        by_cat[r["category"]][1] += 1
        by_cat[r["category"]][0] += int(r["score"]["passed"])
    lat = [r["result"]["usage"]["latency_ms"] for r in rows]
    toks = [r["result"]["usage"]["prompt_tokens"] + r["result"]["usage"]["completion_tokens"] for r in rows]
    answered = [r for r in rows if r["result"].get("status") == "answered" and r["result"].get("rows")]
    faithful = [r for r in answered if r["result"].get("summary_faithful")]
    sources = Counter(r["result"].get("refused_source") for r in rows if r["result"].get("refused"))
    return {
        "by_category": {c: tuple(by_cat[c]) for c in CATEGORIES if c in by_cat},
        "passed": sum(v[0] for v in by_cat.values()),
        "total": sum(v[1] for v in by_cat.values()),
        "guardrail_blocks": sum(r["result"].get("guardrail_blocks", 0) for r in rows),
        "refused": sum(1 for r in rows if r["result"].get("refused")),
        "refusal_sources": {s: sources.get(s, 0) for s in REFUSAL_SOURCES},
        "executed": sum(1 for r in rows if r["result"].get("executed")),
        "errors": sum(1 for r in rows if r["result"].get("status") == "error"),
        "summary_errors": sum(1 for r in rows if r["result"].get("summary_error")),
        "corrections": sum(1 for r in rows if len(r["result"].get("attempts", [])) > 0),
        "p50_ms": round(percentile(lat, 0.5)), "p95_ms": round(percentile(lat, 0.95)),
        "tokens_per_q": round(statistics.mean(toks)) if toks else 0,
        "faithful": len(faithful), "faithful_of": len(answered),
    }


def _pct(passed: int, total: int, ci: bool = True) -> str:
    if not total:
        return "n/a"
    text = f"{passed}/{total} ({100 * passed / total:.0f}%"
    if ci:
        lo, hi = wilson(passed, total)
        text += f", 95% CI {100 * lo:.0f}-{100 * hi:.0f}%"
    return text + ")"


def build_report(results: dict[str, list[dict]], cases: list[dict], meta: dict) -> str:
    """Markdown report from per-variant result rows. Pure."""
    variants = list(results)
    sums = {v: summarise(results[v]) for v in variants}
    gold_by_id = {c["id"]: c for c in cases}
    lines = ["# Eval report", ""]
    if meta.get("mock"):
        lines += [("> MOCK RUN: the LLM is an oracle that returns the gold SQL. This checks the pipeline only. "
                   "These numbers say nothing about the model."), ""]
    lines += [
        f"Run: {meta['date']}. Model: `{meta['model']}`. Gold items: {len(cases)}. Today = {settings.today}.",
        "",
        ("Variants: agent = schema docs, sample values and examples; ddl = raw CREATE TABLE text plus today's date; "
        "floor = table names only. Intervals are Wilson 95% intervals; with 40 questions they are wide, so small "
        "differences are not evidence."),
        "", "## Execution accuracy by category", "",
        "| Category | n | " + " | ".join(variants) + " |", "|---|---|" + "---|" * len(variants),
    ]
    for cat in CATEGORIES:
        n = sum(1 for c in cases if c["category"] == cat)
        cells = [_pct(*sums[v]["by_category"].get(cat, (0, 0))) if cat in sums[v]["by_category"] else "n/a"
                 for v in variants]
        lines.append(f"| {cat} | {n} | " + " | ".join(cells) + " |")
    lines.append("| **overall** | " + str(len(cases)) + " | "
                 + " | ".join(f"**{_pct(sums[v]['passed'], sums[v]['total'])}**" for v in variants) + " |")
    lines += [
        "",
        ("Scorer limits: G26 (return rate by category) cannot tell 'lines on delivered orders' from 'lines on "
        "non-cancelled orders', because every Jul-Sep order is delivered; G28 (highest return rate) names the same "
        "product under several denominators, so only its rate separates them. See the items' `notes`."),
        "", "## Operations", "", "| Metric | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants),
    ]
    rows = [
        ("Guardrail blocks (parser or database)", lambda s: str(s["guardrail_blocks"])),
        ("Questions declined", lambda s: str(s["refused"])),
        ("Declined by the question pre-filter", lambda s: str(s["refusal_sources"]["prefilter"])),
        ("Declined by the model (REFUSE / UNANSWERABLE)", lambda s: str(s["refusal_sources"]["model"])),
        ("Declined by the SQL parser", lambda s: str(s["refusal_sources"]["parser"])),
        ("Declined by the database (privileges)", lambda s: str(s["refusal_sources"]["db"])),
        ("Questions with a failed attempt (guardrail, database or format)", lambda s: str(s["corrections"])),
        ("Hard errors (no answer, no refusal)", lambda s: str(s["errors"])),
        ("Summary call errors (rows still scored)", lambda s: str(s["summary_errors"])),
        ("Latency p50 / p95 (ms)", lambda s: f"{s['p50_ms']} / {s['p95_ms']}"),
        ("Tokens per question (prompt + completion)", lambda s: str(s["tokens_per_q"])),
        ("Summary faithfulness (summary passed the number check)",
         lambda s: _pct(s["faithful"], s["faithful_of"], ci=False)),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(sums[v]) for v in variants) + " |")
    for v in variants:
        fails = [r for r in results[v] if not r["score"]["passed"]]
        lines += ["", f"## Failed questions: {v} ({len(fails)})", ""]
        if not fails:
            lines.append("None.")
        for r in fails:
            res = r["result"]
            lines += [f"### {r['id']} ({r['category']}): {r['question']}", "",
                      "Reasons: " + "; ".join(r["score"]["reasons"] or ["n/a"]), ""]
            sql = res.get("sql") or (res.get("attempts") or [{}])[-1].get("sql")
            lines += ["Agent SQL:", "", "```sql", sql or "(none)", "```", ""]
            gold_sql = gold_by_id[r["id"]].get("sql")
            if gold_sql:
                lines += ["Gold SQL:", "", "```sql", gold_sql, "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=[*VARIANTS, "all"], default="all")
    ap.add_argument("--ids", help="comma-separated gold ids to run")
    ap.add_argument("--limit", type=int, help="run only the first N cases")
    ap.add_argument("--mock", action="store_true", help="oracle LLM (gold SQL); writes under evals/results/mock")
    args = ap.parse_args(argv)

    cases = load_gold()
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    variants = list(VARIANTS) if args.variant == "all" else [args.variant]

    out_dir = RESULTS_DIR / ("mock" if args.mock else "live")
    out_dir.mkdir(parents=True, exist_ok=True)
    chat_fn = OracleLLM(load_gold()).chat if args.mock else llm.chat
    if not args.mock:
        print(f"Live run on {settings.llm_provider}:{settings.ollama_model}. GPU-bound; run one eval at a time.",
              flush=True)

    results: dict[str, list[dict]] = {}
    with psycopg.connect(settings.analyst_dsn) as conn:
        for v in variants:
            results[v] = run_variant(v, cases, conn, chat_fn=chat_fn, log_path=out_dir / f"{v}.requests.jsonl")
            with (out_dir / f"{v}.jsonl").open("w", encoding="utf-8") as fh:
                for r in results[v]:
                    fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    meta = {"date": date.today().isoformat(), "mock": args.mock,
            "model": "oracle-mock" if args.mock else f"{settings.llm_provider}:{settings.ollama_model}"}
    report = build_report(results, cases, meta)
    full_run = args.variant == "all" and not (args.mock or args.ids or args.limit)
    target = REPORT_PATH if full_run else out_dir / "report.md"
    target.write_text(report, encoding="utf-8")
    print(f"report written to {target}")
    for v in variants:
        s = summarise(results[v])
        print(f"{v}: {s['passed']}/{s['total']} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
