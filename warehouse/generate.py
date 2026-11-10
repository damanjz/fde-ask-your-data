"""Deterministic generator for the simulated Larkfield Coffee Co. BI warehouse.

Twelve months (2025-11-01 to 2026-10-31) of a star schema. Everything is seeded, so two runs give
byte-identical CSVs. All people, contact details and numbers are synthetic.

Known patterns baked in (the gold questions ask about them):
  * Seasonality: Black Friday, Christmas, Republic Day, Independence Day, Raksha Bandhan and a steep
    pre-Diwali gifting ramp from 12 Oct 2026 (Diwali itself is 8 Nov 2026, after the data ends).
    Cold brew peaks in summer; kettles and electric brewers in winter; grinders are gifts.
  * Regions: south Indian cities favour estate beans, north/west metros favour espresso gear; tier-2
    cities pay cash on delivery far more and see more returns and cancellations.
  * Returns: grinders and kettles return far more than beans. GR-005 (from Feb 2026) and KT-004 (from
    Apr 2026) have a defect problem.
  * Ticket backlog spike: a delivery slowdown on standard shipping, 3-24 Aug 2026, drives a delivery
    ticket spike, slow first responses and a pile of unresolved tickets.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from pathlib import Path

SEED = 20261110
START = date(2025, 11, 1)
END = date(2026, 10, 31)
TARGET_ORDERS = 8000
N_CUSTOMERS = 2000
N_PRE_WINDOW = 1150  # customers who signed up before the window

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE / "products.json"
OUT_DIR = HERE / "out"

# city, state, weight, tier, has_store
CITIES = [
    ("Bengaluru", "Karnataka", 17, 1, True),
    ("Mumbai", "Maharashtra", 12, 1, True),
    ("Delhi", "Delhi", 11, 1, False),
    ("Hyderabad", "Telangana", 9, 1, True),
    ("Chennai", "Tamil Nadu", 8, 1, False),
    ("Pune", "Maharashtra", 7, 1, False),
    ("Kolkata", "West Bengal", 5, 1, False),
    ("Ahmedabad", "Gujarat", 4, 2, False),
    ("Kochi", "Kerala", 4, 2, False),
    ("Jaipur", "Rajasthan", 3, 2, False),
    ("Chandigarh", "Chandigarh", 3, 2, False),
    ("Coimbatore", "Tamil Nadu", 3, 2, False),
    ("Mysuru", "Karnataka", 3, 2, False),
    ("Gurugram", "Haryana", 3, 1, False),
    ("Noida", "Uttar Pradesh", 2.5, 1, False),
    ("Lucknow", "Uttar Pradesh", 2.5, 2, False),
    ("Indore", "Madhya Pradesh", 2.5, 2, False),
    ("Nagpur", "Maharashtra", 2, 2, False),
    ("Visakhapatnam", "Andhra Pradesh", 2, 2, False),
    ("Bhubaneswar", "Odisha", 1.5, 2, False),
]
SOUTH = {"Bengaluru", "Hyderabad", "Chennai", "Kochi", "Coimbatore", "Mysuru", "Visakhapatnam"}
NORTH_WEST = {"Delhi", "Mumbai", "Gurugram", "Noida", "Chandigarh", "Pune"}

SIGNUP_CHANNELS = [
    ("organic_search", 26, 1.0),
    ("instagram", 20, 0.8),
    ("google_ads", 17, 0.85),
    ("referral", 13, 1.4),
    ("marketplace", 10, 0.6),
    ("email_campaign", 8, 1.3),
    ("offline_store", 6, 1.2),
]

FIRST = ["Aarav", "Aditya", "Akash", "Amit", "Ankit", "Arjun", "Arnav", "Bhavesh", "Chirag", "Deepak", "Dhruv", "Gaurav", "Harsh", "Ishaan", "Karan", "Kunal", "Manish", "Mohit", "Naveen", "Nikhil", "Pranav", "Rahul", "Rajat", "Rohan", "Sahil", "Sameer", "Sandeep", "Siddharth", "Suresh", "Tarun", "Varun", "Vikram", "Vivek", "Yash", "Aanya", "Aditi", "Ananya", "Anjali", "Deepa", "Divya", "Isha", "Kavya", "Kiran", "Lakshmi", "Meera", "Neha", "Nisha", "Pooja", "Priya", "Radhika", "Riya", "Sanya", "Shreya", "Sneha", "Sunita", "Swati", "Tanvi", "Uma", "Vidya", "Zoya"]
LAST = ["Agarwal", "Banerjee", "Bhat", "Chatterjee", "Chopra", "Desai", "Dutta", "Gupta", "Iyer", "Jain", "Joshi", "Kapoor", "Khan", "Krishnan", "Kulkarni", "Malhotra", "Mehta", "Menon", "Mishra", "Nair", "Naidu", "Pandey", "Patel", "Pillai", "Rao", "Reddy", "Saxena", "Sen", "Shah", "Sharma", "Singh", "Sinha", "Srinivasan", "Thakur", "Trivedi", "Varma", "Verma", "Yadav", "Bose", "Das", "Ghosh", "Hegde", "Shetty", "Gowda", "Kamath"]
EMAIL_DOMAINS = ["gmail.com"] * 6 + ["yahoo.in", "outlook.com", "rediffmail.com", "proton.me"]

MONTH_FACTOR = {11: 0.95, 12: 1.10, 1: 0.95, 2: 0.88, 3: 0.85, 4: 0.78, 5: 0.74, 6: 0.78,
                7: 0.88, 8: 0.98, 9: 0.95, 10: 1.10}
DOW_FACTOR = [0.95, 0.90, 0.92, 0.95, 1.05, 1.20, 1.15]  # Mon..Sun

# name, start, end, order multiplier
FESTIVALS = [
    ("Black Friday Sale", date(2025, 11, 27), date(2025, 11, 30), 2.0),
    ("Christmas and New Year", date(2025, 12, 20), date(2026, 1, 2), 1.35),
    ("Republic Day Sale", date(2026, 1, 22), date(2026, 1, 26), 1.5),
    ("Holi", date(2026, 3, 2), date(2026, 3, 4), 1.2),
    ("Independence Day Sale", date(2026, 8, 10), date(2026, 8, 15), 1.5),
    ("Raksha Bandhan", date(2026, 8, 22), date(2026, 8, 28), 1.35),
    ("Ganesh Chaturthi", date(2026, 9, 11), date(2026, 9, 14), 1.1),
    ("Diwali Run-up", date(2026, 10, 12), date(2026, 10, 31), 1.0),  # ramp handled in season()
]
DIWALI_RAMP_START = date(2026, 10, 12)
DELAY_START, DELAY_END = date(2026, 8, 3), date(2026, 8, 24)  # standard-shipping slowdown
SPIKE_START, SPIKE_END = date(2026, 8, 3), date(2026, 9, 5)  # tickets created in this span are slow
PRICE_RISE = date(2026, 4, 1)  # beans +5% from here

# base popularity (relative order-line weights)
BASE_W = {
    "BN-001": 14, "BN-002": 10, "BN-003": 7, "BN-004": 6, "BN-005": 6, "BN-006": 5, "BN-007": 8,
    "BN-008": 5, "BN-009": 4, "BN-010": 2, "BN-011": 4, "BN-012": 5, "BN-013": 4, "BN-014": 5, "BN-015": 6,
    "BR-001": 3, "BR-002": 1.5, "BR-003": 2.5, "BR-004": 1.5, "BR-005": 1.5, "BR-006": 2, "BR-007": 1.5,
    "BR-008": 0.8, "BR-009": 2, "BR-010": 0.25,
    "GR-001": 1.3, "GR-002": 1.5, "GR-003": 1.0, "GR-004": 0.6, "GR-005": 1.3, "GR-006": 0.15,
    "GR-007": 1.2, "GR-008": 0.3,
    "KT-001": 0.9, "KT-002": 0.9, "KT-003": 0.5, "KT-004": 0.55, "KT-005": 0.6,
    "AC-001": 1.5, "AC-002": 1.5, "AC-003": 3, "AC-004": 1.2, "AC-005": 0.5, "AC-006": 1.5,
    "AC-007": 1.3, "AC-008": 1, "AC-009": 0.6, "AC-010": 1, "AC-011": 0.5, "AC-012": 1,
}
COST_RATIO = {"beans": (0.42, 0.50), "grinder": (0.50, 0.60), "brewer": (0.45, 0.58),
              "kettle": (0.50, 0.60), "accessory": (0.35, 0.48)}
COLD_BREW = {"BN-011", "BR-009"}
COLD_BREW_M = {4: 2.5, 5: 3.2, 6: 3.0, 3: 1.6, 7: 1.6, 8: 1.2, 9: 1.0}
GIFT_SKUS = {"BN-013", "AC-006", "AC-007", "AC-010", "KT-004", "BR-010"}
SOUTH_BEANS = {"BN-002", "BN-003", "BN-005", "BN-006"}
ESPRESSO_SKUS = {"BN-007", "BN-015", "BN-008", "BN-009"}

RETURN_BASE = {"grinder": 0.13, "kettle": 0.10, "brewer": 0.075, "accessory": 0.045, "beans": 0.022}
RETURN_REASONS = {
    "beans": (["quality_issue", "wrong_item", "damaged_in_transit", "changed_mind"], [40, 20, 15, 25]),
    "hardware": (["defective", "damaged_in_transit", "changed_mind", "not_as_described", "wrong_item"],
                 [45, 20, 20, 10, 5]),
}
TICKET_CHANNELS = (["email", "chat", "whatsapp", "phone"], [38, 27, 20, 15])
FIRST_RESP_MEDIAN_H = {"phone": 0.2, "chat": 0.4, "whatsapp": 2.5, "email": 7.0}
RESOLUTION_MEDIAN_H = {"product_defect": 72, "refund": 48, "delivery": 24, "payment": 12, "how_to": 6,
                       "account": 4, "other": 24}


# --- helpers -----------------------------------------------------------------------------------------


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def fiscal(d: date) -> tuple[str, int]:
    start_year = d.year if d.month >= 4 else d.year - 1
    q = ((d.month - 4) % 12) // 3 + 1
    return f"FY{start_year}-{str(start_year + 1)[2:]}", q


def festival_for(d: date) -> str | None:
    for name, s, e, _ in FESTIVALS:
        if s <= d <= e:
            return name
    return None


def ramp(d: date) -> float:
    """0 before 12 Oct 2026, rising to 1 on 31 Oct (pre-Diwali gifting run-up)."""
    if d < DIWALI_RAMP_START:
        return 0.0
    return ((d - DIWALI_RAMP_START).days / 19) ** 1.4


def season(d: date) -> float:
    s = MONTH_FACTOR[d.month] * DOW_FACTOR[d.weekday()]
    s *= 1 + 0.12 * (d - START).days / 365  # business growth
    for name, a, b, mult in FESTIVALS:
        if a <= d <= b and name != "Diwali Run-up":
            s *= mult
    if d.month == 11 and d.year == 2025 and d.day <= 10:
        s *= 0.9  # post-Diwali lull
    s *= 1 + 1.6 * ramp(d)
    return s


def poisson(rng: random.Random, lam: float) -> int:
    if lam < 30:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1
    return max(0, round(rng.gauss(lam, math.sqrt(lam))))


def weighted(rng: random.Random, items: list, weights: list):
    return rng.choices(items, weights=weights, k=1)[0]


# --- dimensions --------------------------------------------------------------------------------------


def build_dim_date() -> list[dict]:
    rows = []
    for d in daterange(START, END):
        fy, fq = fiscal(d)
        rows.append({
            "full_date": d, "year": d.year, "quarter": (d.month - 1) // 3 + 1, "month": d.month,
            "month_name": d.strftime("%B"), "year_month": d.strftime("%Y-%m"),
            "week_of_year": d.isocalendar()[1], "day_of_week": d.isoweekday(), "day_name": d.strftime("%A"),
            "is_weekend": d.isoweekday() >= 6, "fiscal_year": fy, "fiscal_quarter": fq,
            "festival": festival_for(d),
        })
    return rows


def build_dim_product(rng: random.Random) -> list[dict]:
    raw = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    rows = []
    for i, p in enumerate(raw, start=1):
        lo, hi = COST_RATIO[p["category"]]
        rows.append({
            "product_key": i, "sku": p["sku"], "product_name": p["name"], "category": p["category"],
            "list_price_inr": p["price_inr"], "unit_cost_inr": round(p["price_inr"] * rng.uniform(lo, hi)),
            "warranty_months": p["warranty_months"],
        })
    return rows


def _phone(rng: random.Random) -> str:
    return f"+91 {rng.choice('6789')}{rng.randint(0, 9999):04d}{rng.randint(0, 99999):05d}"


def build_customers(rng: random.Random, days: list[date]) -> list[dict]:
    cw = [c[2] for c in CITIES]
    season_w = [season(d) for d in days]
    emails: set[str] = set()
    customers = []
    for i in range(N_CUSTOMERS):
        in_window = i >= N_PRE_WINDOW
        city, state, _, _, store = weighted(rng, CITIES, cw)
        chans = [c for c in SIGNUP_CHANNELS if c[0] != "offline_store" or store]
        ch = weighted(rng, chans, [c[1] for c in chans])
        if in_window:
            signup = rng.choices(days, weights=season_w, k=1)[0]
        else:
            span = (START - date(2023, 1, 1)).days - 1
            signup = date(2023, 1, 1) + timedelta(days=int(span * rng.triangular(0, 1, 0.85)))
        first, last = rng.choice(FIRST), rng.choice(LAST)
        email = f"{first}.{last}{rng.randint(1, 99)}@{rng.choice(EMAIL_DOMAINS)}".lower()
        while email in emails:
            email = f"{first}.{last}{rng.randint(1, 9999)}@{rng.choice(EMAIL_DOMAINS)}".lower()
        emails.add(email)
        customers.append({
            "name": f"{first} {last}", "email": email, "phone": _phone(rng), "city": city, "state": state,
            "signup_date": signup, "signup_channel": ch[0], "weight": rng.lognormvariate(0, 0.9) * ch[2],
            "in_window": in_window,
        })
    customers.sort(key=lambda c: (c["signup_date"], c["email"]))
    for i, c in enumerate(customers, start=1):
        c["customer_key"] = i
    return customers


# --- facts -------------------------------------------------------------------------------------------


def gift_prob(d: date) -> float:
    p = 0.04 + 0.14 * ramp(d)
    for name, a, b, _ in FESTIVALS:
        if a <= d <= b and name in ("Raksha Bandhan", "Christmas and New Year"):
            p = max(p, 0.12)
    return p


def product_weight(p: dict, d: date, city: str, gift: bool) -> float:
    sku, cat = p["sku"], p["category"]
    w = BASE_W[sku]
    if sku in COLD_BREW:
        w *= COLD_BREW_M.get(d.month, 0.4 if d.month in (11, 12, 1, 2) else 0.8)
    if cat == "kettle" or sku in ("BR-008", "BR-010"):
        w *= 1.5 if d.month in (11, 12, 1, 2) else (0.65 if d.month in (4, 5, 6) else 1.0)
    if cat == "grinder" or sku in GIFT_SKUS:
        w *= 1 + 1.4 * ramp(d)
        if date(2025, 12, 20) <= d <= date(2026, 1, 2):
            w *= 1.8
        if date(2026, 8, 22) <= d <= date(2026, 8, 28) and sku in ("BN-013", "AC-006", "AC-007"):
            w *= 1.7
    if date(2025, 11, 27) <= d <= date(2025, 11, 30) and cat in ("grinder", "kettle", "brewer"):
        w *= 1.8
    if city in SOUTH and sku in SOUTH_BEANS:
        w *= 1.8
    if city == "Bengaluru" and sku == "BN-002":
        w *= 1.3
    if city in NORTH_WEST and (sku in ESPRESSO_SKUS or sku == "BR-010"):
        w *= 1.5 if sku != "BR-010" else 2.0
    if city in NORTH_WEST and cat == "kettle":
        w *= 1.3
    if gift and (cat == "grinder" or sku in GIFT_SKUS):
        w *= 2.0
    return w


def unit_price(p: dict, d: date) -> int:
    if p["category"] == "beans" and d < PRICE_RISE:
        return round(p["list_price_inr"] / 1.05)
    return p["list_price_inr"]


COUPONS = [  # code, start, end, rate, base prob
    ("BLACKFRI20", date(2025, 11, 27), date(2025, 11, 30), 0.20, 0.55),
    ("NEWYEAR10", date(2025, 12, 24), date(2026, 1, 2), 0.10, 0.40),
    ("REPUBLIC12", date(2026, 1, 22), date(2026, 1, 26), 0.12, 0.45),
    ("HOLI10", date(2026, 3, 1), date(2026, 3, 4), 0.10, 0.40),
    ("FREEDOM15", date(2026, 8, 10), date(2026, 8, 15), 0.15, 0.50),
    ("RAKHI10", date(2026, 8, 22), date(2026, 8, 28), 0.10, 0.40),
    ("DIWALI15", date(2026, 10, 12), date(2026, 10, 31), 0.15, 0.30),
]


def pick_coupon(rng: random.Random, d: date, first_order: bool, signup_channel: str, subtotal: int):
    code, amount = None, 0
    if first_order and signup_channel == "referral" and rng.random() < 0.6:
        return "REFER100", min(100, subtotal)
    if first_order and rng.random() < 0.45:
        code, rate = "WELCOME10", 0.10
    else:
        for c, a, b, rate, prob in COUPONS:
            if a <= d <= b:
                p = prob + (0.25 * ramp(d) if c == "DIWALI15" else 0)
                if rng.random() < p:
                    code = c
                break
        if code is None:
            if signup_channel == "email_campaign" and rng.random() < 0.25:
                code, rate = "MAILER5", 0.05
            elif rng.random() < 0.06:
                code, rate = "LOYAL5", 0.05
    if code is None:
        return None, 0
    amount = min(round(subtotal * rate), 1500)
    return code, amount


def pick_payment(rng: random.Random, tier: int, channel: str) -> str:
    if channel == "retail_store":
        return weighted(rng, ["upi", "credit_card", "debit_card", "wallet"], [55, 25, 15, 5])
    methods = ["upi", "credit_card", "debit_card", "cod", "netbanking", "wallet"]
    ws = [48, 20, 9, 10, 6, 7] if tier == 1 else [40, 8, 10, 32, 5, 5]
    return weighted(rng, methods, ws)


def delivery_days(rng: random.Random, d: date, ship: str, tier: int) -> int:
    if ship == "in_store":
        return 0
    if ship == "express":
        return weighted(rng, [1, 2, 3], [30, 50, 20])
    base = weighted(rng, [2, 3, 4, 5, 6, 7, 8], [8, 22, 28, 20, 12, 6, 4]) + (1 if tier == 2 else 0)
    if DELAY_START <= d <= DELAY_END:
        base += rng.randint(3, 6)
    if d in (date(2025, 11, 28), date(2025, 11, 29)) or d >= date(2026, 10, 20):
        base += 1
    return base


def build_orders(rng: random.Random, customers: list[dict], products: list[dict], days: list[date]):
    city_info = {c[0]: c for c in CITIES}
    city_names = [c[0] for c in CITIES]
    city_w = [c[2] for c in CITIES]
    base = TARGET_ORDERS / sum(season(d) for d in days)

    new_by_day: dict[date, list[dict]] = {d: [] for d in days}
    for c in customers:
        if c["in_window"]:
            new_by_day[c["signup_date"]].append(c)

    sig_dates = [c["signup_date"] for c in customers]
    cum, run = [], 0.0
    for c in customers:
        run += c["weight"]
        cum.append(run)

    orders, items = [], []
    for d in days:
        news = new_by_day[d]
        n_exist = max(0, poisson(rng, base * season(d)) - len(news))
        k = bisect_left(sig_dates, d)  # customers who signed up strictly before d
        buyers = [(c, True) for c in news]
        if k:
            for _ in range(n_exist):
                buyers.append((customers[bisect_right(cum, rng.random() * cum[k - 1], 0, k)], False))
        rng.shuffle(buyers)
        for cust, first_order in buyers:
            o, its = make_order(rng, d, cust, first_order, city_info, city_names, city_w, products)
            orders.append(o)
            items.append(its)
    for oid, (o, its) in enumerate(zip(orders, items, strict=True), start=1):
        o["order_id"] = oid
        for it in its:
            it["order_id"] = oid
    flat = []
    for its in items:
        flat.extend(its)
    for iid, it in enumerate(flat, start=1):
        it["order_item_id"] = iid
    return orders, flat


def make_order(rng, d, cust, first_order, city_info, city_names, city_w, products):
    city, state = cust["city"], cust["state"]
    gifted = rng.random() < gift_prob(d)
    if gifted:
        city = weighted(rng, city_names, city_w)
        state = city_info[city][1]
    tier, store = city_info[city][3], city_info[city][4]

    chans = ["website", "mobile_app", "marketplace", "retail_store"]
    cw = [40, 27, 18, 15 if store and not gifted else 0]
    if cust["signup_channel"] == "marketplace":
        cw[2] = 70
    channel = weighted(rng, chans, cw)

    n_items = weighted(rng, [1, 2, 3, 4], [58, 27, 11, 4])
    chosen: list[dict] = []
    weights = [product_weight(p, d, city, gifted) for p in products]
    for _ in range(n_items):
        for _try in range(5):
            p = weighted(rng, products, weights)
            if p not in chosen:
                chosen.append(p)
                break
    if any(p["category"] != "beans" and p["category"] != "accessory" for p in chosen) \
            and not any(p["category"] == "beans" for p in chosen) and rng.random() < 0.55:
        bean_w = [w if p["category"] == "beans" else 0 for p, w in zip(products, weights, strict=True)]
        chosen.append(weighted(rng, products, bean_w))
    its = []
    for p in chosen:
        if p["category"] == "beans":
            q = weighted(rng, [1, 2, 3, 4], [55, 28, 12, 5])
        else:
            q = 1 if rng.random() < 0.95 else 2
        price = unit_price(p, d)
        its.append({"product_key": p["product_key"], "sku": p["sku"], "category": p["category"],
                    "quantity": q, "unit_price_inr": price, "line_total_inr": q * price})
    subtotal = sum(i["line_total_inr"] for i in its)

    coupon, discount = pick_coupon(rng, d, first_order, cust["signup_channel"], subtotal)
    payment = pick_payment(rng, tier, channel)
    if channel == "retail_store":
        ship, fee = "in_store", 0
    else:
        express_p = 0.30 if tier == 1 else 0.12
        ship = "express" if rng.random() < express_p else "standard"
        fee = 149 if ship == "express" else (0 if subtotal >= 999 else 60)
    days_to_deliver = delivery_days(rng, d, ship, tier)

    cancel_p = 0.065 if payment == "cod" else 0.018
    if ship == "standard" and DELAY_START <= d <= DELAY_END:
        cancel_p *= 2.5
    if rng.random() < cancel_p:
        status, dd = "cancelled", None
    elif d + timedelta(days=days_to_deliver) > END:
        status, dd = "shipped", None
    else:
        status, dd = "delivered", days_to_deliver

    order = {
        "customer_key": cust["customer_key"], "order_date": d, "channel": channel, "city": city,
        "state": state, "payment_method": payment, "shipping_method": ship, "coupon_code": coupon,
        "subtotal_inr": subtotal, "discount_inr": discount, "shipping_fee_inr": fee,
        "total_inr": subtotal - discount + fee, "status": status, "delivery_days": dd,
        "_gifted": gifted, "_tier": tier, "_signup_channel": cust["signup_channel"],
    }
    return order, its


def build_returns(rng: random.Random, orders: list[dict], items: list[dict]) -> list[dict]:
    by_order = {o["order_id"]: o for o in orders}
    returns = []
    for it in items:
        o = by_order[it["order_id"]]
        if o["status"] != "delivered":
            continue
        d, sku, cat = o["order_date"], it["sku"], it["category"]
        p = RETURN_BASE[cat]
        defect_story = (sku == "GR-005" and d >= date(2026, 2, 1)) or (sku == "KT-004" and d >= date(2026, 4, 1))
        if defect_story:
            p = 0.32 if sku == "GR-005" else 0.26
        if o["payment_method"] == "cod":
            p *= 1.4
        if o["channel"] == "marketplace":
            p *= 1.25
        if o["channel"] == "retail_store":
            p *= 0.5
        if o["_gifted"]:
            p *= 1.3
        if o["_tier"] == 2:
            p *= 1.15
        if rng.random() >= min(p, 0.6):
            continue
        if defect_story and rng.random() < 0.8:
            reason = "defective"
        else:
            pool, ws = RETURN_REASONS["beans" if cat == "beans" else "hardware"]
            reason = weighted(rng, pool, ws)
        delivered = d + timedelta(days=o["delivery_days"])
        rdate = delivered + timedelta(days=max(1, round(rng.lognormvariate(1.5, 0.55))))
        if rdate > END:
            continue
        share = it["line_total_inr"] * o["discount_inr"] / o["subtotal_inr"] if o["subtotal_inr"] else 0
        refund = it["line_total_inr"] - share
        if reason == "changed_mind":
            refund *= 0.9
        returns.append({
            "order_item_id": it["order_item_id"], "order_id": o["order_id"],
            "product_key": it["product_key"], "return_date": rdate, "reason": reason,
            "refund_amount_inr": round(refund),
        })
    returns.sort(key=lambda r: (r["return_date"], r["order_item_id"]))
    for i, r in enumerate(returns, start=1):
        r["return_id"] = i
    return returns


def build_tickets(rng, orders, items, returns, customers) -> list[dict]:
    hardware_orders = {i["order_id"] for i in items if i["category"] in ("grinder", "brewer", "kettle")}
    ret_by_order: dict[int, list[dict]] = {}
    for r in returns:
        ret_by_order.setdefault(r["order_id"], []).append(r)
    raw: list[tuple[date, int, int | None, str]] = []  # created, customer_key, order_id, category

    def add(day: date, o: dict, cat: str):
        if day <= END:
            raw.append((day, o["customer_key"], o["order_id"], cat))

    for o in orders:
        d, oid = o["order_date"], o["order_id"]
        dd = o["delivery_days"]
        if o["status"] == "cancelled":
            if rng.random() < 0.10:
                add(d + timedelta(days=rng.randint(0, 3)), o, "refund")
            continue
        slow_std = o["shipping_method"] == "standard" and DELAY_START <= d <= DELAY_END
        horizon = dd if dd is not None else max(1, (END - d).days)
        if slow_std:
            p_del = 0.60
        elif dd is not None and dd > 6:
            p_del = 0.30
        else:
            p_del = 0.035
        if o["shipping_method"] != "in_store" and rng.random() < p_del:
            add(d + timedelta(days=rng.randint(1, max(1, horizon))), o, "delivery")
        for r in ret_by_order.get(oid, []):
            cat = "product_defect" if r["reason"] == "defective" else "refund"
            if rng.random() < (0.7 if cat == "product_defect" else 0.45):
                add(r["return_date"] - timedelta(days=rng.randint(0, 2)), o, cat)
        if o["payment_method"] in ("netbanking", "wallet", "upi") and rng.random() < 0.02:
            add(d + timedelta(days=rng.randint(0, 1)), o, "payment")
        if dd is not None and oid in hardware_orders and rng.random() < 0.12:
            add(d + timedelta(days=dd + rng.randint(0, 3)), o, "how_to")
    # tickets not tied to an order
    for _ in range(150):
        c = customers[rng.randrange(len(customers))]
        day = START + timedelta(days=rng.randrange((END - START).days + 1))
        if day >= c["signup_date"]:
            raw.append((day, c["customer_key"], None, rng.choice(["account", "account", "other"])))
    for _ in range(60):
        c = customers[rng.randrange(len(customers))]
        day = START + timedelta(days=rng.randrange((END - START).days + 1))
        if day >= c["signup_date"]:
            raw.append((day, c["customer_key"], None, "other"))
    raw.sort(key=lambda t: (t[0], t[1], t[2] or 0, t[3]))

    tickets = []
    for i, (day, ckey, oid, cat) in enumerate(raw, start=1):
        spike = SPIKE_START <= day <= SPIKE_END
        channel = weighted(rng, *TICKET_CHANNELS)
        frt = FIRST_RESP_MEDIAN_H[channel] * rng.lognormvariate(0, 0.6) * (4.0 if spike else 1.0)
        res = RESOLUTION_MEDIAN_H[cat] * rng.lognormvariate(0, 0.7) * (3.0 if spike else 1.0)
        hour = rng.uniform(0, 24)
        deadline = (END + timedelta(days=1) - day).total_seconds() / 3600 - hour
        stuck = rng.random() < (0.12 if spike else 0.02)
        resolved = (not stuck) and res <= deadline
        csat = None
        if resolved and rng.random() < 0.55:
            center = 4.3 - (1.2 if spike else 0.0) - min(1.0, frt / 40)
            csat = int(min(5, max(1, round(rng.gauss(center, 0.9)))))
        tickets.append({
            "ticket_id": i, "customer_key": ckey, "order_id": oid, "created_date": day, "category": cat,
            "channel": channel, "first_response_hours": round(frt, 1), "resolved": resolved,
            "resolution_hours": round(res, 1) if resolved else None, "csat_score": csat,
        })
    return tickets


# --- assemble ----------------------------------------------------------------------------------------

TABLE_COLUMNS = {
    "dim_date": ["full_date", "year", "quarter", "month", "month_name", "year_month", "week_of_year",
                 "day_of_week", "day_name", "is_weekend", "fiscal_year", "fiscal_quarter", "festival"],
    "dim_product": ["product_key", "sku", "product_name", "category", "list_price_inr", "unit_cost_inr",
                    "warranty_months"],
    "dim_customer": ["customer_key", "name", "email", "phone", "city", "state", "signup_date",
                     "signup_channel"],
    "fact_orders": ["order_id", "customer_key", "order_date", "channel", "city", "state", "payment_method",
                    "shipping_method", "coupon_code", "subtotal_inr", "discount_inr", "shipping_fee_inr",
                    "total_inr", "status", "delivery_days"],
    "fact_order_items": ["order_item_id", "order_id", "product_key", "quantity", "unit_price_inr",
                         "line_total_inr"],
    "fact_returns": ["return_id", "order_item_id", "order_id", "product_key", "return_date", "reason",
                     "refund_amount_inr"],
    "fact_tickets": ["ticket_id", "customer_key", "order_id", "created_date", "category", "channel",
                     "first_response_hours", "resolved", "resolution_hours", "csat_score"],
}
LOAD_ORDER = list(TABLE_COLUMNS)  # parents first


def generate(seed: int = SEED) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    days = list(daterange(START, END))
    dim_date = build_dim_date()
    dim_product = build_dim_product(rng)
    customers = build_customers(rng, days)
    orders, items = build_orders(rng, customers, dim_product, days)
    returns = build_returns(rng, orders, items)
    tickets = build_tickets(rng, orders, items, returns, customers)
    return {
        "dim_date": dim_date, "dim_product": dim_product, "dim_customer": customers,
        "fact_orders": orders, "fact_order_items": items, "fact_returns": returns, "fact_tickets": tickets,
    }


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def write_csv(data: dict[str, list[dict]], out_dir: Path = OUT_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for table in LOAD_ORDER:
        path = out_dir / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(TABLE_COLUMNS[table])
            for row in data[table]:
                w.writerow([_cell(row[c]) for c in TABLE_COLUMNS[table]])
        manifest[table] = {"rows": len(data[table]), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    data = generate()
    manifest = write_csv(data)
    for table, info in manifest.items():
        print(f"{table:18s} {info['rows']:>7d} rows  {info['sha256'][:12]}")


if __name__ == "__main__":
    main()
