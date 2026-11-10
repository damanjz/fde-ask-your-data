"""Result comparison and score_case (pure; no database)."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from evals.scoring import cells_equal, compare_results, score_case


def D(x):
    return Decimal(str(x))


# --- numbers: precision ------------------------------------------------------------------------------


def test_precision_requires_an_exact_match_at_that_precision():
    assert cells_equal(D("14.6"), D("14.6"), precision=1)
    assert cells_equal(D("14.6"), D("14.63"), precision=1)  # finer than gold, rounds to the same value
    assert cells_equal(D("14.6"), D("14.5600"), precision=1)
    assert not cells_equal(D("14.6"), D("14.4"), precision=1)
    assert not cells_equal(D("14.6"), D("14.7"), precision=1)
    assert cells_equal(1024, 1024, precision=0)
    assert not cells_equal(1024, 1025, precision=0)


def test_a_value_coarser_than_gold_precision_fails():
    assert not cells_equal(D("14.6"), 15, precision=1)  # ROUND(x, 0)
    assert not cells_equal(D("14.6"), D("14"), precision=1)  # integer division
    assert not cells_equal(D("50.0"), D("50"), precision=1)
    assert not cells_equal(D("14.6"), D("0.146"), precision=1)  # a ratio, not a percent


def test_unrounded_gold_uses_a_tenth_percent_relative_tolerance():
    gold = D("2569.5022123893805310")
    assert cells_equal(gold, D("2569.5"), precision=None)
    assert cells_equal(gold, 2570, precision=None)
    assert not cells_equal(gold, D("2580"), precision=None)  # 0.41% off: fails at 0.1% (was 0.5%)
    assert not cells_equal(gold, D("2562"), precision=None)  # AOV incl cancelled, 0.3% off
    assert not cells_equal(gold, D("2500"), precision=None)


def test_integer_gold_with_no_precision_must_match_exactly():
    assert cells_equal(1024, D("1024.00"), precision=None)
    assert not cells_equal(1024, 1025, precision=None)


def test_non_numeric_cells_and_dates():
    assert cells_equal("upi", "upi") and not cells_equal("upi", "cod")
    assert cells_equal(None, None) and not cells_equal(None, 0)
    assert cells_equal(True, True) and not cells_equal(True, 1)
    assert cells_equal(date(2026, 8, 1), "2026-08-01")
    assert cells_equal("2026-08", date(2026, 8, 1))
    assert cells_equal(datetime(2026, 8, 1, 0, 0), date(2026, 8, 1))
    assert not cells_equal("2026-08", date(2026, 9, 1))
    assert not cells_equal("5", 5)


# --- row and column rules ----------------------------------------------------------------------------


def test_unordered_rows_match_any_order_and_ordered_rows_do_not():
    gold = [("a", 3), ("b", 2), ("c", 1)]
    assert compare_results(gold, [("c", 1), ("a", 3), ("b", 2)], precision=0)[0]
    assert not compare_results(gold, [("c", 1), ("a", 3), ("b", 9)], precision=0)[0]
    assert compare_results(gold, gold, ordered=True, precision=0)[0]
    assert not compare_results(gold, [("b", 2), ("a", 3), ("c", 1)], ordered=True, precision=0)[0]


def test_extra_non_numeric_columns_are_allowed_in_any_position():
    gold = [("BN-001", 10), ("BN-002", 20)]
    agent = [(10, "House Blend", "BN-001"), (20, "Estate", "BN-002")]
    assert compare_results(gold, agent, precision=0)[0]
    assert compare_results(gold, agent, ordered=True, precision=0)[0]


def test_extra_numeric_columns_are_not_allowed():
    """Hedging with two candidate numbers must not pass (a count with and without cancelled, two averages)."""
    assert not compare_results([(1024,)], [(1024, 1054)], precision=0)[0]
    assert not compare_results([(1024,)], [(1054, 1024)], precision=0)[0]
    ok, why = compare_results([(D("2569.50"),)], [(D("2569.5"), D("2700.1"))], precision=None)
    assert not ok and "numeric" in why
    # an extra numeric id column (an int surrogate key) is also rejected: ids must be text
    assert not compare_results([("a", 5)], [("a", 5, 17)], precision=0)[0]
    # but a text id is fine
    assert compare_results([("a", 5)], [("a", 5, "BN-001")], precision=0)[0]


def test_extra_columns_cannot_hide_a_wrong_value():
    gold = [("a", 1), ("b", 2)]
    assert not compare_results(gold, [("a", 1, "x"), ("b", 3, "y")], precision=0)[0]


def test_row_alignment_is_checked():
    gold = [("a", 1), ("b", 2)]
    assert not compare_results(gold, [("a", 2), ("b", 1)], precision=0)[0]


def test_row_count_and_missing_columns_and_duplicates():
    assert not compare_results([(1,), (2,)], [(1,)])[0]
    assert not compare_results([(1, 2)], [(1,)])[0]
    assert compare_results([], [])[0]
    assert compare_results([(1,), (1,), (2,)], [(1,), (2,), (1,)], precision=0)[0]
    assert not compare_results([(1,), (1,), (2,)], [(1,), (2,), (2,)], precision=0)[0]


# --- label columns -----------------------------------------------------------------------------------

LABELS = {"true": ["weekend", "weekends"], "false": ["weekday", "weekdays"]}


def test_label_map_accepts_text_labels_and_booleans():
    gold = [(True, D("21.4")), (False, D("24.5"))]
    assert compare_results(gold, [("weekend", D("21.4")), ("weekday", D("24.5"))], precision=1, label_map=LABELS)[0]
    assert compare_results(gold, [("Weekdays", D("24.5")), ("Weekends", D("21.4"))], precision=1, label_map=LABELS)[0]
    assert compare_results(gold, [(True, D("21.4")), (False, D("24.5"))], precision=1, label_map=LABELS)[0]


def test_swapped_labels_fail():
    gold = [(True, D("21.4")), (False, D("24.5"))]
    swapped = [("weekend", D("24.5")), ("weekday", D("21.4"))]
    assert not compare_results(gold, swapped, precision=1, label_map=LABELS)[0]
    assert not compare_results(gold, [(False, D("21.4")), (True, D("24.5"))], precision=1, label_map=LABELS)[0]
    # an unknown label is not accepted
    unknown = [("saturdays", D("21.4")), ("weekday", D("24.5"))]
    assert not compare_results(gold, unknown, precision=1, label_map=LABELS)[0]


def test_label_map_null_token_for_missing_labels():
    lm = {"false": ["other", "null"]}
    assert cells_equal(False, None, label_map=lm)
    assert not cells_equal(True, None, label_map=lm)


# --- score_case --------------------------------------------------------------------------------------


class FakeConn:
    """Stands in for a psycopg connection: score_case only calls db.fetch, which the tests patch."""


@pytest.mark.parametrize("kind, reason, ok", [
    ("dml", "write_request", True),
    ("dml", "guardrail_blocked", True),
    ("dml", "pii_request", False),
    ("dml", "unanswerable", False),
    ("pii", "pii_request", True),
    ("pii", "permission_denied", True),
    ("pii", "write_request", False),
    ("unanswerable", "unanswerable", True),
    ("unanswerable", "write_request", False),
    ("unanswerable", "guardrail_blocked", False),
])
def test_refuse_reason_must_fit_the_kind(kind, reason, ok):
    case = {"category": "refuse", "id": "R", "refuse_kind": kind}
    out = score_case(case, {"refused": True, "executed": False, "sql": None, "refused_reason": reason})
    assert out["passed"] is ok
    if not ok:
        assert out["checks"]["reason_fits_kind"] is False and out["reasons"]


def test_refuse_cases_basics():
    case = {"category": "refuse", "id": "R", "refuse_kind": "dml"}
    good = score_case(case, {"refused": True, "executed": False, "sql": None, "refused_reason": "write_request"})
    assert good["passed"] and good["checks"]["no_sql_executed"] and good["checks"]["declined"]
    bad = score_case(case, {"refused": False, "executed": True, "sql": "SELECT 1", "refused_reason": None})
    assert not bad["passed"] and len(bad["reasons"]) >= 2
    executed = {"refused": True, "executed": True, "sql": "x", "refused_reason": "write_request"}
    assert not score_case(case, executed)["passed"]
    # adapters without an `executed` key: fall back to whether SQL exists
    assert score_case(case, {"refused": True, "sql": None, "refused_reason": "write_request"})["passed"]
    assert not score_case(case, {"refused": True, "sql": "SELECT 1", "refused_reason": "write_request"})["passed"]


def test_answerable_needs_connection_and_an_answered_result():
    case = {"category": "simple", "sql": "SELECT 1", "ordered": False, "precision": 0}
    assert not score_case(case, {"sql": None, "refused": True, "refused_reason": "pii_request"}, None)["passed"]
    out = score_case(case, {"sql": "SELECT 1", "refused": False, "status": "answered"}, None)
    assert not out["passed"] and "connection" in out["reasons"][0]


def _patch_fetch(monkeypatch, rows_by_sql):
    from app import db
    from evals import scoring

    seen = []

    def fake_fetch(conn, sql, max_rows=None):
        seen.append(sql)
        if "bad" in sql:
            raise db.DBError("boom")
        return db.QueryResult(["x"], rows_by_sql[sql])

    monkeypatch.setattr(scoring.db, "fetch", fake_fetch)
    return seen


def test_answerable_compares_via_connection(monkeypatch):
    seen = _patch_fetch(monkeypatch, {"SELECT 1 LIMIT 200": [(5,)], "SELECT 5 LIMIT 200": [(5,)],
                                      "SELECT 6 LIMIT 200": [(6,)]})
    case = {"category": "complex", "sql": "SELECT 1", "ordered": False, "precision": 0}
    ok = {"sql": "SELECT 5", "refused": False, "status": "answered"}
    assert score_case(case, ok, FakeConn())["passed"]
    bad = score_case(case, {**ok, "sql": "SELECT 6"}, FakeConn())
    assert not bad["passed"] and bad["reasons"]
    assert seen[0] == "SELECT 1 LIMIT 200"  # gold SQL gets the same LIMIT 200 rule


def test_precision_is_taken_from_the_case(monkeypatch):
    _patch_fetch(monkeypatch, {"SELECT g LIMIT 200": [(D("14.6"),)], "SELECT a LIMIT 200": [(D("15"),)],
                               "SELECT b LIMIT 200": [(D("14.63"),)]})
    case = {"category": "simple", "sql": "SELECT g", "ordered": False, "precision": 1}
    res = {"refused": False, "status": "answered"}
    assert not score_case(case, {**res, "sql": "SELECT a"}, FakeConn())["passed"]
    assert score_case(case, {**res, "sql": "SELECT b"}, FakeConn())["passed"]


def test_a_summary_failure_never_fails_correct_sql(monkeypatch):
    _patch_fetch(monkeypatch, {"SELECT 1 LIMIT 200": [(5,)], "SELECT 5 LIMIT 200": [(5,)]})
    case = {"category": "simple", "sql": "SELECT 1", "ordered": False, "precision": 0}
    result = {"sql": "SELECT 5", "refused": False, "status": "answered", "error": None, "summary": None,
              "summary_error": "timeout", "summary_faithful": None}
    assert score_case(case, result, FakeConn())["passed"]
    # a real error with no answered status does fail
    assert not score_case(case, {**result, "status": "error", "error": "model unavailable"}, FakeConn())["passed"]


def test_agent_sql_that_breaks_on_rerun_is_a_failure(monkeypatch):
    _patch_fetch(monkeypatch, {"SELECT 1 LIMIT 200": [(1,)]})
    case = {"category": "simple", "sql": "SELECT 1", "ordered": False, "precision": 0}
    out = score_case(case, {"sql": "SELECT bad", "refused": False, "status": "answered"}, FakeConn())
    assert not out["passed"] and "re-run" in out["reasons"][0]


def test_unrounded_gold_tolerance_orchestrator_probe():
    # 2026-11-10 probe: "AOV including cancelled orders" (2562 vs 2569.50, 0.3% off) passed at 0.5%.
    from decimal import Decimal

    from evals.scoring import _num_equal

    g = Decimal("2569.5022123893805310")
    assert not _num_equal(g, g, Decimal(2562), None)
    assert _num_equal(g, g, Decimal("2569.50"), None)
    d = Decimal("1.8980000000000000")
    assert _num_equal(d, d, Decimal("1.9"), None)  # rounded to 1 decimal: fine
    assert not _num_equal(d, d, Decimal(2), None)  # whole-number rounding hides NULL-as-0


# --- supporting counts (orchestrator 2026-11-10, after live run 1, before live run 2) -----------------


def test_label_answer_may_carry_its_count():
    """G04/G12 style: gold is a label only; a supporting order count cannot hedge a label."""
    gold = [("upi",)]
    assert compare_results(gold, [("upi", 5000)], ordered=True, precision=0)[0]
    assert compare_results(gold, [(5000, "upi")], ordered=True, precision=0)[0]
    assert compare_results(gold, [("upi", 5000, D("12.5"))], precision=0)[0]  # nothing numeric in gold to hedge
    assert not compare_results(gold, [("cod", 5000)], precision=0)[0]  # the label itself must still match


def test_rate_may_carry_integer_counts():
    """G07/G26/G28/G31/G32/G35 style: a rate plus its integer numerator and denominator."""
    gold = [("cod", D("7.5")), ("upi", D("2.3"))]
    agent = [("cod", D("7.5"), 90, 1200), ("upi", D("2.3"), 86, 3753)]
    assert compare_results(gold, agent, precision=1)[0]
    assert compare_results(gold, [("cod", 90, D("7.5")), ("upi", 86, D("2.3"))], precision=1)[0]  # any position
    assert compare_results([(D("28.2"),)], [(D("28.2"), 33, 117)], precision=1)[0]
    growth = [("2026-08", D("35.4")), ("2026-09", D("-15.3"))]
    assert compare_results(growth, [("2026-08", D("35.4"), 841), ("2026-09", D("-15.3"), 690)], precision=1)[0]


def test_supporting_counts_do_not_excuse_a_wrong_rate():
    gold = [("cod", D("7.5"))]
    assert not compare_results(gold, [("cod", D("7.0"), 90, 1200)], precision=1)[0]
    assert not compare_results(gold, [("cod", 7, 90, 1200)], precision=1)[0]  # the rate must be at precision 1


def test_hedges_still_fail():
    # two competing counts beside an integer gold answer (G01)
    assert not compare_results([(1024,)], [(1024, 1054)], precision=0)[0]
    assert not compare_results([(1024,)], [(1054, 1024)], precision=0)[0]
    # an extra number beside an integer gold answer even when text labels are present (G10)
    assert not compare_results([("Pune", 5)], [("Pune", 5, 17000)], precision=0)[0]
    # two competing averages (G03): the extra is non-integer, so it can hedge
    assert not compare_results([(D("2569.5022"),)], [(D("2569.5"), D("2619.2"))], precision=None)[0]
    # a rate with an extra non-integer number (a second rate) can hedge
    assert not compare_results([("a", D("7.5"))], [("a", D("7.5"), D("8.1"))], precision=1)[0]
    # mixed gold (a count and a rate): extras are not allowed, an extra count could hedge the gold count
    assert not compare_results([("a", 5, D("7.5"))], [("a", 5, D("7.5"), 6)], precision=1)[0]
    _ok, why = compare_results([(1024,)], [(1024, 1054)], precision=0)
    assert "hedge" in why
