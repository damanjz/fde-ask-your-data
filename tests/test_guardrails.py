"""Guardrail red-team: every bypass attempt we could think of must be rejected, legit SQL must pass."""

import pytest

from app.guardrails import (
    ALLOWED_TABLES,
    GuardrailError,
    mask_pii,
    screen_question,
    validate_sql,
)

GOOD = [
    "SELECT COUNT(*) FROM fact_orders",
    "select count(*) from fact_orders where status <> 'cancelled'",
    "SELECT city, SUM(total_inr) AS rev FROM fact_orders GROUP BY city ORDER BY rev DESC LIMIT 5",
    "WITH m AS (SELECT order_date, total_inr FROM fact_orders) SELECT SUM(total_inr) FROM m",
    "SELECT * FROM dim_customer_safe WHERE city = 'Pune'",
    "SELECT EXTRACT(YEAR FROM order_date) AS y, COUNT(*) FROM fact_orders GROUP BY 1",
    "SELECT SUBSTRING(sku FROM 1 FOR 2) AS p, COUNT(*) FROM dim_product GROUP BY 1",
    ("SELECT o.city, p.category FROM fact_orders o JOIN fact_order_items i ON i.order_id = o.order_id "
    "JOIN dim_product p ON p.product_key = i.product_key"),
    "SELECT * FROM fact_orders o, dim_date d WHERE d.full_date = o.order_date",
    "SELECT a.x FROM (SELECT 1 AS x) a",
    "SELECT order_id FROM public.fact_orders",
    "SELECT order_id FROM \"fact_orders\"",
    "SELECT 'DELETE FROM x; DROP TABLE y' AS s, reason FROM fact_returns WHERE reason = 'delete'",
    "SELECT COUNT(*) FROM fact_orders WHERE coupon_code LIKE '%10%';",
    "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'cancelled') / COUNT(*), 1) FROM fact_orders",
    "SELECT d, LAG(x) OVER (ORDER BY d) FROM (SELECT order_date d, COUNT(*) x FROM fact_orders GROUP BY 1) t",
    "SELECT order_date::date, total_inr::numeric(10,2) FROM fact_orders",
    "SELECT g FROM generate_series(1, 5) AS g",
    "SELECT CASE WHEN total_inr > 100 THEN 'big' ELSE 'small' END FROM fact_orders",
    "SELECT a FROM (SELECT 1 a UNION ALL SELECT 2) q ORDER BY a",
    "SELECT * FROM fact_orders WHERE order_id IN (SELECT order_id FROM fact_returns)",
    "SELECT 1 /* inline note */ AS one -- trailing note",
    "SELECT DATE '2026-10-31' - INTERVAL '30 days'",
    "WITH m(a, b) AS (SELECT 1, 2) SELECT a, b FROM m",
    "SELECT g FROM generate_series(1, 3) AS t(g)",
    "SELECT product_name FROM dim_product WHERE product_name ILIKE '%press%'",
    "SELECT * FROM fact_orders FETCH FIRST 5 ROWS ONLY",
]


@pytest.mark.parametrize("sql", GOOD)
def test_legit_queries_pass(sql):
    v = validate_sql(sql)
    assert v.sql.upper().startswith(("SELECT", "WITH"))
    assert not v.sql.rstrip().endswith(";")


def test_limit_added_when_absent_and_kept_when_present():
    assert validate_sql("SELECT * FROM fact_orders").sql.endswith("LIMIT 200")
    assert validate_sql("SELECT * FROM fact_orders").limit_added
    v = validate_sql("SELECT * FROM fact_orders LIMIT 5")
    assert v.sql.endswith("LIMIT 5") and not v.limit_added
    # a LIMIT only inside a subquery does not limit the outer query
    v = validate_sql("SELECT * FROM (SELECT * FROM fact_orders LIMIT 5) t")
    assert v.sql.endswith("LIMIT 200")
    # FETCH FIRST counts as a limit; trailing semicolon is dropped before LIMIT is appended
    assert not validate_sql("SELECT * FROM fact_orders FETCH FIRST 5 ROWS ONLY").limit_added
    assert validate_sql("SELECT 1;").sql == "SELECT 1 LIMIT 200"


