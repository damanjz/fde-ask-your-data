"""Validate the gold set against the loaded warehouse: every gold SQL passes the guardrails, runs, and returns
rows (or is documented as empty); ordered items have a numeric column without ties; the mix is 25/10/5.

    python -m evals.validate_gold
"""

from __future__ import annotations

import sys
from collections import Counter
from decimal import Decimal

import psycopg

from app import guardrails
from app.config import settings
from app.db import fetch
from evals.run import load_gold

EXPECTED = {"simple": 25, "complex": 10, "refuse": 5}
REFUSE_KINDS = {"dml", "pii", "unanswerable"}


def validate(cases: list[dict], conn) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    counts = Counter(c["category"] for c in cases)
    if dict(counts) != EXPECTED:
        problems.append(f"category mix {dict(counts)} != {EXPECTED}")
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    if len({c["question"] for c in cases}) != len(cases):
        problems.append("duplicate questions")
    for c in cases:
        cid = c["id"]
        if c["category"] not in EXPECTED:
            problems.append(f"{cid}: bad category {c['category']!r}")
            continue
        if c["category"] == "refuse":
            if c.get("sql") is not None or c.get("refuse_kind") not in REFUSE_KINDS:
                problems.append(f"{cid}: refuse items need sql null and refuse_kind in {sorted(REFUSE_KINDS)}")
            continue
        try:
            sql = guardrails.validate_sql(c["sql"]).sql
        except guardrails.GuardrailError as exc:
            problems.append(f"{cid}: gold SQL fails the guardrails: {exc}")
            continue
        try:
            res = fetch(conn, sql)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{cid}: gold SQL fails to run: {exc}")
            continue
        if not res.rows and "empty" not in c.get("notes", "").lower():
            problems.append(f"{cid}: gold SQL returns no rows and notes do not document an empty result")
        if c.get("ordered") and len(res.rows) > 1:
            ok = False
            for j in range(len(res.columns)):
                col = [r[j] for r in res.rows]
                if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in col) \
                        and len(set(col)) == len(col):
                    ok = True
            if not ok:
                problems.append(f"{cid}: ordered item has no numeric column free of ties; add a tie-breaker")
        if res.truncated:
            problems.append(f"{cid}: gold result is truncated at the row cap")
        problems.extend(_check_precision(c, res))
    return problems


def _check_precision(c: dict, res) -> list[str]:
    """`precision` must be declared, and must describe what the gold SQL actually returns."""
    cid = c["id"]
    if "precision" not in c:
        return [f"{cid}: missing precision (int decimals, or null for unrounded gold)"]
    p = c["precision"]
    if p is not None and not (isinstance(p, int) and 0 <= p <= 4):
        return [f"{cid}: precision must be null or an int 0-4"]
    out = []
    for j in range(len(res.columns)):
        for row in res.rows:
            v = row[j]
            if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)) or isinstance(v, int):
                continue
            scale = -v.as_tuple().exponent if isinstance(v, Decimal) else 99
            if p is not None and scale != p:
                out.append(f"{cid}: gold value {v} has {scale} decimals but precision is {p}")
                break
            if p is None and scale < 3:
                out.append(f"{cid}: precision is null (unrounded) but gold value {v} looks rounded")
                break
    lm = c.get("label_map")
    if lm is not None:
        flags = {str(row[j]).lower() for row in res.rows for j in range(len(res.columns))
                 if isinstance(row[j], bool)}
        if not flags or not set(lm) <= {"true", "false"}:
            out.append(f"{cid}: label_map needs a boolean gold column and keys 'true'/'false'")
    return out


def main() -> int:
    cases = load_gold()
    with psycopg.connect(settings.analyst_dsn) as conn:
        problems = validate(cases, conn)
    print(f"{len(cases)} gold items: {dict(Counter(c['category'] for c in cases))}")
    for p in problems:
        print("PROBLEM:", p)
    if not problems:
        print("gold set valid: every gold SQL runs and returns rows")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
