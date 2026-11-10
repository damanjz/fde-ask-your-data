"""Prompt building, gold-set hygiene and the HTTP API (all without a database or an LLM)."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, prompts, schema_docs
from evals.run import build_report, load_gold, percentile, summarise

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = {("fact_orders", "city"): ["Bengaluru", "Mumbai"], ("dim_date", "festival"): ["Diwali Run-up"]}


def test_system_prompt_contains_the_schema_conventions_and_samples():
    docs = schema_docs.load()
    p = prompts.build_system_prompt(docs, SAMPLES)
    for table in docs.tables.values():
        assert f"TABLE {table.name}" in p
        for col in table.columns:
            assert f"  {col.name} " in p
    assert "dim_customer_safe" in p and "TABLE dim_customer " not in p
    assert "Today is 2026-11-10" in p and "fiscal quarter" in p.lower() and "FY2026-27" in p
    assert "Examples: Bengaluru, Mumbai." in p and "Examples: Diwali Run-up." in p
    assert "Values: standard, express, in_store." in p
    assert "REFUSE:" in p and "UNANSWERABLE:" in p and "```sql" in p
    assert "Hinglish" in p


def test_system_prompt_is_one_message_and_stays_small():
    p = prompts.build_system_prompt(schema_docs.load(), SAMPLES)
    assert len(p) < 13000  # keeps the whole request inside an 8k-token context


def test_baseline_prompt_has_table_names_only():
    docs = schema_docs.load()
    p = prompts.build_baseline_prompt(list(docs.tables))
    for name in docs.tables:
        assert name in p
    columns = {c.name for t in docs.tables.values() for c in t.columns}
    leaked = {c for c in columns if re.search(rf"\b{re.escape(c)}\b", p) and c not in ("category",)}
    assert not leaked, leaked
    assert "Conventions" not in p and "Examples" not in p and "2026" not in p
    assert "REFUSE:" in p  # same output format as the agent so the comparison isolates the schema docs


def test_summary_prompt_carries_the_rows_and_only_one_system_message():
    msgs = prompts.build_summary_messages("q?", ["a", "b"], [[1, None], [2, "x"]], 2)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "a | b" in msgs[1]["content"] and "NULL" in msgs[1]["content"] and "(2 total)" in msgs[1]["content"]


def test_correction_message_is_a_user_turn():
    m = prompts.correction_message("column x does not exist")
    assert m["role"] == "user" and "column x does not exist" in m["content"]


# --- gold hygiene ------------------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def test_gold_shape():
    cases = load_gold()
    assert len(cases) == 40
    assert {c["category"] for c in cases} == {"simple", "complex", "refuse"}
    assert [sum(c["category"] == k for c in cases) for k in ("simple", "complex", "refuse")] == [25, 10, 5]
    assert len({c["id"] for c in cases}) == 40
    for c in cases:
        assert c["notes"]
        if c["category"] == "refuse":
            assert c["sql"] is None and c["refuse_kind"] in ("dml", "pii", "unanswerable")
        else:
            assert c["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
            assert "precision" in c and (c["precision"] is None or c["precision"] in (0, 1, 2))
    assert [c["refuse_kind"] for c in cases if c["category"] == "refuse"].count("dml") == 2


def test_gold_questions_are_never_few_shot_examples():
    gold_q = {_norm(c["question"]) for c in load_gold()}
    gold_sql = {_norm(c["sql"]) for c in load_gold() if c["sql"]}
    for ex in schema_docs.load().examples:
        assert _norm(ex["question"]) not in gold_q
        assert _norm(ex["sql"]) not in gold_sql


def test_gold_questions_do_not_appear_in_the_prompt_text():
    p = prompts.build_system_prompt(schema_docs.load(), SAMPLES).lower()
    for c in load_gold():
        assert c["question"].lower() not in p


def test_top_n_gold_items_have_a_tie_breaker():
    for c in load_gold():
        if c["sql"] and re.search(r"\blimit\s+\d+\s*$", c["sql"], re.IGNORECASE) and re.search(r"order by", c["sql"], re.IGNORECASE):
            order_by = re.split(r"order by", c["sql"], flags=re.IGNORECASE)[-1]
            assert "," in order_by.split("LIMIT")[0], c["id"]
            assert c["ordered"], c["id"]


def test_the_app_never_reads_the_evals_directory():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "evals" not in text, path
        assert "gold.jsonl" not in text, path


def test_gold_hinglish_and_vague_time_words_are_present():
    qs = " ".join(c["question"].lower() for c in load_gold())
    for token in ("last month", "last quarter", "this year", "kitne", "pichle", "bhai"):
        assert token in qs, token


# --- report ------------------------------------------------------------------------------------------


def _row(i, cat, passed, **res):
    base = {"status": "answered", "refused": False, "executed": True, "guardrail_blocks": 0, "attempts": [],
            "rows": [[1]], "summary_faithful": True, "sql": "SELECT 1",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "latency_ms": 1000 * i}}
    base.update(res)
    return {"id": f"G{i:02d}", "category": cat, "question": f"q{i}", "result": base,
            "score": {"passed": passed, "checks": {}, "reasons": [] if passed else ["rows differ"]}}


def test_percentile_and_summary():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([], 0.5) == 0
    assert percentile([10, 20], 0.95) == 19.5
    rows = [_row(1, "simple", True), _row(2, "simple", False), _row(3, "refuse", True, refused=True,
                                                                         executed=False, rows=[])]
    s = summarise(rows)
    assert s["by_category"] == {"simple": (1, 2), "refuse": (1, 1)} and s["passed"] == 2
    assert s["p50_ms"] == 2000 and s["tokens_per_q"] == 110 and (s["faithful"], s["faithful_of"]) == (2, 2)


def test_report_has_the_required_sections_and_flags_mock_runs():
    cases = [{"id": f"G{i:02d}", "category": c, "question": f"q{i}", "sql": "SELECT 1"}
             for i, c in ((1, "simple"), (2, "simple"), (3, "refuse"))]
    results = {"agent": [_row(1, "simple", True), _row(2, "simple", False),
                         _row(3, "refuse", True, refused=True, refused_source="model", executed=False, rows=[])],
               "ddl": [_row(1, "simple", True), _row(2, "simple", False),
                       _row(3, "refuse", True, refused=True, refused_source="prefilter", executed=False, rows=[])],
               "floor": [_row(1, "simple", False), _row(2, "simple", False),
                         _row(3, "refuse", False, refused=False)]}
    text = build_report(results, cases, {"date": "2026-11-10", "model": "m", "mock": False})
    for needle in ("Execution accuracy by category", "| agent | ddl | floor |", "Guardrail blocks", "p50 / p95",
                   "Tokens per question", "Summary faithfulness", "Failed questions: agent (1)",
                   "Failed questions: floor (3)", "95% CI", "Declined by the model", "Declined by the SQL parser",
                   "Declined by the question pre-filter", "Declined by the database", "Summary call errors",
                   "G26", "G28", "Gold SQL:", "rows differ"):
        assert needle in text, needle
    assert "MOCK RUN" not in text
    assert "MOCK RUN" in build_report(results, cases, {"date": "d", "model": "oracle", "mock": True})


# --- API ---------------------------------------------------------------------------------------------


def test_ask_endpoint(monkeypatch):
    fake = {k: None for k in main.PUBLIC_KEYS}
    fake.update(question="q", status="answered", rows=[[1]], columns=["n"], sql="SELECT 1", refused=False,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "latency_ms": 5}, secret_internal="x")
    monkeypatch.setattr(main.agent, "ask", lambda q: fake)
    client = TestClient(main.app)
    r = client.post("/ask", json={"question": "How many orders?"})
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == "SELECT 1" and body["rows"] == [[1]] and "secret_internal" not in body
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert client.post("/ask", json={}).status_code == 422


def test_schema_endpoint_and_static_page():
    client = TestClient(main.app)
    body = client.get("/schema").json()
    assert "dim_customer_safe" in body["tables"] and "dim_customer" not in body["tables"]
    page = client.get("/")
    assert page.status_code == 200 and "ask your data" in page.text
    assert "http://" not in page.text and "https://" not in page.text  # no external CDN


# --- review 2026-11-10 -------------------------------------------------------------------------------


def test_wilson_interval():
    from evals.run import wilson

    lo, hi = wilson(34, 40)
    assert 0.70 < lo < 0.72 and 0.92 < hi < 0.94
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(40, 40)
    assert hi == 1.0 and 0.90 < lo < 0.92
    lo, hi = wilson(0, 40)
    assert lo == 0.0 and 0.0 < hi < 0.10


def test_every_accuracy_cell_carries_a_confidence_interval():
    cases = [{"id": "G01", "category": "simple", "question": "q", "sql": "SELECT 1"}]
    text = build_report({"agent": [_row(1, "simple", True)]}, cases, {"date": "d", "model": "m", "mock": False})
    assert "| simple | 1 | 1/1 (100%, 95% CI " in text and "**1/1 (100%, 95% CI " in text


def test_refusal_sources_are_counted_in_the_summary():
    rows = [_row(1, "refuse", True, refused=True, refused_source="model", executed=False, rows=[]),
            _row(2, "refuse", True, refused=True, refused_source="prefilter", executed=False, rows=[]),
            _row(3, "refuse", True, refused=True, refused_source="prefilter", executed=False, rows=[]),
            _row(4, "simple", True, summary_error="timeout")]
    s = summarise(rows)
    assert s["refusal_sources"] == {"prefilter": 2, "model": 1, "parser": 0, "db": 0}
    assert s["summary_errors"] == 1


def test_gold_review_changes_are_recorded_in_the_notes():
    by = {c["id"]: c for c in load_gold()}
    tag = "(review 2026-11-10, before live run 1)"
    for cid in ("G26", "G28", "G29", "G30", "G34"):
        assert tag in by[cid]["notes"], cid
    assert by["G30"]["label_map"] and by["G34"]["label_map"]
    assert "grow" in by["G29"]["question"]
    assert by["G05"]["precision"] == 1 and by["G03"]["precision"] is None and by["G01"]["precision"] == 0
    assert "G26" in by["G26"]["id"] and "cannot tell" in by["G26"]["notes"]


def test_ddl_extraction_covers_only_the_allowed_tables():
    docs = schema_docs.load()
    ddl = schema_docs.ddl_for_allowed_tables(docs)
    assert ddl.count("CREATE TABLE") == 6 and "CREATE VIEW dim_customer_safe" in ddl
    assert "CREATE TABLE dim_customer " not in ddl and "email" not in ddl.replace("signup_channel", "")


# --- hardening ---------------------------------------------------------------------------------------


def test_compose_binds_to_localhost_and_has_no_literal_passwords():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    env = compose["services"]["db"]["environment"]
    assert env["POSTGRES_USER"] == "bi_admin" and env["POSTGRES_PASSWORD"].startswith("${BI_ADMIN_PASSWORD")
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "analyst_ro:analyst_ro" not in text and "bi_owner:bi_owner" not in text
    assert "${BI_ANALYST_PASSWORD" in text


def test_env_example_has_placeholders_and_env_is_ignored():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "change-me" in example and "ANALYST_DSN" in example and "OWNER_DSN" in example
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_roles_sql_revokes_public_database_privileges_and_owner_is_not_superuser():
    sql = (ROOT / "warehouse" / "roles.sql").read_text(encoding="utf-8")
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC" in sql and "NOSUPERUSER" in sql
    assert "{owner_password}" in sql and "{analyst_password}" in sql
    assert "analyst_ro" in sql and "GRANT" in sql
    config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "analyst_ro:analyst_ro" not in config and "bi_owner:bi_owner" not in config


def test_settings_have_no_default_passwords():
    from app.config import Settings

    for field in ("analyst_dsn", "owner_dsn", "admin_dsn"):
        default = Settings.model_fields[field].default
        assert "@127.0.0.1" in default and ":" not in default.split("//")[1].split("@")[0]