def test_comments_are_stripped_and_cannot_hide_a_limit():
    v = validate_sql("SELECT 1 -- LIMIT 1")
    assert v.comments_stripped and v.sql.endswith("LIMIT 200") and "--" not in v.sql
    v = validate_sql("SELECT /* LIMIT 1 */ 1")
    assert v.limit_added


BAD_UNSAFE = [
    # statements that are not reads
    "INSERT INTO fact_orders VALUES (1)",
    "UPDATE fact_orders SET status = 'x'",
    "DELETE FROM fact_orders",
    "DROP TABLE fact_orders",
    "TRUNCATE fact_orders",
    "CREATE TABLE x AS SELECT 1",
    "ALTER TABLE fact_orders ADD COLUMN z int",
    "GRANT ALL ON fact_orders TO PUBLIC",
    "COPY fact_orders TO '/tmp/x'",
    "COPY (SELECT 1) TO PROGRAM 'id'",
    "SET ROLE bi_owner",
    "SET statement_timeout = 0",
    "RESET ALL",
    "SHOW ALL",
    "BEGIN",
    "COMMIT",
    "CALL some_proc()",
    "DO $$ BEGIN PERFORM 1; END $$",
    "EXPLAIN ANALYZE SELECT 1",
    "VACUUM",
    "LISTEN x",
    "PREPARE p AS SELECT 1",
    "EXECUTE p",
    # smuggling a write behind a SELECT
    "SELECT 1; DROP TABLE fact_orders",
    "SELECT 1;; SELECT 2",
    "SELECT 1; SELECT 2",
    "SELECT 1; -- ok\nDELETE FROM fact_orders",
    "SELECT 1 /* x */ ; /* y */ DELETE FROM fact_orders",
    "WITH d AS (DELETE FROM fact_orders RETURNING *) SELECT * FROM d",
    "WITH i AS (INSERT INTO fact_orders VALUES (1) RETURNING 1) SELECT * FROM i",
    "WITH u AS (UPDATE fact_orders SET status = 'x' RETURNING 1) SELECT * FROM u",
    ("WITH m AS (MERGE INTO fact_orders t USING fact_returns r ON t.order_id = r.order_id "
    "WHEN MATCHED THEN DELETE) SELECT 1"),
    "SELECT * INTO newtable FROM fact_orders",
    "SELECT * INTO TEMP t FROM fact_orders",
    "SELECT * FROM fact_orders FOR UPDATE",
    "SELECT * FROM fact_orders FOR SHARE",
    "SELECT * FROM fact_orders FOR NO KEY UPDATE",
    "SELECT * FROM fact_orders FOR KEY SHARE",
    "SELECT * FROM fact_orders FOR UPDATE SKIP LOCKED",
    # comment and whitespace tricks
    "SEL/**/ECT 1",
    "DEL/**/ETE FROM fact_orders",
    "/* start */ DELETE /* x */ FROM fact_orders",
    "-- hi\nDROP TABLE fact_orders",
    "SELECT 1 /* unterminated",
    "SELECT 1 /* a /* nested */ */ ; DROP TABLE x",
    "SELECT 1\n;\nDROP TABLE x",
    "SELECT 1\t;\tDELETE FROM x",
    # string and identifier tricks
    "SELECT E'\\'; DROP TABLE x; --'",
    "SELECT e'abc\\x41'",
    "SELECT U&'\\0041'",
    "SELECT $$; DROP TABLE x; --$$",
    "SELECT $q$ x $q$",
    "SELECT $1",
    "SELECT 'unterminated",
    "SELECT \"unterminated",
    "SELECT 1 \\gexec",
    "SELECT 1 \\; DROP TABLE x",
    "SELECT 1\x00; DROP TABLE x",
    # dangerous functions
    "SELECT pg_sleep(10)",
    "SELECT PG_SLEEP(10)",
    "SELECT * FROM pg_sleep(10)",
    "SELECT pg_catalog.pg_sleep(10)",
    "SELECT \"pg_sleep\"(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT set_config('role', 'bi_owner', false)",
    "SELECT SET_CONFIG('statement_timeout', '0', false)",
    "SELECT current_setting('is_superuser')",
    "SELECT dblink('host=x', 'select 1')",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "SELECT query_to_xml('select * from dim_customer', true, false, '')",
    "SELECT table_to_xml('dim_customer', true, false, '')",
    "SELECT pg_terminate_backend(1)",
    "SELECT pg_cancel_backend(1)",
    "SELECT nextval('s')",
    "SELECT setval('s', 1)",
    "SELECT txid_current()",
    "SELECT version()",
    "SELECT current_database()",
    "SELECT unknown_func(1)",
    "SELECT public.myfunc(1)",
    "SELECT 1 FROM fact_orders WHERE operator(pg_catalog.=)(1, 1)",
    "SELECT (TABLE dim_customer)",
    "SELECT * FROM fact_orders WHERE order_id IN (TABLE dim_customer)",
    "SELECT repeat('x', 1000000000)",
    "SELECT lpad('x', 1000000000, 'y')",
    "SELECT row_to_json(c) FROM dim_customer_safe c",
    "SELECT 'x'::regclass",
    "SELECT 'pg_sleep'::regproc",
]


