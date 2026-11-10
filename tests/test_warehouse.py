"""The generator: determinism, referential integrity, arithmetic and the known patterns (pure Python)."""

from collections import Counter, defaultdict
from datetime import date

import pytest

from warehouse import generate as g


@pytest.fixture(scope="module")
def data():
    return g.generate()


def test_deterministic(data):
    again = g.generate()
    for table in g.LOAD_ORDER:
        assert again[table] == data[table], table


def test_different_seed_differs(data):
    assert g.generate(seed=1)["fact_orders"] != data["fact_orders"]


def test_row_counts_match_the_spec(data):
    assert len(data["dim_date"]) == 365
    assert len(data["dim_product"]) == 50
    assert len(data["dim_customer"]) == 2000
    assert 7500 <= len(data["fact_orders"]) <= 8500
    assert len(data["fact_order_items"]) > len(data["fact_orders"])
    assert 300 <= len(data["fact_returns"]) <= 1500
    assert 1000 <= len(data["fact_tickets"]) <= 2500


def test_date_range(data):
    dates = [r["full_date"] for r in data["dim_date"]]
    assert dates[0] == date(2025, 11, 1) and dates[-1] == date(2026, 10, 31)
    assert all(g.START <= o["order_date"] <= g.END for o in data["fact_orders"])


def test_fiscal_calendar(data):
    by = {r["full_date"]: r for r in data["dim_date"]}
    assert (by[date(2026, 4, 1)]["fiscal_year"], by[date(2026, 4, 1)]["fiscal_quarter"]) == ("FY2026-27", 1)
    assert (by[date(2026, 9, 30)]["fiscal_year"], by[date(2026, 9, 30)]["fiscal_quarter"]) == ("FY2026-27", 2)
    assert (by[date(2026, 10, 31)]["fiscal_year"], by[date(2026, 10, 31)]["fiscal_quarter"]) == ("FY2026-27", 3)
    assert (by[date(2026, 3, 31)]["fiscal_year"], by[date(2026, 3, 31)]["fiscal_quarter"]) == ("FY2025-26", 4)
    assert (by[date(2025, 11, 1)]["fiscal_year"], by[date(2025, 11, 1)]["fiscal_quarter"]) == ("FY2025-26", 3)
    assert by[date(2026, 10, 31)]["festival"] == "Diwali Run-up" and by[date(2026, 10, 5)]["festival"] is None
    assert by[date(2026, 8, 1)]["day_name"] == "Saturday" and by[date(2026, 8, 1)]["is_weekend"]


def test_products_reuse_the_support_agent_catalogue(data):
    assert {p["sku"] for p in data["dim_product"]} >= {"GR-005", "BN-001", "KT-004"}
    assert Counter(p["category"] for p in data["dim_product"]) == {
        "beans": 15, "accessory": 12, "brewer": 10, "grinder": 8, "kettle": 5}
    assert all(0 < p["unit_cost_inr"] < p["list_price_inr"] for p in data["dim_product"])


def test_referential_integrity_and_arithmetic(data):
    cust = {c["customer_key"]: c for c in data["dim_customer"]}
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    items_by_order = defaultdict(list)
    for it in data["fact_order_items"]:
        assert it["order_id"] in orders and it["quantity"] >= 1
        assert it["line_total_inr"] == it["quantity"] * it["unit_price_inr"]
        items_by_order[it["order_id"]].append(it)
    assert set(items_by_order) == set(orders)  # every order has at least one line
    for o in orders.values():
        assert o["customer_key"] in cust
        assert o["subtotal_inr"] == sum(i["line_total_inr"] for i in items_by_order[o["order_id"]])
        assert o["total_inr"] == o["subtotal_inr"] - o["discount_inr"] + o["shipping_fee_inr"]
        assert 0 <= o["discount_inr"] <= o["subtotal_inr"]
        assert (o["coupon_code"] is None) == (o["discount_inr"] == 0)
        assert o["order_date"] >= cust[o["customer_key"]]["signup_date"]
        assert (o["status"] == "delivered") == (o["delivery_days"] is not None)
        if o["status"] == "delivered":
            assert o["order_date"].toordinal() + o["delivery_days"] <= g.END.toordinal()
    assert [o["order_id"] for o in data["fact_orders"]] == sorted(orders)


def test_returns_and_tickets_are_consistent(data):
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    items = {i["order_item_id"]: i for i in data["fact_order_items"]}
    seen = set()
    for r in data["fact_returns"]:
        it = items[r["order_item_id"]]
        o = orders[r["order_id"]]
        assert r["order_item_id"] not in seen
        seen.add(r["order_item_id"])
        assert it["order_id"] == r["order_id"] and it["product_key"] == r["product_key"]
        assert o["status"] == "delivered"
        assert o["order_date"] < r["return_date"] <= g.END
        assert 0 < r["refund_amount_inr"] <= it["line_total_inr"]
    for t in data["fact_tickets"]:
        if t["order_id"] is not None:
            assert t["created_date"] >= orders[t["order_id"]]["order_date"]
        assert t["created_date"] <= g.END
        assert t["resolved"] == (t["resolution_hours"] is not None)
        assert t["csat_score"] is None or (t["resolved"] and 1 <= t["csat_score"] <= 5)


def test_no_pii_is_realistic_but_unique_and_synthetic(data):
    emails = [c["email"] for c in data["dim_customer"]]
    assert len(set(emails)) == len(emails)
    assert all(c["phone"].startswith("+91 ") for c in data["dim_customer"])


