"""SQL guardrails. Defence in depth: the database role is the control that must hold.

validate_sql() lexes the statement the way Postgres would (strings, quoted identifiers, nested comments),
strips comments, and refuses anything that is not one read-only SELECT/WITH over allowlisted tables and
functions. The cleaned SQL it returns (with LIMIT added when absent) is what gets executed.

screen_question() is a cheap pre-LLM filter for blatant write and personal-data requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_LIMIT = 200
MAX_SQL_CHARS = 8000
MAX_DEPTH = 12

# Tables the agent may read. Kept in sync with warehouse/schema_docs.yaml by a test.
ALLOWED_TABLES = frozenset({
    "dim_date", "dim_product", "dim_customer_safe", "fact_orders", "fact_order_items", "fact_returns",
    "fact_tickets",
})
PII_TABLES = frozenset({"dim_customer"})

DENY_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "upsert", "drop", "alter", "create", "truncate", "grant", "revoke",
    "copy", "call", "do", "execute", "prepare", "deallocate", "set", "reset", "show", "begin", "start", "commit",
    "rollback", "savepoint", "release", "lock", "vacuum", "analyze", "analyse", "cluster", "reindex", "refresh",
    "listen", "notify", "unlisten", "discard", "load", "explain", "comment", "security", "import", "into",
    "declare", "abort", "table", "checkpoint", "reassign", "returning", "regclass", "regproc", "regprocedure",
    "regoper", "regoperator", "regtype", "regrole", "regnamespace", "regconfig", "regdictionary",
})

# Functions that read files, run other queries, change settings or leak server state. Never retried.
DANGEROUS_PREFIXES = (
    "pg_", "lo_", "dblink", "set_config", "current_setting", "nextval", "setval", "currval", "lastval",
    "txid", "query_to_xml", "table_to_xml", "schema_to_xml", "database_to_xml", "cursor_to_xml", "xmlparse",
    "xpath", "version", "current_database", "inet_", "has_", "set_", "update", "pgp_", "crypt",
)

ALLOWED_FUNCTIONS = frozenset({
    # aggregates and windows
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop",
    "var_samp", "corr", "covar_pop", "regr_slope", "bool_and", "bool_or", "every", "string_agg", "array_agg",
    "percentile_cont", "percentile_disc", "mode", "row_number", "rank", "dense_rank", "percent_rank",
    "cume_dist", "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
    # numeric
    "round", "ceil", "ceiling", "floor", "trunc", "abs", "sign", "mod", "power", "sqrt", "ln", "log", "exp",
    "greatest", "least", "width_bucket",
    # conditional
    "coalesce", "nullif",
    # text
    "lower", "upper", "initcap", "length", "char_length", "trim", "ltrim", "rtrim", "btrim", "substr",
    "substring", "left", "right", "replace", "split_part", "position", "strpos", "concat", "concat_ws",
    "reverse", "to_char", "to_date", "to_number",
    # dates
    "date_trunc", "date_part", "extract", "make_date", "date", "interval",
    # series and arrays
    "generate_series", "unnest", "array_length", "cardinality",
    # casts / type names that take parameters
    "cast", "numeric", "decimal", "varchar", "char", "character", "float", "int", "integer", "bigint",
    "smallint", "real", "text", "timestamp", "timestamptz", "time", "boolean", "bool",
})

# Wall-clock readings: the warehouse is a fixed snapshot, so questions must resolve to literal dates.
WALL_CLOCK = frozenset({
    "current_date", "current_time", "current_timestamp", "localtime", "localtimestamp", "now",
    "statement_timestamp", "clock_timestamp", "transaction_timestamp", "timeofday", "age",
})
WALL_CLOCK_MESSAGE = (
    "{w} reads the wall clock, which is not the warehouse date; use literal dates from the resolved window "
    "(see the conventions)"
)

# Words that may legitimately be followed by "(" without being a function call.
PAREN_KEYWORDS = frozenset({
    "select", "from", "join", "on", "using", "where", "and", "or", "not", "in", "exists", "any", "all", "some",
    "values", "as", "with", "recursive", "union", "intersect", "except", "when", "then", "else", "case", "by",
    "having", "over", "partition", "order", "group", "filter", "within", "distinct", "lateral", "between",
    "like", "ilike", "similar", "to", "is", "limit", "offset", "fetch", "materialized", "array", "row",
    "rows", "range", "unbounded", "preceding", "following", "current", "escape", "end", "cross", "inner",
    "left", "right", "full", "outer", "natural", "only", "asc", "desc", "nulls", "first", "last", "at", "zone",
})
# FROM inside these functions is part of the function syntax, not a FROM clause.
FROM_FUNCTIONS = frozenset({"extract", "substring", "substr", "trim", "overlay", "position", "btrim"})
CLAUSE_ENDERS = frozenset({
    "where", "group", "having", "order", "limit", "offset", "fetch", "union", "intersect", "except", "window",
    "for",
})


class GuardrailError(ValueError):
    """The SQL was refused. kind: 'unsafe' (an attack signature: never retry) or 'fixable' (one retry).

    rule names the check; 'pii_table' is a dim_customer reference, which the agent retries once.
    """

    def __init__(self, message: str, kind: str = "fixable", rule: str = ""):
        super().__init__(message)
        self.kind = kind
        self.rule = rule or kind


@dataclass
class Tok:
    kind: str  # word | qident | string | number | op | punct
    text: str
    start: int
    end: int


@dataclass
class Validated:
    sql: str
    limit_added: bool
    tables: set[str] = field(default_factory=set)
    comments_stripped: bool = False


_WORD_RE = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
_NUM_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_DOLLAR_RE = re.compile(r"\$[A-Za-z_\u0080-\U0010ffff]*\$")
_OP_CHARS = set("+-*/<>=~!@#%^&|`?:")


def _lex(sql: str) -> tuple[list[Tok], str, bool]:
    """Tokenise. Returns (tokens without comments, sql with comments replaced by a space, had_comments)."""
    toks: list[Tok] = []
    clean: list[str] = []
    had_comment = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "\x00" or (ord(ch) < 32 and ch not in "\t\n\r\f"):
            raise GuardrailError("control characters are not allowed", "unsafe", "control_char")
        if ch in " \t\n\r\f":
            clean.append(ch)
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            clean.append(" ")
            had_comment = True
            continue
        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise GuardrailError("unterminated comment", "fixable", "unterminated_comment")
            i = j
            clean.append(" ")
            had_comment = True
            continue
        if ch == "'":
            prev = sql[i - 1] if i else ""
            before = sql[i - 2] if i > 1 else ""
            if prev in "eE" and not (before.isalnum() or before in "_$"):
                raise GuardrailError("escape string syntax (E'...') is not allowed", "unsafe", "escape_string")
            if prev == "&" and before in "uU":
                raise GuardrailError("unicode escape strings are not allowed", "unsafe", "escape_string")
            j = i + 1
            while True:
                if j >= n:
                    raise GuardrailError("unterminated string literal", "fixable", "unterminated_string")
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("string", sql[i + 1 : j], i, j + 1))
            clean.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == '"':
            prev = sql[i - 1] if i else ""
            before = sql[i - 2] if i > 1 else ""
            if prev == "&" and before in "uU":
                raise GuardrailError("unicode escape identifiers are not allowed", "unsafe", "escape_string")
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise GuardrailError("unterminated quoted identifier", "fixable", "unterminated_ident")
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            if not buf:
                raise GuardrailError("empty quoted identifier", "fixable", "syntax")
            toks.append(Tok("qident", "".join(buf), i, j + 1))
            clean.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == "$":
            if _DOLLAR_RE.match(sql, i):
                raise GuardrailError("dollar-quoted strings are not allowed", "unsafe", "dollar_quote")
            raise GuardrailError("query parameters ($1) are not allowed", "unsafe", "param")
        if ch == "\\":
            raise GuardrailError("backslashes are not allowed outside string literals", "unsafe", "backslash")
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _NUM_RE.match(sql, i)
            toks.append(Tok("number", m.group(), i, m.end()))
            clean.append(m.group())
            i = m.end()
            continue
        m = _WORD_RE.match(sql, i)
        if m:
            toks.append(Tok("word", m.group().lower(), i, m.end()))
            clean.append(m.group())
            i = m.end()
            continue
        if ch in "(),;.[]":
            toks.append(Tok("punct", ch, i, i + 1))
            clean.append(ch)
            i += 1
            continue
        if ch in _OP_CHARS:
            j = i
            while j < n and sql[j] in _OP_CHARS and not sql.startswith(("--", "/*"), j):
                j += 1
            if j == i:
                j = i + 1
            toks.append(Tok("op", sql[i:j], i, j))
            clean.append(sql[i:j])
            i = j
            continue
        raise GuardrailError(f"unexpected character {ch!r}", "fixable", "syntax")
    return toks, "".join(clean), had_comment


def _is(tok: Tok | None, text: str) -> bool:
    return tok is not None and tok.kind in ("punct", "word") and tok.text == text


def _cte_defs(toks: list[Tok]) -> tuple[set[str], set[int]]:
    """CTE names introduced by WITH ... name [(cols)] AS [NOT MATERIALIZED] ( ... ), and the token index of
    each definition. Only a definition is exempt from the function check, never a later call of that name."""
    names: set[str] = set()
    def_idx: set[int] = set()
    for i, t in enumerate(toks):
        if not _is(t, "as"):
            continue
        k = i + 1
        if k < len(toks) and toks[k].text == "not":
            k += 1
        if k < len(toks) and toks[k].text == "materialized":
            k += 1
        if not (k < len(toks) and _is(toks[k], "(")):
            continue
        j = i - 1
        if j >= 0 and _is(toks[j], ")"):  # column list: name (a, b) AS (
            depth = 0
            while j >= 0:
                if _is(toks[j], ")"):
                    depth += 1
                elif _is(toks[j], "("):
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            j -= 1
        if j >= 1 and toks[j].kind in ("word", "qident") and (
            _is(toks[j - 1], "with") or _is(toks[j - 1], "recursive") or _is(toks[j - 1], ",")
        ):
            names.add(toks[j].text)
            def_idx.add(j)
    return names, def_idx


def _check_relation(parts: list[str], ctes: set[str], tables: set[str]) -> None:
    if len(parts) == 1:
        name = parts[0]
        if name.startswith("pg_"):
            raise GuardrailError("system catalogs are not allowed", "unsafe", "system_catalog")
        if name in PII_TABLES:
            raise GuardrailError(
                "dim_customer holds personal data and is not available; use dim_customer_safe for analytic questions "
                "(and reply REFUSE if the user asked for names, emails or phones)",
                "fixable",
                "pii_table",
            )
        if name in ALLOWED_TABLES:
            tables.add(name)
            return
        if name in ctes:
            return
        raise GuardrailError(
            f"table '{name}' is not in the allowlist ({', '.join(sorted(ALLOWED_TABLES))})",
            "fixable",
            "table_not_allowed",
        )
    if len(parts) == 2 and parts[0] == "public":
        _check_relation(parts[1:], set(), tables)
        return
    schema = parts[-2] if len(parts) >= 2 else ""
    if schema.startswith("pg_") or schema == "information_schema":
        raise GuardrailError("system catalogs are not allowed", "unsafe", "system_catalog")
    raise GuardrailError(f"schema-qualified table '{'.'.join(parts)}' is not allowed", "fixable", "table_not_allowed")


def _check_function(name: str, qualified: bool, alias_list: bool = False) -> None:
    if qualified:
        raise GuardrailError(f"schema-qualified function '{name}' is not allowed", "unsafe", "function")
    if name.startswith(DANGEROUS_PREFIXES) or name in DENY_KEYWORDS:
        raise GuardrailError(f"function '{name}' is not allowed", "unsafe", "forbidden_function")
    if name in WALL_CLOCK:
        raise GuardrailError(WALL_CLOCK_MESSAGE.format(w=name.upper()), "fixable", "wall_clock")
    if alias_list:  # AS name(col, col): an alias with a column list, not a call
        return
    if name not in ALLOWED_FUNCTIONS:
        raise GuardrailError(
            f"function '{name}' is not on the allowed list; use standard aggregates, date and text functions",
            "fixable",
            "function_not_allowed",
        )


def _scan(toks: list[Tok], ctes: set[str], cte_defs: set[int]) -> tuple[set[str], bool]:
    """Walk the tokens once: keywords, relation names after FROM/JOIN, function calls, top-level LIMIT."""
    tables: set[str] = set()
    depth = 0
    from_active = [False]
    openers: list[str | None] = [None]
    expect_rel = False
    has_limit = False
    n = len(toks)
    for i, t in enumerate(toks):
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < n else None
        if t.kind == "punct" and t.text in "([":
            depth += 1
            if depth > MAX_DEPTH:
                raise GuardrailError("query is nested too deeply", "fixable", "depth")
            opener = prev.text if prev is not None and prev.kind == "word" else None
            openers.append(opener)
            from_active.append(False)
            continue
        if t.kind == "punct" and t.text in ")]":
            depth -= 1
            if depth < 0:
                raise GuardrailError("unbalanced parentheses", "fixable", "syntax")
            openers.pop()
            from_active.pop()
            continue
        if t.kind == "punct" and t.text == ",":
            if from_active[depth]:
                expect_rel = True
            continue
        if t.kind in ("word", "qident") and t.text.startswith("pg_"):
            raise GuardrailError("system catalogs and pg_ functions are not allowed", "unsafe", "system_catalog")
        if t.kind == "qident" and nxt is not None and _is(nxt, "("):
            raise GuardrailError("quoted function names are not allowed", "unsafe", "function")
        if t.kind == "word":
            w = t.text
            if w in DENY_KEYWORDS:
                raise GuardrailError(
                    f"'{w.upper()}' is a reserved word that is not allowed here: only read-only SELECT queries "
                    "run, and it cannot be used as an alias or column name either; rename the alias",
                    "fixable",
                    "keyword",
                )
            if w in WALL_CLOCK:
                raise GuardrailError(WALL_CLOCK_MESSAGE.format(w=w.upper()), "fixable", "wall_clock")
            if w == "for" and nxt is not None and nxt.text in ("update", "share", "no", "key"):
                raise GuardrailError("row locking (FOR UPDATE/SHARE) is not allowed", "fixable", "keyword")
            if depth == 0 and w == "limit":
                has_limit = True
            if depth == 0 and w == "fetch" and nxt is not None and nxt.text in ("first", "next"):
                has_limit = True
            if w == "from" and not (prev is not None and prev.text == "distinct") \
                    and openers[depth] not in FROM_FUNCTIONS:
                from_active[depth] = True
                expect_rel = True
                continue
            if w == "join":
                expect_rel = True
                continue
            if w in CLAUSE_ENDERS:
                from_active[depth] = False
                expect_rel = False
            if expect_rel and w in ("select", "with", "values"):
                expect_rel = False
            elif expect_rel and w == "lateral":
                continue
            elif expect_rel and w == "only":
                raise GuardrailError("FROM ONLY is not allowed", "fixable", "syntax")
        if expect_rel and t.kind in ("word", "qident"):
            if nxt is not None and _is(nxt, "("):  # table function in FROM
                expect_rel = False
                _check_function(t.text, qualified=_is(prev, "."))
                continue
            parts = [t.text]
            j = i
            while j + 2 < n and _is(toks[j + 1], ".") and toks[j + 2].kind in ("word", "qident"):
                parts.append(toks[j + 2].text)
                j += 2
            # relation names after a dot are consumed here; mark them so the loop does not re-check them
            _check_relation(parts, ctes, tables)
            expect_rel = False
            continue
        if t.kind == "word" and nxt is not None and _is(nxt, "("):
            w = t.text
            if w in PAREN_KEYWORDS or i in cte_defs:
                continue
            _check_function(w, qualified=_is(prev, "."), alias_list=_is(prev, "as"))
    if depth != 0:
        raise GuardrailError("unbalanced parentheses", "fixable", "syntax")
    return tables, has_limit


def validate_sql(sql: str, limit: int = DEFAULT_LIMIT) -> Validated:
    """Validate one statement and return the SQL to run (comments stripped, LIMIT added when absent)."""
    if not isinstance(sql, str) or not sql.strip():
        raise GuardrailError("empty SQL", "fixable", "empty")
    if len(sql) > MAX_SQL_CHARS:
        raise GuardrailError("SQL is too long", "fixable", "too_long")
    toks, clean, had_comment = _lex(sql)
    if not toks:
        raise GuardrailError("empty SQL", "fixable", "empty")

    semis = [t for t in toks if t.kind == "punct" and t.text == ";"]
    if semis:
        if len(semis) > 1 or toks[-1] is not semis[0]:
            raise GuardrailError("only a single statement is allowed (no semicolons)", "unsafe", "multi_statement")
        toks = toks[:-1]
        clean = clean[: clean.rindex(";")]
        if not toks:
            raise GuardrailError("empty SQL", "fixable", "empty")
    clean = clean.strip()

    first = toks[0]
    if first.kind != "word" or first.text not in ("select", "with"):
        if first.kind == "word" and first.text in DENY_KEYWORDS:
            raise GuardrailError(f"'{first.text.upper()}' is not allowed: read-only SELECT queries only", "unsafe", "keyword")
        raise GuardrailError("the statement must start with SELECT or WITH", "fixable", "not_select")

    ctes, cte_defs = _cte_defs(toks)
    for name in ctes:
        if name in PII_TABLES or name.startswith(DANGEROUS_PREFIXES) or name in DENY_KEYWORDS:
            raise GuardrailError(f"CTE name '{name}' is not allowed", "unsafe", "cte_shadow")
    tables, has_limit = _scan(toks, ctes, cte_defs)
    if not has_limit:
        clean = f"{clean} LIMIT {limit}"
    return Validated(sql=clean, limit_added=not has_limit, tables=tables, comments_stripped=had_comment)


# --- question screening ------------------------------------------------------------------------------

# Imperative openers: "delete ...", "please drop ...", "bhai delete ...", also after a full stop.
_LEAD = r"(?:^|[.!?;]\s*)\W*(?:(?:please|pls|can you|could you|would you|kindly|bhai|yaar|hey|ok|now|just)\s+)*"
_NEG = r"(?!.{0,15}\b(?:mat|nahi|nahin|don'?t|do not|never|not)\b)"  # "delete karo mat" = do not delete
_WRITE_OBJECT = (
    r".{0,60}\b(?:table|rows?|records?|orders?|customers?|tickets?|returns?|database|data|history|products?|"
    r"items?|coupons?|entries)\b"
)
_WRITE_PATTERNS = [
    re.compile(
        _LEAD + r"(?:delete\s+from|drop\s+(?:table|database|schema|view|index)|truncate\b|"
        r"insert\s+into|update\s+\w+\s+set|alter\s+(?:table|role|user)|"
        r"create\s+(?:table|view|index|role|user)|grant\s+\w+|revoke\s+\w+)",
        re.IGNORECASE,
    ),
    re.compile(_LEAD + r"(?:delete|drop|truncate|wipe|erase|purge)\b" + _NEG + _WRITE_OBJECT, re.IGNORECASE),
    re.compile(
        _LEAD + r"update\b" + _NEG + r".{0,40}\b(?:price|prices|status|stock|cost|orders?|customers?|"
        r"products?|records?|rows?|table)\b.{0,40}\b(?:to|=|as|set)\b",
        re.IGNORECASE,
    ),
    re.compile(
        _LEAD + r"insert\b" + _NEG + r".{0,40}\b(?:into|row|record|new (?:order|customer|product))\b",
        re.IGNORECASE,
    ),
    re.compile(
        _LEAD + r"(?:change|set|edit|modify|raise|lower)\b" + _NEG
        + r".{0,30}\b(?:price|status|stock|cost)\b.{0,40}\b(?:to|=)\s*\d",
        re.IGNORECASE,
    ),
    re.compile(_LEAD + r"mark\b.{0,40}\b(?:as\s+)?(?:resolved|closed|cancelled|refunded)\b", re.IGNORECASE),
    re.compile(r"\b(?:delete|remove|hata|mita)\s+kar\s*(?:do|dena|de)\b", re.IGNORECASE),
]
_EMAIL = r"e-?mails?(?!\s*[_-]?\s*campaign)"
_PEOPLE = r"(?:customers?|buyers?|clients?|users?|people|persons?|shoppers?|contacts?)"
_PII_PATTERNS = [
    re.compile(r"\b(?:e-?mail|mail)\s*(?:address(?:es)?|ids?|id's)\b", re.IGNORECASE),
    re.compile(r"\b" + _PEOPLE + r"'?s?\W+(?:\w+\W+){0,3}?" + _EMAIL + r"\b", re.IGNORECASE),
    re.compile(
        r"\b" + _EMAIL + r"\W+(?:\w+\W+){0,3}?(?:of|for|from|belonging to)\W+(?:\w+\W+){0,3}?" + _PEOPLE + r"\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:list|export|share|send|give|get)\s+(?:me\s+)?(?:all\s+|the\s+|our\s+)?" + _EMAIL + r"\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:phone|mobile|cell|whatsapp|contact)\s*(?:numbers?|nos?\b|details|info)", re.IGNORECASE),
    re.compile(r"\b(?:customer|buyer|client)'?s?\s+(?:full\s+)?names?\b", re.IGNORECASE),
    re.compile(
        r"\bnames?\s+(?:and|or|of|for)\s+(?:the\s+|our\s+|all\s+|top\s+\d+\s+|\d+\s+)?(?:\w+\s+){0,2}"
        r"(?:customers?|buyers?|clients?|people|users)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:home|street|postal|shipping|billing|full|customer)\s+address(?:es)?\b", re.IGNORECASE),
    re.compile(r"\bcontact\s+(?:list|details|info)\b", re.IGNORECASE),
    re.compile(r"\bdim_customer(?!_safe)\b", re.IGNORECASE),
]

WRITE_MESSAGE = "I can only read the warehouse. I can't change, add or delete data."
PII_MESSAGE = (
    "I can't share customer names, emails, phone numbers or other personal details. "
    "I can answer with counts and aggregates by city, state or signup channel instead."
)


def screen_question(question: str) -> tuple[str, str] | None:
    """Return (kind, message) for blatant write or personal-data requests, else None."""
    q = question or ""
    for pat in _WRITE_PATTERNS:
        if pat.search(q):
            return "write", WRITE_MESSAGE
    for pat in _PII_PATTERNS:
        if pat.search(q):
            return "pii", PII_MESSAGE
    return None


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")


def mask_pii(text: str) -> str:
    """Mask emails and phone-like numbers (for the request log)."""
    return _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", text))