@pytest.mark.parametrize("sql", BAD_UNSAFE)
def test_rejected(sql):
    with pytest.raises(GuardrailError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO fact_orders VALUES (1)",
    "SELECT 1; DROP TABLE fact_orders",
    "SELECT pg_sleep(10)",
    "SELECT set_config('role', 'bi_owner', false)",
    "SELECT $$x$$",
    "SELECT E'x'",
    "WITH set_config AS (SELECT 1) SELECT set_config('role', 'bi_owner', false)",
    "WITH query_to_xml AS (SELECT 1) SELECT query_to_xml('select * from dim_customer', true, false, '')",
    "WITH version AS (SELECT 1) SELECT version()",
    "WITH pg_sleep(x) AS (SELECT 1) SELECT 1",
    "SELECT * FROM generate_series(1, 2) AS set_config(a)",
    "SELECT * FROM fact_orders WHERE order_id = 1 AND pg_catalog.set_config('a', 'b', false) = 'b'",
])
def test_attack_signatures_are_unsafe_and_never_retried(sql):
    with pytest.raises(GuardrailError) as ei:
        validate_sql(sql)
    assert ei.value.kind == "unsafe"


BAD_TABLES = [
    "SELECT * FROM dim_customer",
    "SELECT * FROM DIM_CUSTOMER",
    "SELECT * FROM \"dim_customer\"",
    "SELECT * FROM public.dim_customer",
    "SELECT email FROM dim_customer",
    "SELECT * FROM fact_orders, dim_customer",
    "SELECT * FROM fact_orders o JOIN dim_customer c ON c.customer_key = o.customer_key",
    "SELECT * FROM fact_orders NATURAL JOIN dim_customer",
    "SELECT * FROM fact_orders o LEFT JOIN\n dim_customer c USING (customer_key)",
    "SELECT (SELECT email FROM dim_customer LIMIT 1)",
    "SELECT * FROM fact_orders WHERE customer_key IN (SELECT customer_key FROM dim_customer)",
    "SELECT * FROM fact_orders WHERE EXISTS (SELECT 1 FROM dim_customer c WHERE c.name LIKE 'A%')",
    "WITH x AS (SELECT * FROM dim_customer) SELECT * FROM x",
    "WITH dim_customer AS (SELECT * FROM dim_customer) SELECT * FROM dim_customer",
    "SELECT * FROM (SELECT * FROM dim_customer) t",
    "SELECT * FROM fact_orders a, fact_returns b, dim_customer c",
    "SELECT * FROM generate_series(1,2), dim_customer",
    "SELECT * FROM (fact_orders o JOIN dim_customer c ON true)",
    "SELECT * FROM pg_catalog.pg_roles",
    "SELECT * FROM pg_roles",
    "SELECT * FROM pg_shadow",
    "SELECT * FROM pg_user",
    "SELECT * FROM pg_stat_activity",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM information_schema.columns WHERE table_name = 'dim_customer'",
    "SELECT * FROM \"pg_catalog\".\"pg_authid\"",
    "SELECT * FROM some_other_table",
    "SELECT * FROM public.some_other_table",
    "SELECT * FROM mydb.public.fact_orders",
    "SELECT * FROM ONLY fact_orders",
    "WITH pg_x AS (SELECT 1) SELECT * FROM pg_x",
]


