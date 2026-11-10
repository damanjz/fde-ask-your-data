"""Integration tests against the real warehouse. The database role is the control that must hold, so these
skip the parser entirely and talk to Postgres as analyst_ro."""

import time

import psycopg
import pytest
from psycopg import errors

from app import agent, db, guardrails
from app.config import settings
from evals import validate_gold
from evals.mock_llm import OracleLLM
from evals.run import load_gold, run_variant
from warehouse import generate

pytestmark = pytest.mark.db

PERMISSION = (errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)


@pytest.fixture
def analyst():
    with psycopg.connect(settings.analyst_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="module")
def owner():
    with psycopg.connect(settings.owner_dsn, autocommit=True) as conn:
        yield conn


BYPASS = [
    "INSERT INTO fact_orders (order_id) VALUES (999999)",
    "DELETE FROM fact_orders",
    "UPDATE fact_orders SET status = 'x'",
    "TRUNCATE fact_orders",
    "DROP TABLE fact_orders",
    "CREATE TABLE sneaky (id int)",
    "CREATE TEMP TABLE sneaky_temp (id int)",
    "CREATE TEMPORARY TABLE sneaky_temp2 AS SELECT 1",
    "CREATE VIEW v AS SELECT 1",
    "ALTER TABLE fact_orders ADD COLUMN z int",
    "GRANT SELECT ON dim_customer TO analyst_ro",
    "SELECT email FROM dim_customer",
    "SELECT name, email, phone FROM dim_customer",
    "SELECT * FROM dim_customer",
    "SELECT count(*) FROM dim_customer",
]


@pytest.mark.parametrize("sql", BYPASS)
def test_analyst_role_cannot_write_or_read_pii_even_without_the_parser(analyst, sql):
    with pytest.raises(PERMISSION):
        analyst.execute(sql)


@pytest.mark.parametrize("sql", BYPASS)
def test_the_privileges_hold_even_if_read_only_is_switched_off(analyst, sql):
    """The role's default read-only mode can be overridden per session; the grants cannot."""
    analyst.execute("SET default_transaction_read_only = off")
    with pytest.raises(errors.InsufficientPrivilege):
        analyst.execute(sql)


def test_set_role_to_the_owner_fails(analyst):
    with pytest.raises(errors.Error):
        analyst.execute("SET ROLE bi_owner")
    with pytest.raises(errors.Error):
        analyst.execute("SELECT set_config('role', 'bi_owner', false)")