def _monthly_orders(data):
    cnt = Counter()
    for o in data["fact_orders"]:
        cnt[o["order_date"].strftime("%Y-%m")] += 1
    return cnt


def test_pattern_diwali_run_up_spike(data):
    m = _monthly_orders(data)
    assert m["2026-10"] == max(m.values())
    assert m["2026-10"] > 1.5 * m["2026-05"]
    daily = Counter(o["order_date"] for o in data["fact_orders"])
    last_week = sum(daily[date(2026, 10, d)] for d in range(25, 32)) / 7
    first_week = sum(daily[date(2026, 10, d)] for d in range(1, 8)) / 7
    assert last_week > 1.6 * first_week


def test_pattern_cold_brew_is_a_summer_product(data):
    keys = {p["sku"]: p["product_key"] for p in data["dim_product"]}
    by_month = Counter()
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    for it in data["fact_order_items"]:
        if it["product_key"] == keys["BN-011"]:
            by_month[orders[it["order_id"]]["order_date"].month] += it["quantity"]
    assert sum(by_month[m] for m in (4, 5, 6)) > 2 * sum(by_month[m] for m in (11, 12, 1))


def test_pattern_return_rate_by_category(data):
    cat = {p["product_key"]: p["category"] for p in data["dim_product"]}
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    sold, ret = Counter(), Counter()
    for it in data["fact_order_items"]:
        if orders[it["order_id"]]["status"] == "delivered":
            sold[cat[it["product_key"]]] += 1
    for r in data["fact_returns"]:
        ret[cat[r["product_key"]]] += 1
    rate = {c: ret[c] / sold[c] for c in sold}
    assert rate["grinder"] > 3 * rate["beans"] and rate["kettle"] > 3 * rate["beans"]
    assert min(rate["grinder"], rate["kettle"]) > rate["brewer"] > rate["beans"]


def test_pattern_defect_products_lead_the_return_rate(data):
    sku = {p["product_key"]: p["sku"] for p in data["dim_product"]}
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    sold, ret = Counter(), Counter()
    for it in data["fact_order_items"]:
        if orders[it["order_id"]]["status"] == "delivered":
            sold[sku[it["product_key"]]] += 1
    for r in data["fact_returns"]:
        ret[sku[r["product_key"]]] += 1
    rates = {s: ret[s] / n for s, n in sold.items() if n >= 30}
    ranked = sorted(rates, key=rates.get, reverse=True)
    assert ranked[0] == "GR-005" and rates["GR-005"] > rates[ranked[1]] + 0.05
    assert "KT-004" in ranked[:3]


def test_pattern_ticket_backlog_spike_in_august_2026(data):
    by_month = Counter(t["created_date"].strftime("%Y-%m") for t in data["fact_tickets"])
    others = [n for m, n in by_month.items() if m != "2026-08"]
    assert by_month["2026-08"] > 2.5 * (sum(others) / len(others))
    aug = [t for t in data["fact_tickets"] if t["created_date"].strftime("%Y-%m") == "2026-08"]
    rest = [t for t in data["fact_tickets"] if t["created_date"].strftime("%Y-%m") in ("2026-06", "2026-07")]
    mean = lambda ts, k: sum(t[k] for t in ts) / len(ts)
    assert mean(aug, "first_response_hours") > 2.5 * mean(rest, "first_response_hours")
    assert sum(not t["resolved"] for t in aug) > 3 * sum(not t["resolved"] for t in rest)
    assert Counter(t["category"] for t in aug).most_common(1)[0][0] == "delivery"


def test_pattern_regional_differences(data):
    tier2 = {c[0] for c in g.CITIES if c[3] == 2}
    cod = {True: [0, 0], False: [0, 0]}
    for o in data["fact_orders"]:
        bucket = cod[o["city"] in tier2]
        bucket[1] += 1
        bucket[0] += o["payment_method"] == "cod"
    assert cod[True][0] / cod[True][1] > 2 * cod[False][0] / cod[False][1]
    # Bengaluru's best-selling bean is a south-Indian estate coffee; Delhi's is the house blend
    sku = {p["product_key"]: p["sku"] for p in data["dim_product"]}
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    rev = defaultdict(Counter)
    for it in data["fact_order_items"]:
        city = orders[it["order_id"]]["city"]
        if city in ("Bengaluru", "Delhi") and sku[it["product_key"]].startswith("BN"):
            rev[city][sku[it["product_key"]]] += it["line_total_inr"]
    assert rev["Bengaluru"].most_common(1)[0][0] == "BN-002"
    assert rev["Delhi"].most_common(1)[0][0] == "BN-012"


def test_pattern_beans_got_5_percent_dearer_in_april(data):
    keys = {p["sku"]: p["product_key"] for p in data["dim_product"]}
    orders = {o["order_id"]: o for o in data["fact_orders"]}
    prices = {(orders[i["order_id"]]["order_date"] >= date(2026, 4, 1)): i["unit_price_inr"]
              for i in data["fact_order_items"] if i["product_key"] == keys["BN-001"]}
    assert prices[False] == 475 and prices[True] == 499


def test_csv_round_trip(tmp_path, data):
    manifest = g.write_csv(data, tmp_path)
    assert manifest["fact_orders"]["rows"] == len(data["fact_orders"])
    header = (tmp_path / "dim_customer.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(g.TABLE_COLUMNS["dim_customer"])
    assert g.write_csv(data, tmp_path) == manifest  # same bytes on a second write