@pytest.mark.parametrize("sql", BAD_TABLES)
def test_table_allowlist(sql):
    with pytest.raises(GuardrailError):
        validate_sql(sql)


def test_cte_name_does_not_exempt_a_function_call():
    """A CTE named like a function is exempt only at its definition, never at a later call."""
    for sql in (
        "WITH set_config AS (SELECT 1) SELECT set_config('role', 'x', false)",
        "WITH version AS (SELECT 1) SELECT version()",
        "WITH unknown_func AS (SELECT 1) SELECT unknown_func(1)",
        "WITH m AS (SELECT 1) SELECT m(1)",
    ):
        with pytest.raises(GuardrailError):
            validate_sql(sql)
    assert validate_sql("WITH m(a) AS (SELECT 1) SELECT a FROM m").sql.startswith("WITH m(a)")


def test_pii_table_is_a_fixable_hit_labelled_pii_table():
    for sql in ("SELECT * FROM dim_customer", "SELECT * FROM fact_orders, dim_customer"):
        with pytest.raises(GuardrailError) as ei:
            validate_sql(sql)
        assert ei.value.kind == "fixable" and ei.value.rule == "pii_table"
        assert "dim_customer_safe" in str(ei.value)


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS start",
    "SELECT 1 AS table",
    "SELECT order_id AS set FROM fact_orders",
    "SELECT COUNT(*) AS load FROM fact_orders",
    "WITH d AS (DELETE FROM fact_orders RETURNING *) SELECT * FROM d",
    "SELECT * FROM fact_orders FOR UPDATE",
    "SELECT * INTO x FROM fact_orders",
    "SELECT 1 FROM fact_orders WHERE order_id IN (SELECT 1) FOR SHARE",
])
def test_reserved_words_after_the_first_token_are_fixable_not_refusals(sql):
    with pytest.raises(GuardrailError) as ei:
        validate_sql(sql)
    assert ei.value.kind == "fixable"


@pytest.mark.parametrize("sql, word", [
    ("SELECT CURRENT_DATE", "CURRENT_DATE"),
    ("SELECT * FROM fact_orders WHERE order_date > current_date - 30", "CURRENT_DATE"),
    ("SELECT NOW()", "NOW"),
    ("SELECT current_timestamp", "CURRENT_TIMESTAMP"),
    ("SELECT localtimestamp", "LOCALTIMESTAMP"),
    ("SELECT clock_timestamp()", "CLOCK_TIMESTAMP"),
    ("SELECT AGE(order_date) FROM fact_orders", "AGE"),
])
def test_wall_clock_is_fixable_with_a_hint(sql, word):
    with pytest.raises(GuardrailError) as ei:
        validate_sql(sql)
    assert ei.value.kind == "fixable" and ei.value.rule == "wall_clock"
    assert word in str(ei.value) and "literal dates" in str(ei.value)


