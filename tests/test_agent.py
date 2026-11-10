"""Agent flow with a mocked LLM and a mocked database. No Ollama, no Postgres."""

import json
from datetime import date
from decimal import Decimal

import pytest

from app import agent, db, llm
from app.agent import ask, check_summary, parse_reply
from app.config import settings


class ScriptedLLM:
    """Returns scripted replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages):
        llm.check_messages(messages)  # the real client enforces one system message, first
        self.requests.append(messages)
        text = self.replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return llm.LLMResponse(content=text, prompt_tokens=100, completion_tokens=20, provider="mock", model="m")


def sql_reply(sql, plan="plan"):
    return f"PLAN: {plan}\n```sql\n{sql}\n```"


class FakeDB:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.ran = []

    def __call__(self, sql):
        self.ran.append(sql)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def result(columns, rows, truncated=False):
    return db.QueryResult(columns, rows, truncated)


def run(question, llm_obj, db_obj=None, **kw):
    return ask(question, chat_fn=llm_obj.chat, run_fn=db_obj or FakeDB(), samples={}, log_requests=False, **kw)


# --- reply parsing -----------------------------------------------------------------------------------


def test_parse_reply_variants():
    r = parse_reply("PLAN: count orders\n```sql\nSELECT 1\n```")
    assert (r["kind"], r["sql"], r["plan"]) == ("sql", "SELECT 1", "count orders")
    assert parse_reply("```\nSELECT 2\n```")["sql"] == "SELECT 2"
    assert parse_reply("Sure! Here you go:\nSELECT 3 FROM fact_orders")["sql"].startswith("SELECT 3")
    assert parse_reply("REFUSE: no writes")["kind"] == "refuse"
    assert parse_reply("refuse - cannot do that")["kind"] == "refuse"
    r = parse_reply("UNANSWERABLE: no marketing spend table")
    assert r["kind"] == "unanswerable" and "marketing" in r["message"]
    assert parse_reply("I think the answer is 42")["kind"] == "none"
    # prose that merely starts with "With" or "Select" is not SQL
    assert parse_reply("With the given schema I am not sure how to answer.")["kind"] == "none"
    assert parse_reply("WITH m AS (SELECT 1) SELECT * FROM m")["kind"] == "sql"
    # a refusal marker wins over SQL the model also wrote
    assert parse_reply("REFUSE: personal data\n```sql\nSELECT email FROM dim_customer\n```")["kind"] == "refuse"
    # first fenced block is used
    assert parse_reply("```sql\nSELECT 1\n```\n```sql\nSELECT 2\n```")["sql"] == "SELECT 1"


# --- happy path --------------------------------------------------------------------------------------


def test_answer_has_contract_keys_and_grounded_summary():
    model = ScriptedLLM(sql_reply("SELECT COUNT(*) AS orders FROM fact_orders"), "We got 1024 orders.")
    out = run("How many orders last month?", model, FakeDB(result(["orders"], [(1024,)])))
    for key in ("sql", "columns", "rows", "summary", "refused", "refused_reason", "usage"):
        assert key in out
    assert out["refused"] is False and out["refused_reason"] is None
    assert out["sql"].endswith("LIMIT 200")  # guardrail added the limit
    assert out["columns"] == ["orders"] and out["rows"] == [[1024]]
    assert out["summary"] == "We got 1024 orders." and out["summary_faithful"] is True
    assert set(out["usage"]) >= {"prompt_tokens", "completion_tokens", "latency_ms"}
    assert out["usage"]["prompt_tokens"] == 200 and out["usage"]["completion_tokens"] == 40  # two LLM calls
    assert out["executed"] is True and out["status"] == "answered"


def test_every_request_has_exactly_one_system_message_first():
    model = ScriptedLLM(
        sql_reply("SELECT nope FROM fact_orders"), sql_reply("SELECT COUNT(*) FROM fact_orders"), "There were 5."
    )
    database = FakeDB(db.DBError("column nope does not exist", "42703"), result(["n"], [(5,)]))
    run("q", model, database)
    assert len(model.requests) == 3  # sql, correction, summary
    for messages in model.requests:
        assert [m["role"] for m in messages].count("system") == 1 and messages[0]["role"] == "system"
    assert [m["role"] for m in model.requests[1]] == ["system", "user", "assistant", "user"]
    assert "column nope does not exist" in model.requests[1][-1]["content"]


def test_only_first_50_rows_shown_but_count_is_full():
    rows = [(i, f"c{i}") for i in range(120)]
    model = ScriptedLLM(sql_reply("SELECT a, b FROM fact_orders"), "There are 120 rows.")
    out = run("list", model, FakeDB(result(["a", "b"], rows)))
    assert len(out["rows"]) == 50 and out["row_count"] == 120


def test_json_safe_cells():
    model = ScriptedLLM(sql_reply("SELECT 1"), "ok")
    out = run("q", model, FakeDB(result(["d", "x", "y"], [(date(2026, 8, 1), Decimal("3.50"), Decimal(4))])))
    assert out["rows"] == [["2026-08-01", 3.5, 4]]
    json.dumps(out)  # must serialise


def test_zero_rows_gets_a_fixed_summary_without_a_second_llm_call():
    model = ScriptedLLM(sql_reply("SELECT 1 FROM fact_orders WHERE false"))
    out = run("q", model, FakeDB(result(["x"], [])))
    assert "no rows" in out["summary"] and len(model.requests) == 1


# --- refusals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("question, reason", [
    ("Delete all cancelled orders", "write_request"),
    ("Give me the email addresses of customers who bought grinders", "pii_request"),
])
def test_blatant_requests_are_refused_before_the_llm(question, reason):
    model = ScriptedLLM()  # any LLM call would raise IndexError
    database = FakeDB()
    out = run(question, model, database)
    assert out["refused"] and out["refused_reason"] == reason and out["status"] == "refused"
    assert model.requests == [] and database.ran == [] and out["executed"] is False
    assert out["sql"] is None and out["summary"]


def test_model_refusal_executes_nothing():
    model = ScriptedLLM("REFUSE: I cannot change data.")
    database = FakeDB()
    out = run("bhai BN-001 ka price 599 kar do, update kar dena", model, database)
    assert out["refused"] and out["refused_reason"] == "write_request" and database.ran == []


def test_model_pii_refusal_is_labelled_pii():
    out = run("who are our best buyers", ScriptedLLM("REFUSE: I can't share customer names."))
    assert out["refused_reason"] == "pii_request"


def test_unanswerable_says_what_is_missing():
    out = run("What is our CAC?", ScriptedLLM("UNANSWERABLE: there is no marketing spend data."))
    assert out["refused"] and out["refused_reason"] == "unanswerable"
    assert "marketing spend" in out["refusal_message"] and out["executed"] is False


@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM fact_orders",
    "SELECT 1; DROP TABLE fact_orders",
    "SELECT pg_sleep(30)",
    "WITH set_config AS (SELECT 1) SELECT set_config('role', 'bi_owner', false)",
])
def test_unsafe_sql_from_the_model_is_blocked_and_never_run(bad_sql):
    model = ScriptedLLM(sql_reply(bad_sql))
    database = FakeDB()
    out = run("how many orders", model, database)
    assert out["refused"] and database.ran == [] and out["guardrail_blocks"] == 1
    assert out["refused_reason"] == "guardrail_blocked" and out["refused_source"] == "parser"
    assert len(model.requests) == 1  # unsafe SQL is not retried


def test_fixable_guardrail_error_gets_one_correction():
    model = ScriptedLLM(
        sql_reply("SELECT * FROM orders"), sql_reply("SELECT COUNT(*) FROM fact_orders"), "There were 7 orders."
    )
    database = FakeDB(result(["n"], [(7,)]))
    out = run("q", model, database)
    assert out["status"] == "answered" and out["guardrail_blocks"] == 1 and len(database.ran) == 1
    assert "not in the allowlist" in model.requests[1][-1]["content"]


def test_second_guardrail_failure_is_a_refusal_not_a_loop():
    model = ScriptedLLM(sql_reply("SELECT * FROM orders"), sql_reply("SELECT * FROM customers"))
    out = run("q", model)
    assert out["refused"] and out["refused_reason"] == "guardrail_blocked" and len(model.requests) == 2


# --- database errors ---------------------------------------------------------------------------------


def test_db_error_then_success_uses_one_correction():
    model = ScriptedLLM(sql_reply("SELECT bad FROM fact_orders"), sql_reply("SELECT 1"), "The result is 1.")
    database = FakeDB(db.DBError("column bad does not exist", "42703"), result(["x"], [(1,)]))
    out = run("q", model, database)
    assert out["status"] == "answered" and len(database.ran) == 2
    assert out["attempts"][0]["stage"] == "db"


def test_db_error_twice_is_an_error_not_a_third_try():
    model = ScriptedLLM(sql_reply("SELECT a"), sql_reply("SELECT b"))
    database = FakeDB(db.DBError("nope", "42703"), db.DBError("still nope", "42703"))
    out = run("q", model, database)
    assert out["status"] == "error" and "still nope" in out["error"] and len(model.requests) == 2
    assert out["refused"] is False and out["rows"] == []


def test_permission_error_from_the_database_is_a_block_without_retry():
    model = ScriptedLLM(sql_reply("SELECT 1"))
    out = run("q", model, FakeDB(db.DBError("permission denied for table dim_customer", "42501")))
    assert out["refused"] and out["refused_reason"] == "permission_denied" and out["guardrail_blocks"] == 1
    assert len(model.requests) == 1


def test_format_error_gets_one_correction():
    model = ScriptedLLM("I am not sure.", sql_reply("SELECT 1"), "Result 1.")
    out = run("q", model, FakeDB(result(["x"], [(1,)])))
    assert out["status"] == "answered" and out["attempts"][0]["stage"] == "format"


# --- never raises ------------------------------------------------------------------------------------


def test_llm_failure_never_raises():
    out = run("q", ScriptedLLM(llm.LLMError("ollama down")))
    assert out["status"] == "error" and "ollama down" in out["error"] and out["refused"] is False


def test_unexpected_exception_never_raises():
    out = run("q", ScriptedLLM(RuntimeError("kaboom")))
    assert out["status"] == "error" and "kaboom" in out["error"]


def test_database_down_never_raises():
    model = ScriptedLLM(sql_reply("SELECT 1"), sql_reply("SELECT 1"))
    out = run("q", model, FakeDB(db.DBError("connection refused"), db.DBError("connection refused")))
    assert out["status"] == "error"


@pytest.mark.parametrize("q", ["", "   ", "x" * 5000])
def test_bad_questions_never_raise(q):
    out = run(q, ScriptedLLM())
    assert out["status"] == "error" and out["error"]


def test_summary_call_failure_keeps_the_rows():
    model = ScriptedLLM(sql_reply("SELECT 1"), llm.LLMError("timeout"))
    out = run("q", model, FakeDB(result(["x"], [(1,)])))
    assert out["status"] == "answered" and out["rows"] == [[1]] and out["summary"] is None


# --- summary grounding -------------------------------------------------------------------------------


def test_summary_with_an_invented_number_is_dropped_and_flagged():
    model = ScriptedLLM(sql_reply("SELECT 1"), "Revenue was 5,364,705, up 12% on last year.")
    out = run("revenue?", model, FakeDB(result(["revenue"], [(5364705,)])))
    assert out["summary"] is None and out["summary_faithful"] is False
    assert out["summary_unmatched"] == ["12"]
    assert out["rows"] == [[5364705]]  # rows are still shown


def test_dropped_summary_is_logged(tmp_path):
    log_file = tmp_path / "req.jsonl"
    model = ScriptedLLM(sql_reply("SELECT 1"), "Made up 999.")
    ask("q?", chat_fn=model.chat, run_fn=FakeDB(result(["x"], [(1,)])), samples={}, log_path=log_file)
    rec = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert rec["summary_dropped"] is True and rec["summary_unmatched"] == ["999"]
    for key in ("question", "sql", "rows_returned", "latency_ms", "prompt_tokens", "completion_tokens",
                "refused_reason"):
        assert key in rec


def test_log_masks_pii_in_the_question(tmp_path):
    log_file = tmp_path / "req.jsonl"
    ask("orders for asha.rao@gmail.com?", chat_fn=ScriptedLLM("UNANSWERABLE: x").chat, run_fn=FakeDB(),
        samples={}, log_path=log_file)
    text = log_file.read_text(encoding="utf-8")
    assert "asha.rao" not in text and "[email]" in text


@pytest.mark.parametrize("summary, rows, columns, expected", [
    ("There were 1,024 orders.", [(1024,)], ["n"], []),
    ("AOV was 2,569.5 rupees.", [(Decimal("2569.5022"),)], ["aov"], []),  # rounded to the precision shown
    ("AOV was 2,570 rupees.", [(Decimal("2569.5022"),)], ["aov"], []),
    ("AOV was 2,580 rupees.", [(Decimal("2569.5022"),)], ["aov"], ["2,580"]),
    ("2026-08 had the most tickets: 320.", [("2026-08", 320)], ["month", "n"], []),
    ("BN-001 leads with 82,834.", [("BN-001", 82834)], ["sku", "rev"], []),
    ("Three rows came back.", [(1,), (2,), (3,)], ["x"], []),  # no digits, nothing to check
    ("The top 5 are listed.", [(1,)], ["x"], []),  # 5 is in the question below
    ("Grew 35.4% and 52.6%.", [(date(2026, 8, 1), Decimal("35.4")), (date(2026, 9, 1), Decimal("52.6"))],
     ["m", "g"], []),
    ("Up 36.4%.", [(Decimal("35.4"),)], ["g"], ["36.4"]),
    ("Revenue 5.4 crore.", [(54123456,)], ["r"], ["5.4"]),
    ("There were 3 rows.", [(1,), (2,), (3,)], ["x"], []),  # the row count itself
    ("Orders fell 15.3% in September.", [(date(2026, 9, 1), Decimal("-15.3"))], ["m", "g"], []),  # sign ignored
    ("Growth was -15.3%.", [(Decimal("15.3"),)], ["g"], []),
    ("The peak was in month 8 with 500 orders.", [(date(2026, 8, 1), 500)], ["m", "n"], ["8"]),  # month != qty
    ("Day 1 had 500 orders.", [(date(2026, 8, 1), 500), (date(2026, 8, 2), 600)], ["d", "n"], ["1"]),
    ("In 2026 there were 500 orders.", [(date(2026, 8, 1), 500)], ["d", "n"], []),  # a 4-digit year is fine
    ("On 2026-08-01 there were 500 orders.", [(date(2026, 8, 1), 500)], ["d", "n"], []),
    ("On 2026-09-01 there were 500 orders.", [(date(2026, 8, 1), 500)], ["d", "n"], ["2026-09-01"]),
    ("FY2026-27 revenue was 5,364,705.", [("FY2026-27", 5364705)], ["fy", "r"], []),
    ("Q2 revenue was 5,364,705.", [("FY2026-27 Q2", 5364705)], ["fy", "r"], []),
])
def test_check_summary(summary, rows, columns, expected):
    assert check_summary(summary, "show the top 5 please", columns, rows, len(rows)) == expected


# Rows shaped like the real G07 answer seen on camera (2026-11-10): the summary named UPI as lowest at 2.3%,
# every number was in the rows, and the claim was still wrong (wallet is lowest).
_PAY = [("cod", 812, 61, Decimal("7.5")), ("upi", 2410, 55, Decimal("2.3")),
        ("card", 990, 31, Decimal("3.1")), ("wallet", 401, 7, Decimal("1.7"))]


@pytest.mark.parametrize("summary, expected", [
    ("UPI has the lowest cancellation rate at 2.3%.", ["lowest: upi"]),
    ("Wallet has the lowest cancellation rate at 1.7%.", []),
    ("COD has the highest cancellation rate at 7.5%, while wallet has the lowest at 1.7%.", []),
    ("Card has the highest rate at 3.1% and wallet the lowest at 1.7%.", ["highest: card"]),
    ("UPI has the most orders with 2410.", []),
    ("Wallet has the most orders with 401.", ["most: wallet"]),
    ("COD has the most cancellations, 61.", []),
    ("UPI had at least 2410 orders.", []),  # 'at least' is not a superlative
    ("Cancellation rates range from 1.7% to 7.5%.", []),  # no superlative claim
    ("UPI and card have the lowest rates.", []),  # two labels: ambiguous, not guessed
    ("The lowest is wallet.", []),  # no number and several numeric columns: not guessed
])
def test_superlative_claims_must_name_the_right_row(summary, expected):
    cols = ["payment_method", "orders", "cancelled", "cancel_rate_pct"]
    assert check_summary(summary, "Cancellation rate by payment method this year", cols, _PAY, 4) == expected


def test_superlative_with_one_numeric_column_needs_no_number():
    rows = [("north", 10), ("south", 30)]
    assert check_summary("North has the most orders.", "orders by region", ["r", "n"], rows, 2) == ["most: north"]
    assert check_summary("South has the most orders.", "orders by region", ["r", "n"], rows, 2) == []


# --- model name is read per call ---------------------------------------------------------------------


def test_model_is_read_from_settings_on_every_call(monkeypatch):
    sent = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return Resp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_model", "model-a")
    llm.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    monkeypatch.setattr(settings, "ollama_model", "model-b")
    llm.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert [p["model"] for p in sent] == ["model-a", "model-b"]
    assert sent[0]["think"] is False and sent[0]["stream"] is False


def test_llm_rejects_two_system_messages():
    with pytest.raises(llm.LLMError):
        llm.check_messages([{"role": "system", "content": "a"}, {"role": "system", "content": "b"}])
    with pytest.raises(llm.LLMError):
        llm.check_messages([{"role": "user", "content": "a"}, {"role": "system", "content": "b"}])


def test_anthropic_conversion_keeps_one_system_string():
    system, rest = llm.to_anthropic_messages(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}, {"role": "assistant", "content": "A"}]
    )
    assert system == "S" and [m["role"] for m in rest] == ["user", "assistant"]


def test_floor_variant_uses_table_names_only():
    for name in ("floor", "baseline"):
        model = ScriptedLLM(sql_reply("SELECT 1"), "ok 1")
        ask("q", variant=name, chat_fn=model.chat, run_fn=FakeDB(result(["x"], [(1,)])), samples={},
            log_requests=False)
        system = model.requests[0][0]["content"]
        assert "fact_orders" in system and "order_date" not in system and "Conventions" not in system
        assert "2026" not in system and "CREATE TABLE" not in system


def test_ddl_variant_has_raw_ddl_and_the_date_and_nothing_else():
    model = ScriptedLLM(sql_reply("SELECT 1"), "ok 1")
    out = ask("q", variant="ddl", chat_fn=model.chat, run_fn=FakeDB(result(["x"], [(1,)])), samples={},
              log_requests=False)
    system = model.requests[0][0]["content"]
    assert out["variant"] == "ddl" and system.count("CREATE TABLE") == 6
    assert "CREATE VIEW dim_customer_safe" in system and "CREATE TABLE dim_customer " not in system
    assert "order_date" in system and "REFERENCES dim_date" in system and "Today is 2026-11-10." in system
    assert "Conventions" not in system and "April to March" not in system and "Examples" not in system
    assert "Bengaluru" not in system  # no sample values
    assert [m["role"] for m in model.requests[0]] == ["system", "user"]


def test_unknown_variant_is_an_error_not_a_crash():
    out = ask("q", variant="nope", chat_fn=ScriptedLLM().chat, run_fn=FakeDB(), samples={}, log_requests=False)
    assert out["status"] == "error" and "unknown variant" in out["error"]


# --- retries for reserved words, wall clock and the PII table ----------------------------------------


def test_reserved_word_alias_gets_one_correction_not_a_refusal():
    model = ScriptedLLM(sql_reply("SELECT COUNT(*) AS start FROM fact_orders"),
                        sql_reply("SELECT COUNT(*) AS n FROM fact_orders"), "There were 5.")
    out = run("q", model, FakeDB(result(["n"], [(5,)])))
    assert out["status"] == "answered" and out["refused"] is False and out["guardrail_blocks"] == 1
    assert "reserved word" in model.requests[1][-1]["content"]


def test_wall_clock_gets_a_hint_and_one_correction():
    model = ScriptedLLM(sql_reply("SELECT COUNT(*) FROM fact_orders WHERE order_date > CURRENT_DATE - 30"),
                        sql_reply("SELECT COUNT(*) FROM fact_orders WHERE order_date >= '2026-10-02'"), "It is 5.")
    out = run("orders in the last 30 days", model, FakeDB(result(["n"], [(5,)])))
    assert out["status"] == "answered"
    assert "literal dates" in model.requests[1][-1]["content"]


def test_dim_customer_hit_is_fixable_once_then_a_pii_refusal():
    model = ScriptedLLM(sql_reply("SELECT city, COUNT(*) FROM dim_customer GROUP BY city"),
                        sql_reply("SELECT city, COUNT(*) FROM dim_customer_safe GROUP BY city"), "5 cities.")
    out = run("customers per city", model, FakeDB(result(["city", "n"], [("Pune", 5)])))
    assert out["status"] == "answered" and out["guardrail_blocks"] == 1
    assert "dim_customer_safe" in model.requests[1][-1]["content"]
    # repeated: the model insists on the PII table
    model = ScriptedLLM(sql_reply("SELECT email FROM dim_customer"), sql_reply("SELECT name FROM dim_customer"))
    database = FakeDB()
    out = run("who are our best buyers", model, database)
    assert out["refused"] and out["refused_reason"] == "pii_request" and out["refused_source"] == "parser"
    assert database.ran == [] and out["executed"] is False and len(model.requests) == 2


def test_dim_customer_then_a_different_blocked_query_is_a_guardrail_refusal():
    model = ScriptedLLM(sql_reply("SELECT 1 FROM dim_customer"), sql_reply("SELECT 1 FROM nope"))
    out = run("q", model)
    assert out["refused"] and out["refused_reason"] == "guardrail_blocked" and out["executed"] is False


# --- refusal sources and summary errors --------------------------------------------------------------


def test_refusal_sources_are_recorded():
    assert run("Delete all orders", ScriptedLLM())["refused_source"] == "prefilter"
    assert run("q", ScriptedLLM("REFUSE: no"))["refused_source"] == "model"
    assert run("q", ScriptedLLM("UNANSWERABLE: no data"))["refused_source"] == "model"
    assert run("q", ScriptedLLM(sql_reply("SELECT pg_sleep(1)")))["refused_source"] == "parser"
    out = run("q", ScriptedLLM(sql_reply("SELECT 1")), FakeDB(db.DBError("permission denied", "42501")))
    assert out["refused_source"] == "db" and out["refused_reason"] == "permission_denied"
    ok = run("q", ScriptedLLM(sql_reply("SELECT 1"), "1"), FakeDB(result(["x"], [(1,)])))
    assert ok["refused_source"] is None


def test_summary_failure_is_recorded_separately_and_the_answer_stands():
    model = ScriptedLLM(sql_reply("SELECT 1"), llm.LLMError("timeout"))
    out = run("q", model, FakeDB(result(["x"], [(1,)])))
    assert out["status"] == "answered" and out["error"] is None
    assert out["summary"] is None and "timeout" in out["summary_error"]
    model = ScriptedLLM(sql_reply("SELECT 1"), RuntimeError("weird"))
    out = run("q", model, FakeDB(result(["x"], [(1,)])))
    assert out["status"] == "answered" and "weird" in out["summary_error"]


def test_model_refusal_wording_is_classified():
    assert run("q", ScriptedLLM("REFUSE: I cannot share customer names."))["refused_reason"] == "pii_request"
    assert run("q", ScriptedLLM("REFUSE: I cannot share phone numbers."))["refused_reason"] == "pii_request"
    assert run("q", ScriptedLLM("REFUSE: I can only read data, not change it."))["refused_reason"] == "write_request"


def test_partial_samples_are_not_cached(monkeypatch):
    monkeypatch.setattr(agent, "_samples_cache", None)
    docs = agent.schema_docs.load()
    monkeypatch.setattr(agent, "fetch_samples", lambda d, run=None, n=8: {("fact_orders", "city"): ["x"]})
    agent.get_samples(docs)
    assert agent._samples_cache is None  # a database that was briefly down must not poison the cache
