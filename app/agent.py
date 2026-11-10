"""The ask-your-data agent: question -> SQL -> guardrails -> read-only execution -> grounded summary.

Public contract (project 5 imports it): ask(question) -> dict with keys sql, columns, rows, summary,
refused, refused_reason, usage. It never raises.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal

from app import db, guardrails, llm, logging_, prompts, schema_docs
from app.config import settings

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1500
_FENCE_RE = re.compile(r"```(?:sql|postgresql|pgsql)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_MARKER_RE = re.compile(r"^\s*(REFUSE|UNANSWERABLE)\s*[:\-]\s*(.*)$", re.IGNORECASE | re.DOTALL)
_PLAN_RE = re.compile(r"^\s*PLAN\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SQL_START_RE = re.compile(
    r"^\s*(select\b.*|with\s+(?:recursive\s+)?\w+(?:\s*\([^)]*\))?\s+as\b.*)$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# --- parsing the model reply -------------------------------------------------------------------------


def parse_reply(text: str) -> dict:
    """Return {'kind': 'sql'|'refuse'|'unanswerable'|'none', 'sql', 'plan', 'message'}."""
    text = (text or "").strip()
    m = _MARKER_RE.match(text)
    if m:
        kind = m.group(1).lower()
        return {"kind": "refuse" if kind == "refuse" else "unanswerable", "sql": None, "plan": None,
                "message": m.group(2).strip().splitlines()[0] if m.group(2).strip() else ""}
    plan_m = _PLAN_RE.search(text)
    plan = plan_m.group(1).strip() if plan_m else None
    fence = _FENCE_RE.search(text)
    if fence:
        return {"kind": "sql", "sql": fence.group(1).strip(), "plan": plan, "message": ""}
    sm = _SQL_START_RE.search(text)
    if sm:  # an unfenced query: take from the first SELECT/WITH line to the end
        return {"kind": "sql", "sql": sm.group(1).strip().rstrip("`").strip(), "plan": plan, "message": ""}
    # a refusal that forgot its marker, after a plan line
    for kind, key in (("refuse", "REFUSE"), ("unanswerable", "UNANSWERABLE")):
        mm = re.search(rf"^\s*{key}\s*[:\-]\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
        if mm:
            return {"kind": kind, "sql": None, "plan": plan, "message": mm.group(1).strip()}
    return {"kind": "none", "sql": None, "plan": plan, "message": text[:200]}


# --- summary grounding check -------------------------------------------------------------------------

_DATE_LIKE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
_DATE_FULL = re.compile(r"\d{4}-\d{2}(?:-\d{2})?(?:[T ].*)?")


def _numeric_cells(rows) -> list[float]:
    """Absolute values of numeric cells: 'down 15.3%' matches a cell of -15.3."""
    out = []
    for r in rows:
        for v in r:
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float, Decimal)):
                out.append(abs(float(v)))
    return out


def check_summary(summary: str, question: str, columns: list[str], rows: list, row_count: int) -> list[str]:
    """Return the numbers in the summary that cannot be found in the rows (empty list = grounded).

    A number is grounded when its absolute value equals a numeric cell after rounding to the precision
    shown, is a digit run inside a text cell ('BN-001', 'FY2026-27'), is the row count, or appears in the
    question. Digit runs inside date cells do not count for 1-2 digit numbers (a month or day is not a
    quantity); only a 4-digit year, or a whole date or month that matches a date cell, is accepted.
    """
    nums = _numeric_cells(rows)
    date_strs: set[str] = set()
    text_cells: list[str] = []
    for r in rows:
        for v in r:
            if isinstance(v, (date, datetime)):
                date_strs.add(v.isoformat())
            elif isinstance(v, str):
                if _DATE_FULL.fullmatch(v):
                    date_strs.add(v)
                else:
                    text_cells.append(v)
    years = {s[:4] for s in date_strs}
    text_runs = {run for c in text_cells for run in re.findall(r"\d+", c)}
    context = question + " " + " ".join(columns) + " " + " ".join(text_cells)
    context_runs = set(re.findall(r"\d+", question)) | set(re.findall(r"\d+", " ".join(columns)))

    unmatched: list[str] = []
    for m in _DATE_LIKE.finditer(summary):
        d = m.group()
        if not (any(s.startswith(d) for s in date_strs) or d in context):
            unmatched.append(d)
    rest = _DATE_LIKE.sub(" ", summary)
    for raw in _NUM_RE.findall(rest):
        tok = raw.rstrip(",").replace(",", "")
        if not tok:
            continue
        if tok in text_runs or tok in context_runs or tok == str(row_count) or tok == str(len(rows)):
            continue
        if len(tok) == 4 and tok in years:
            continue
        decimals = len(tok.split(".")[1]) if "." in tok else 0
        x = float(tok)
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if any(abs(x - v) <= tol for v in nums):
            continue
        unmatched.append(raw.rstrip(","))
    return unmatched + check_superlatives(summary, rows)


_SUPERLATIVE = re.compile(r"(?<!\bat )\b(lowest|least|fewest|smallest|minimum|highest|most|largest|biggest|maximum|peak)\b",
                          re.IGNORECASE)
_MIN_WORDS = {"lowest", "least", "fewest", "smallest", "minimum"}
_CLAUSE_SPLIT = re.compile(r"(?<=[.;!?])\s+|,\s+|;\s*|\s+(?:while|whereas|but|and)\s+", re.IGNORECASE)


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return None
    return float(v)


def _matches_shown(x: float, tok: str) -> bool:
    decimals = len(tok.split(".")[1]) if "." in tok else 0
    return abs(abs(x) - float(tok)) <= 0.5 * 10 ** (-decimals) + 1e-9


def check_superlatives(summary: str, rows: list) -> list[str]:
    """Return 'lowest: UPI'-style claims where the named row does not hold the column's min or max.

    Numbers alone can be grounded and the claim still wrong ('UPI has the lowest rate at 2.3%' when wallet
    is 1.7%). A clause is checked only when it is unambiguous: one superlative word, exactly one label that
    names a single row, and one numeric column (the one holding the clause's number, or the only numeric
    column). Anything less certain is left alone rather than guessed.
    """
    if len(rows) < 2:
        return []
    label_rows: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        for v in r:
            if isinstance(v, str) and v.strip() and not _DATE_FULL.fullmatch(v):
                label_rows.setdefault(v.strip(), []).append(i)
    width = len(rows[0])
    numeric_cols = [j for j in range(width)
                    if all(_num(r[j]) is not None or r[j] is None for r in rows)
                    and any(_num(r[j]) is not None for r in rows)]
    wrong: list[str] = []
    for clause in _CLAUSE_SPLIT.split(summary):
        words = {m.group(1).lower() for m in _SUPERLATIVE.finditer(clause)}
        if len(words) != 1:
            continue
        word = words.pop()
        named = [lab for lab in label_rows
                 if re.search(r"(?<!\w)" + re.escape(lab) + r"(?!\w)", clause, re.IGNORECASE)]
        if len(named) != 1 or len(label_rows[named[0]]) != 1:
            continue
        label, i = named[0], label_rows[named[0]][0]
        toks = [t.replace(",", "") for t in _NUM_RE.findall(_DATE_LIKE.sub(" ", clause))]
        toks = [t for t in toks if t]
        cols = numeric_cols
        if toks:
            cols = [j for j in numeric_cols
                    if _num(rows[i][j]) is not None and any(_matches_shown(_num(rows[i][j]), t) for t in toks)]
        if len(cols) != 1:
            continue
        values = [_num(r[cols[0]]) for r in rows if _num(r[cols[0]]) is not None]
        target = min(values) if word in _MIN_WORDS else max(values)
        if _num(rows[i][cols[0]]) != target:
            wrong.append(f"{word}: {label}")
    return wrong


# --- sample values -----------------------------------------------------------------------------------

_samples_cache: dict | None = None


def fetch_samples(docs: schema_docs.SchemaDocs, run=None, n: int = 8) -> dict:
    """A few live sample values per column flagged `sample: true` (top by frequency)."""
    run = run or db.run_query
    out: dict = {}
    for t in docs.tables.values():
        for c in t.columns:
            if not c.sample:
                continue
            sql = (f"SELECT {c.name} FROM {t.name} WHERE {c.name} IS NOT NULL "
                   f"GROUP BY {c.name} ORDER BY count(*) DESC, {c.name} LIMIT {n}")
            try:
                out[(t.name, c.name)] = [r[0] for r in run(sql).rows]
            except Exception:
                log.warning("could not sample %s.%s", t.name, c.name, exc_info=True)
    return out


def get_samples(docs: schema_docs.SchemaDocs) -> dict:
    global _samples_cache
    if _samples_cache is not None:
        return _samples_cache
    samples = fetch_samples(docs)
    wanted = sum(1 for t in docs.tables.values() for c in t.columns if c.sample)
    if len(samples) == wanted:  # do not cache a partial result from a database that was briefly down
        _samples_cache = samples
    return samples


# --- helpers -----------------------------------------------------------------------------------------


def json_cell(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and v.as_tuple().exponent >= 0 else float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _deterministic_summary(row_count: int) -> str:
    return "The query returned no rows for those filters." if row_count == 0 else ""


class _Run:
    """Per-request bookkeeping."""

    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.llm_calls = 0
        self.attempts: list[dict] = []
        self.guardrail_blocks = 0
        self.executed = False
        self.provider = ""
        self.model = ""

    def call(self, chat_fn, messages):
        resp = chat_fn(messages)
        self.llm_calls += 1
        self.prompt_tokens += resp.prompt_tokens
        self.completion_tokens += resp.completion_tokens
        self.provider, self.model = resp.provider, resp.model
        return resp


def _empty(question: str) -> dict:
    return {
        "question": question, "status": "error", "sql": None, "plan": None, "columns": [], "rows": [],
        "row_count": 0, "truncated": False, "summary": None, "refused": False, "refused_reason": None,
        "refusal_message": None, "refused_source": None, "error": None, "summary_error": None, "executed": False, "guardrail_blocks": 0, "attempts": [],
        "summary_checked": False, "summary_faithful": None, "summary_unmatched": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0, "llm_calls": 0},
    }


def _refusal(out: dict, reason: str, message: str, source: str) -> dict:
    """source: prefilter (question screen), model (REFUSE/UNANSWERABLE), parser (guardrails) or db (privileges)."""
    out.update(status="refused", refused=True, refused_reason=reason, refusal_message=message, summary=message,
               refused_source=source)
    return out


# agent: schema docs + samples + examples. ddl: raw DDL and today's date only (the standard comparison).
# floor: table names only. "baseline" is kept as an alias of floor.
VARIANTS = ("agent", "ddl", "floor")


def _build_messages(variant: str, question: str, samples: dict | None) -> list[dict]:
    docs = schema_docs.load()
    if variant == "ddl":
        system = prompts.build_ddl_prompt(schema_docs.ddl_for_allowed_tables(docs), docs.today)
    elif variant in ("floor", "baseline"):
        system = prompts.build_baseline_prompt(list(docs.tables))
    elif variant == "agent":
        system = prompts.build_system_prompt(docs, get_samples(docs) if samples is None else samples)
    else:
        raise ValueError(f"unknown variant {variant!r}; use one of {VARIANTS}")
    return [{"role": "system", "content": system}, {"role": "user", "content": question}]


def _ask(question: str, variant: str, chat_fn, run_fn, samples, run: _Run, out: dict) -> dict:
    if not question or not question.strip():
        out["error"] = "empty question"
        return out
    if len(question) > MAX_QUESTION_CHARS:
        out["error"] = f"question is longer than {MAX_QUESTION_CHARS} characters"
        return out

    screened = guardrails.screen_question(question)
    if screened:
        kind, message = screened
        return _refusal(out, "write_request" if kind == "write" else "pii_request", message, "prefilter")

    messages = _build_messages(variant, question, samples)
    corrections_left = settings.max_corrections
    problem = ""
    pii_hits = 0
    while True:
        resp = run.call(chat_fn, messages)
        reply = parse_reply(resp.content)
        out["plan"] = reply["plan"] or out["plan"]
        if reply["kind"] == "refuse":
            msg = reply["message"] or guardrails.WRITE_MESSAGE
            pii = re.search(r"personal|\bpii\b|e-?mail|phone|names?\b|address|contact|private", msg, re.IGNORECASE)
            return _refusal(out, "pii_request" if pii else "write_request", msg, "model")
        if reply["kind"] == "unanswerable":
            return _refusal(out, "unanswerable", reply["message"] or "The warehouse has no data for that.", "model")

        sql = reply["sql"]
        if reply["kind"] == "none":
            problem = "the reply had no SQL block, REFUSE or UNANSWERABLE line"
            run.attempts.append({"stage": "format", "problem": problem, "sql": None})
        else:
            try:
                validated = guardrails.validate_sql(sql, settings.max_rows)
            except guardrails.GuardrailError as exc:
                run.guardrail_blocks += 1
                run.attempts.append({"stage": "guardrail", "rule": exc.rule, "problem": str(exc), "sql": sql})
                out["sql"] = None
                if exc.kind == "unsafe":
                    return _refusal(out, "guardrail_blocked", f"The generated query was blocked: {exc}", "parser")
                if exc.rule == "pii_table":
                    pii_hits += 1
                    if pii_hits >= 2:  # asked twice for the PII table: treat it as a personal-data request
                        return _refusal(out, "pii_request", guardrails.PII_MESSAGE, "parser")
                problem = str(exc)
            else:
                try:
                    result = run_fn(validated.sql)
                except db.DBError as exc:
                    run.attempts.append({"stage": "db", "sqlstate": exc.sqlstate, "problem": exc.message,
                                         "sql": validated.sql})
                    if exc.is_permission:
                        run.guardrail_blocks += 1
                        return _refusal(out, "permission_denied",
                                        f"The database refused that query: {exc.message}", "db")
                    problem = exc.message
                    out["sql"] = validated.sql
                else:
                    run.executed = True
                    out["sql"] = validated.sql
                    return _finish(question, result, out, chat_fn, run)
        if corrections_left <= 0:
            if out["sql"] is None and run.attempts and run.attempts[-1]["stage"] == "guardrail":
                return _refusal(out, "guardrail_blocked", f"The generated query was blocked: {problem}", "parser")
            out["error"] = problem
            return out
        corrections_left -= 1
        messages = [*messages, {"role": "assistant", "content": resp.content}, prompts.correction_message(problem)]


def _finish(question: str, result: db.QueryResult, out: dict, chat_fn, run: _Run) -> dict:
    columns = list(result.columns)
    rows = [[json_cell(v) for v in r] for r in result.rows]
    out.update(status="answered", columns=columns, rows=rows[: settings.show_rows], row_count=len(rows),
               truncated=result.truncated, executed=True)
    if not rows:
        out["summary"] = _deterministic_summary(0)
        out.update(summary_checked=True, summary_faithful=True)
        return out
    try:
        resp = run.call(chat_fn, prompts.build_summary_messages(question, columns, rows, len(rows)))
        summary = resp.content.strip()
        unmatched = check_summary(summary, question, columns, rows, len(rows))
    except Exception as exc:  # noqa: BLE001 - the rows are the answer; a summary failure is recorded, never fatal
        log.warning("summary step failed: %s", exc)
        out.update(summary=None, summary_checked=False, summary_faithful=None, summary_error=str(exc))
        return out
    out["summary_checked"] = True
    if summary and not unmatched:
        out.update(summary=summary, summary_faithful=True)
    else:
        log.warning("summary dropped: numbers not in rows: %s", unmatched)
        out.update(summary=None, summary_faithful=False, summary_unmatched=unmatched)
    return out


def ask(question: str, *, variant: str = "agent", chat_fn=None, run_fn=None, samples: dict | None = None,
        log_requests: bool = True, log_path=None) -> dict:
    """Answer one question. variant: 'agent' (schema docs), 'ddl' (raw DDL + date) or 'floor' (table names only).

    Never raises.
    """
    start = time.monotonic()
    out = _empty(question)
    run = _Run()
    try:
        _ask(question, variant, chat_fn or llm.chat, run_fn or db.run_query, samples, run, out)
    except llm.LLMError as exc:
        out.update(status="error", error=f"model unavailable: {exc}")
    except Exception as exc:
        log.exception("ask failed")
        out.update(status="error", error=f"internal error: {exc!r}")
    out["executed"] = run.executed
    out["guardrail_blocks"] = run.guardrail_blocks
    out["attempts"] = run.attempts
    out["variant"] = variant
    out["usage"] = {
        "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens,
        "latency_ms": int((time.monotonic() - start) * 1000), "llm_calls": run.llm_calls,
    }
    out["model"] = run.model or settings.ollama_model
    if log_requests:
        logging_.log_request({
            "question": question, "variant": variant, "status": out["status"], "sql": out["sql"],
            "rows_returned": out["row_count"], "latency_ms": out["usage"]["latency_ms"],
            "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens,
            "refused_reason": out["refused_reason"], "guardrail_blocks": run.guardrail_blocks,
            "attempts": len(run.attempts), "summary_dropped": out["summary_faithful"] is False,
            "summary_unmatched": out["summary_unmatched"], "error": out["error"],
            "summary_error": out["summary_error"], "refused_source": out["refused_source"],
        }, log_path)
    return out