def test_analyst_is_not_superuser_and_owns_nothing(analyst):
    row = analyst.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user")
    assert row.fetchone() == (False, False, False)
    owned = analyst.execute("SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                            "WHERE r.rolname = 'analyst_ro'").fetchone()[0]
    assert owned == 0


def test_statement_timeout_is_set_on_the_role(analyst):
    assert analyst.execute("SHOW statement_timeout").fetchone()[0] == "5s"
    assert analyst.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
    assert analyst.execute("SHOW search_path").fetchone()[0] == "public"


def test_role_timeout_cancels_a_slow_query_without_any_session_setting(analyst):
    start = time.monotonic()
    with pytest.raises(errors.QueryCanceled):
        analyst.execute("SELECT pg_sleep(30)")
    assert time.monotonic() - start < 10


def test_pii_columns_are_not_in_the_safe_view(owner):
    cols = {r[0] for r in owner.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'dim_customer_safe'")}
    assert cols == {"customer_key", "city", "state", "signup_date", "signup_channel"}


def test_analyst_can_read_the_safe_surface(analyst):
    for table in ("dim_date", "dim_product", "dim_customer_safe", "fact_orders", "fact_order_items",
                  "fact_returns", "fact_tickets"):
        assert analyst.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
    assert analyst.execute("SELECT count(*) FROM dim_customer_safe").fetchone()[0] == 2000


def test_analyst_cannot_see_other_schemas_or_roles_secrets(analyst):
    with pytest.raises(errors.InsufficientPrivilege):
        analyst.execute("SELECT * FROM pg_shadow")
    with pytest.raises(errors.InsufficientPrivilege):
        analyst.execute("SELECT pg_read_file('/etc/passwd')")
    with pytest.raises(errors.InsufficientPrivilege):
        analyst.execute("COPY fact_orders TO '/tmp/x'")


# --- the executor ------------------------------------------------------------------------------------


def test_run_query_returns_rows_and_columns():
    res = db.run_query("SELECT order_id, status FROM fact_orders ORDER BY order_id LIMIT 3")
    assert res.columns == ["order_id", "status"] and len(res.rows) == 3 and not res.truncated


def test_run_query_caps_rows_without_pulling_everything():
    res = db.run_query("SELECT o.order_id FROM fact_orders o CROSS JOIN generate_series(1, 100000) g")
    assert len(res.rows) == settings.max_rows and res.truncated


def test_percent_signs_are_not_treated_as_placeholders():
    assert db.run_query("SELECT 'abc' LIKE '%b%' AS m").rows == [(True,)]


def test_executor_is_read_only_and_reports_the_error():
    with pytest.raises(db.DBError) as ei:
        db.run_query("INSERT INTO fact_orders (order_id) VALUES (1)")
    assert ei.value.message  # server-side cursors cannot wrap an INSERT, so it fails before execution


def test_executor_turns_a_permission_error_into_dberror():
    with pytest.raises(db.DBError) as ei:
        db.run_query("SELECT email FROM dim_customer")
    assert ei.value.is_permission


def test_executor_timeout(monkeypatch):
    monkeypatch.setattr(settings, "statement_timeout_ms", 300)
    with pytest.raises(db.DBError) as ei:
        db.run_query("SELECT pg_sleep(5)")
    assert ei.value.is_timeout


def test_executor_error_message_is_usable_for_self_correction():
    with pytest.raises(db.DBError) as ei:
        db.run_query("SELECT nope FROM fact_orders")
    assert "nope" in ei.value.message


# --- warehouse content -------------------------------------------------------------------------------


def test_loaded_counts_match_the_generator(owner):
    data = generate.generate()
    for table in generate.LOAD_ORDER:
        n = owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert n == len(data[table]), table


def test_schema_docs_match_the_database(owner):
    from app import schema_docs

    docs = schema_docs.load()
    for table in docs.tables.values():
        rows = owner.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
            (table.name,)).fetchall()
        assert [r[0] for r in rows] == [c.name for c in table.columns], table.name
        for col in table.columns:
            if col.values:
                actual = {r[0] for r in owner.execute(f"SELECT DISTINCT {col.name} FROM {table.name}")}
                assert actual == set(col.values), f"{table.name}.{col.name}"


def test_samples_are_fetched_live():
    docs = agent.schema_docs.load()
    samples = agent.fetch_samples(docs)
    assert "Bengaluru" in samples[("fact_orders", "city")]
    assert "Diwali Run-up" in samples[("dim_date", "festival")]


def test_gold_set_is_valid_against_the_loaded_warehouse():
    cases = load_gold()
    with psycopg.connect(settings.analyst_dsn) as conn:
        assert validate_gold.validate(cases, conn) == []


def test_every_gold_sql_passes_the_guardrails_unchanged_except_limit():
    for case in load_gold():
        if case["sql"]:
            v = guardrails.validate_sql(case["sql"])
            assert v.sql.startswith(case["sql"].rstrip("; "))


def test_eval_pipeline_end_to_end_with_the_oracle_llm(tmp_path):
    cases = load_gold()
    picked = [c for c in cases if c["id"] in {"G01", "G14", "G26", "G33", "G36", "G38", "G40"}]
    oracle = OracleLLM(cases)
    with psycopg.connect(settings.analyst_dsn) as conn:
        rows = run_variant("agent", picked, conn, chat_fn=oracle.chat, log_path=tmp_path / "r.jsonl", progress=False)
    assert all(r["score"]["passed"] for r in rows), [r["score"] for r in rows if not r["score"]["passed"]]
    assert (tmp_path / "r.jsonl").read_text(encoding="utf-8").count("\n") == len(picked)


def test_agent_against_the_real_database_with_a_scripted_model():
    class One:
        calls = 0

        def chat(self, messages):
            One.calls += 1
            text = ("PLAN: count\n```sql\nSELECT COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled'\n```"
                    if One.calls == 1 else "There were 9999 orders.")
            from app.llm import LLMResponse
            return LLMResponse(content=text, prompt_tokens=1, completion_tokens=1)

    out = agent.ask("how many orders?", chat_fn=One().chat, samples={}, log_requests=False)
    assert out["status"] == "answered" and out["rows"][0][0] > 7000
    assert out["summary"] is None  # 9999 is not the real count, so the faithfulness check dropped it
    assert out["summary_faithful"] is False


def test_agent_blocks_pii_end_to_end_with_a_misbehaving_model():
    class Bad:
        def chat(self, messages):
            from app.llm import LLMResponse
            return LLMResponse(content="PLAN: x\n```sql\nSELECT email FROM dim_customer\n```")

    out = agent.ask("who are our best buyers", chat_fn=Bad().chat, samples={}, log_requests=False)
    assert out["refused"] and out["executed"] is False and out["rows"] == []


def test_cte_and_window_queries_run_through_the_server_cursor():
    sql = guardrails.validate_sql(
        "WITH m AS (SELECT DATE_TRUNC('month', order_date)::date AS d, COUNT(*) AS n FROM fact_orders GROUP BY 1) "
        "SELECT d, n, LAG(n) OVER (ORDER BY d) FROM m ORDER BY d"
    ).sql
    res = db.run_query(sql)
    assert len(res.rows) == 12 and res.rows[0][2] is None


def test_runaway_queries_are_cancelled_by_the_timeout(monkeypatch):
    monkeypatch.setattr(settings, "statement_timeout_ms", 700)
    sql = guardrails.validate_sql(
        "SELECT COUNT(*) FROM fact_orders a, fact_orders b, fact_orders c, fact_orders d"
    ).sql
    start = time.monotonic()
    with pytest.raises(db.DBError) as ei:
        db.run_query(sql)
    assert ei.value.is_timeout and time.monotonic() - start < 6


# --- review 2026-11-10 -------------------------------------------------------------------------------


def test_owner_is_not_a_superuser_and_public_has_no_database_privileges(owner):
    row = owner.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = 'bi_owner'")
    assert row.fetchone() == (False, False, False)
    with pytest.raises(errors.InsufficientPrivilege):
        owner.execute("CREATE ROLE sneaky_role")
    assert owner.execute("SELECT has_database_privilege('analyst_ro', current_database(), 'TEMP')").fetchone() == (False,)
    assert owner.execute("SELECT has_database_privilege('bi_owner', current_database(), 'CONNECT')").fetchone() == (True,)
    acl = owner.execute("SELECT datacl::text FROM pg_database WHERE datname = current_database()").fetchone()[0]
    assert not [e for e in acl.strip("{}").split(",") if e.startswith("=")]  # no PUBLIC entry (CONNECT/TEMP)


def test_tables_are_owned_by_the_non_superuser_owner(owner):
    owners = {r[0] for r in owner.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")}
    assert owners == {"bi_owner"}


def test_database_only_listens_on_localhost():
    import os
    import shutil
    import subprocess

    docker = os.environ.get("DOCKER") or shutil.which("docker")
    if not docker or not os.path.exists(docker):
        pytest.skip("docker CLI not found (set DOCKER or put docker on PATH)")
    out = subprocess.run([docker, "ps", "--format", "{{.Names}} {{.Ports}}"], check=False, capture_output=True,
                         text=True)
    line = next(ln for ln in out.stdout.splitlines() if ln.startswith("fde-bi-db"))
    assert "127.0.0.1:5435->5432" in line and "0.0.0.0" not in line


def _wrong_variants():
    """SQL that the old scorer passed (or could pass) although it answers the question wrongly."""
    oct_ = "order_date BETWEEN '2026-10-01' AND '2026-10-31'"
    lq = "order_date BETWEEN '2026-07-01' AND '2026-09-30'"
    ty = "order_date BETWEEN '2026-04-01' AND '2026-10-31'"
    ret = ("FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id "
           "JOIN dim_product p ON p.product_key = i.product_key "
           "LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id "
           f"WHERE o.status = 'delivered' AND o.{lq}")
    return [
        ("G01", "hedge: count with and without cancelled",
         f"SELECT COUNT(*) FILTER (WHERE status <> 'cancelled'), COUNT(*) FROM fact_orders WHERE {oct_}"),
        ("G03", "hedge: AVG with and without cancelled",
         f"SELECT AVG(total_inr) FILTER (WHERE status <> 'cancelled'), AVG(total_inr) FROM fact_orders WHERE {ty}"),
        ("G05", "ROUND(x, 0) against a 1-decimal gold",
         (f"SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*), 0) FROM fact_orders "
         f"WHERE {lq} AND status <> 'cancelled'")),
        ("G05", "integer division",
         (f"SELECT 100 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*) FROM fact_orders "
         f"WHERE {lq} AND status <> 'cancelled'")),
        ("G05", "a ratio instead of a percent",
         (f"SELECT ROUND(1.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*), 3) FROM fact_orders "
         f"WHERE {lq} AND status <> 'cancelled'")),
        ("G30", "swapped weekend and weekday labels",
         ("SELECT CASE WHEN d.is_weekend THEN 'weekday' ELSE 'weekend' END, "
         "ROUND(COUNT(o.order_id)::numeric / COUNT(DISTINCT d.full_date), 1) FROM dim_date d "
         "LEFT JOIN fact_orders o ON o.order_date = d.full_date AND o.status <> 'cancelled' "
         "WHERE d.full_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY d.is_weekend")),
        ("G34", "swapped Diwali and other labels",
         ("SELECT CASE WHEN COALESCE(d.festival = 'Diwali Run-up', false) THEN 'other days' ELSE 'diwali' END, "
         "SUM(o.total_inr) FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date "
         f"WHERE o.{oct_} AND o.status <> 'cancelled' GROUP BY COALESCE(d.festival = 'Diwali Run-up', false)")),
        ("G26", "category return rates rounded to whole percents",
         f"SELECT p.category, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 0) {ret} GROUP BY p.category"),
        ("G28", "highest return rate with the rate rounded to a whole percent",
         ("SELECT p.product_name, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 0) AS rate "
         "FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id "
         "JOIN dim_product p ON p.product_key = i.product_key "
         "LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' "
         "GROUP BY p.product_name HAVING COUNT(*) >= 30 ORDER BY rate DESC, p.product_name LIMIT 1")),
        ("G10", "top 5 cities with an extra numeric revenue column",
         (f"SELECT city, COUNT(*) AS n, SUM(total_inr) FROM fact_orders o WHERE {ty} AND status <> 'cancelled' "
         "GROUP BY city ORDER BY n DESC, city LIMIT 5")),
    ]


@pytest.mark.parametrize("gold_id, why, sql", _wrong_variants(), ids=lambda v: v if len(str(v)) < 12 else "")
def test_wrong_but_formerly_passing_variants_now_fail(gold_id, why, sql):
    from evals.scoring import score_case

    case = next(c for c in load_gold() if c["id"] == gold_id)
    with psycopg.connect(settings.analyst_dsn) as conn:
        out = score_case(case, {"sql": sql, "refused": False, "status": "answered"}, conn)
    assert not out["passed"], why


def test_ten_or_more_wrong_variants_are_covered():
    assert len(_wrong_variants()) >= 10


def test_correct_variants_still_pass():
    """The stricter scorer must not reject honest answers: finer rounding, text labels, extra text columns."""
    from evals.scoring import score_case

    by = {c["id"]: c for c in load_gold()}
    lq = "order_date BETWEEN '2026-07-01' AND '2026-09-30'"
    good = [
        ("G05", (f"SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*), 2) "
                f"FROM fact_orders WHERE {lq} AND status <> 'cancelled'")),
        ("G05", (f"SELECT 100.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*) "
                f"FROM fact_orders WHERE {lq} AND status <> 'cancelled'")),
        ("G30", ("SELECT CASE WHEN d.is_weekend THEN 'weekend' ELSE 'weekday' END AS day_type, "
                "ROUND(COUNT(o.order_id)::numeric / COUNT(DISTINCT d.full_date), 1) AS orders_per_day FROM dim_date d "
                "LEFT JOIN fact_orders o ON o.order_date = d.full_date AND o.status <> 'cancelled' "
                "WHERE d.full_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY d.is_weekend")),
        ("G03", ("SELECT ROUND(AVG(total_inr), 0) FROM fact_orders WHERE status <> 'cancelled' "
                "AND order_date BETWEEN '2026-04-01' AND '2026-10-31'")),
        ("G21", ("SELECT sku, product_name, list_price_inr FROM dim_product WHERE category = 'grinder' "
                "ORDER BY list_price_inr DESC LIMIT 1")),
    ]
    with psycopg.connect(settings.analyst_dsn) as conn:
        for gold_id, sql in good:
            out = score_case(by[gold_id], {"sql": sql, "refused": False, "status": "answered"}, conn)
            assert out["passed"], (gold_id, out["reasons"])


def test_ddl_and_floor_variants_run_end_to_end_with_the_oracle(tmp_path):
    cases = load_gold()
    picked = [c for c in cases if c["id"] in {"G01", "G30", "G34", "G37", "G40"}]
    oracle = OracleLLM(cases)
    with psycopg.connect(settings.analyst_dsn) as conn:
        for variant in ("agent", "ddl", "floor"):
            rows = run_variant(variant, picked, conn, chat_fn=oracle.chat, log_path=tmp_path / f"{variant}.jsonl",
                               progress=False)
            assert all(r["score"]["passed"] for r in rows), (variant, [r["score"] for r in rows])
            assert {r["result"]["variant"] for r in rows} == {variant}


def test_supporting_counts_pass_against_the_real_gold():
    """Live run 1 failed these correct answers; run 2's rule accepts a count beside a label or a rate."""
    from evals.scoring import score_case

    by = {c["id"]: c for c in load_gold()}
    lq = "o.order_date BETWEEN '2026-07-01' AND '2026-09-30'"
    ty = "o.order_date BETWEEN '2026-04-01' AND '2026-10-31'"
    ret = ("FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id "
           "JOIN dim_product p ON p.product_key = i.product_key "
           "LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered'")
    good = [
        ("G04", (f"SELECT payment_method, COUNT(*) AS orders FROM fact_orders o WHERE {lq} AND o.status <> 'cancelled' "
                "GROUP BY payment_method ORDER BY orders DESC LIMIT 1")),
        ("G12", ("SELECT coupon_code, COUNT(*) AS uses FROM fact_orders o WHERE coupon_code IS NOT NULL "
                "AND o.status <> 'cancelled' GROUP BY coupon_code ORDER BY uses DESC LIMIT 1")),
        ("G07", ("SELECT payment_method, ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'cancelled') / COUNT(*), 1) "
                f"AS rate_pct, COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled, COUNT(*) AS total "
                f"FROM fact_orders o WHERE {ty} GROUP BY payment_method")),
        ("G26", (f"SELECT p.category, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) AS rate_pct, "
                f"COUNT(r.return_id) AS returned, COUNT(*) AS sold {ret} AND {lq} GROUP BY p.category")),
        ("G28", (f"SELECT p.product_name, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) AS rate, "
                f"COUNT(r.return_id) AS returned, COUNT(*) AS sold {ret} GROUP BY p.product_name "
                "HAVING COUNT(*) >= 30 ORDER BY rate DESC, p.product_name LIMIT 1")),
    ]
    with psycopg.connect(settings.analyst_dsn) as conn:
        for gold_id, sql in good:
            out = score_case(by[gold_id], {"sql": sql, "refused": False, "status": "answered"}, conn)
            assert out["passed"], (gold_id, out["reasons"])