def test_fixable_errors_are_retryable():
    for sql in ("SELECT * FROM some_other_table", "SELECT unknown_func(1)", "(SELECT 1)", "SHOW TABLES"):
        with pytest.raises(GuardrailError) as ei:
            validate_sql(sql)
        assert ei.value.kind in ("fixable", "unsafe")
    with pytest.raises(GuardrailError) as ei:
        validate_sql("SELECT * FROM some_other_table")
    assert ei.value.kind == "fixable"


@pytest.mark.parametrize("sql", ["", "   ", ";", "-- only a comment", "/* x */", "(SELECT 1)", "VALUES (1)", "TABLE fact_orders"])
def test_empty_or_not_select(sql):
    with pytest.raises(GuardrailError):
        validate_sql(sql)


def test_misc_limits():
    with pytest.raises(GuardrailError):
        validate_sql("SELECT " + "1," * 5000 + "1")
    with pytest.raises(GuardrailError):
        validate_sql("SELECT " + "(" * 20 + "1" + ")" * 20)
    with pytest.raises(GuardrailError):
        validate_sql("SELECT (1")
    with pytest.raises(GuardrailError):
        validate_sql("SELECT 1)")


def test_allowlist_matches_schema_docs():
    from app import schema_docs

    assert set(schema_docs.load().tables) == set(ALLOWED_TABLES)


# --- question screening ------------------------------------------------------------------------------


@pytest.mark.parametrize("q, kind", [
    ("Delete all cancelled orders from fact_orders", "write"),
    ("drop table fact_returns", "write"),
    ("DELETE FROM fact_orders WHERE status = 'cancelled'", "write"),
    ("Please truncate the tickets table", "write"),
    ("Update the price of BN-001 to 599", "write"),
    ("update fact_orders set status = 'delivered'", "write"),
    ("insert a new order for customer 5", "write"),
    ("bhai sare cancelled orders delete kar do", "write"),
    ("wipe all the customer data", "write"),
    ("Give me the email addresses of customers who ordered in October", "pii"),
    ("list the emails of our top 10 buyers", "pii"),
    ("phone numbers of customers in Pune", "pii"),
    ("What are the names of the top 5 customers by spend?", "pii"),
    ("customer names who returned grinders", "pii"),
    ("show me customer contact details", "pii"),
    ("select * from dim_customer", "pii"),
    ("home address of customer 17", "pii"),
    ("emails of customers in Pune", "pii"),
    ("customer email ids please", "pii"),
    ("hey. delete the records please", "write"),
    ("bhai delete all orders", "write"),
])
def test_screen_blocks(q, kind):
    got = screen_question(q)
    assert got is not None and got[0] == kind


@pytest.mark.parametrize("q", [
    "How many orders did we get last month?",
    "Customers by signup channel, including email_campaign",
    "How many customers signed up through the email campaign?",
    "Which signup channel has the most repeat buyers?",
    "Revenue from Pune this year",
    "How many tickets are still open?",
    "Is saal ka total revenue kitna hua?",
    "Average order value for orders using the WELCOME10 coupon",
    "Update: how did Diwali compare to Christmas in orders?",
    "What is the return rate for grinders last quarter?",
    "which product names have the most returns",
    "How many tickets came in by email last month?",
    "Avg first response for email tickets vs chat",
    "Was there a drop in orders after Diwali?",
    "How many customers bought each product name?",
    "Delete karo mat, bas count batao",
    "show me tickets by email",
    "Which customers bought grinders in Pune?",
    "What is the mobile app share of orders?",
    "Did we drop the price of beans? What happened to revenue?",
])
def test_screen_lets_legit_questions_through(q):
    assert screen_question(q) is None


def test_mask_pii():
    out = mask_pii("mail me at asha.rao@gmail.com or call +91 98765 43210")
    assert "asha.rao" not in out and "98765" not in out
