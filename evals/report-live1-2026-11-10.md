# Eval report

Run: 2026-11-10. Model: `ollama:fusion27b:iq3`. Gold items: 40. Today = 2026-11-10.

Variants: agent = schema docs, sample values and examples; ddl = raw CREATE TABLE text plus today's date; floor = table names only. Intervals are Wilson 95% intervals; with 40 questions they are wide, so small differences are not evidence.

## Execution accuracy by category

| Category | n | agent | ddl | floor |
|---|---|---|---|---|
| simple | 25 | 21/25 (84%, 95% CI 65-94%) | 11/25 (44%, 95% CI 27-63%) | 2/25 (8%, 95% CI 2-25%) |
| complex | 10 | 5/10 (50%, 95% CI 24-76%) | 0/10 (0%, 95% CI 0-28%) | 0/10 (0%, 95% CI 0-28%) |
| refuse | 5 | 5/5 (100%, 95% CI 57-100%) | 4/5 (80%, 95% CI 38-96%) | 4/5 (80%, 95% CI 38-96%) |
| **overall** | 40 | **31/40 (78%, 95% CI 62-88%)** | **15/40 (38%, 95% CI 24-53%)** | **6/40 (15%, 95% CI 7-29%)** |

Scorer limits: G26 (return rate by category) cannot tell 'lines on delivered orders' from 'lines on non-cancelled orders', because every Jul-Sep order is delivered; G28 (highest return rate) names the same product under several denominators, so only its rate separates them. See the items' `notes`.

## Operations

| Metric | agent | ddl | floor |
|---|---|---|---|
| Guardrail blocks (parser or database) | 0 | 1 | 10 |
| Questions declined | 5 | 5 | 6 |
| Declined by the question pre-filter | 3 | 3 | 3 |
| Declined by the model (REFUSE / UNANSWERABLE) | 2 | 2 | 3 |
| Declined by the SQL parser | 0 | 0 | 0 |
| Declined by the database (privileges) | 0 | 0 | 0 |
| Questions with a failed attempt (guardrail, database or format) | 0 | 1 | 31 |
| Hard errors (no answer, no refusal) | 0 | 0 | 26 |
| Summary call errors (rows still scored) | 0 | 0 | 0 |
| Latency p50 / p95 (ms) | 8308 / 12103 | 7754 / 11854 | 9330 / 18685 |
| Tokens per question (prompt + completion) | 3245 | 1147 | 835 |
| Summary faithfulness (summary passed the number check) | 35/35 (100%) | 33/33 (100%) | 7/7 (100%) |

## Failed questions: agent (9)

### G04 (simple): Which payment method did customers use most last quarter?

Reasons: agent returned 1 numeric columns, gold has 0; extra columns must be non-numeric

Agent SQL:

```sql
SELECT payment_method, COUNT(*) AS orders FROM fact_orders WHERE order_date BETWEEN '2026-07-01' AND '2026-09-30' AND status <> 'cancelled' GROUP BY payment_method ORDER BY orders DESC LIMIT 1
```

Gold SQL:

```sql
SELECT payment_method FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled' GROUP BY payment_method ORDER BY COUNT(*) DESC, payment_method LIMIT 1
```

### G07 (simple): Cancellation rate by payment method this year

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT payment_method,
       COUNT(*) AS orders,
       SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
       ROUND(100.0 * SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) / COUNT(*), 1) AS cancel_pct
FROM fact_orders
WHERE order_date BETWEEN '2026-04-01' AND '2026-10-31'
GROUP BY payment_method
ORDER BY cancel_pct DESC, payment_method ASC LIMIT 200
```

Gold SQL:

```sql
SELECT payment_method, ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'cancelled') / COUNT(*), 1) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' GROUP BY payment_method
```

### G11 (simple): How much discount did we give on DIWALI15 coupon orders?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT SUM(discount_inr) AS total_discount_inr FROM fact_orders WHERE coupon_code = 'DIWALI15' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(discount_inr) FROM fact_orders o WHERE coupon_code = 'DIWALI15' AND o.status <> 'cancelled'
```

### G12 (simple): Which coupon code was used on the most orders?

Reasons: agent returned 1 numeric columns, gold has 0; extra columns must be non-numeric

Agent SQL:

```sql
SELECT coupon_code, COUNT(*) AS orders FROM fact_orders WHERE coupon_code IS NOT NULL AND status <> 'cancelled' GROUP BY coupon_code ORDER BY orders DESC, coupon_code ASC LIMIT 1
```

Gold SQL:

```sql
SELECT coupon_code FROM fact_orders o WHERE coupon_code IS NOT NULL AND o.status <> 'cancelled' GROUP BY coupon_code ORDER BY COUNT(*) DESC, coupon_code LIMIT 1
```

### G26 (complex): What was the return rate for each product category last quarter?

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
    p.category,
    COUNT(i.order_item_id) AS lines_sold,
    COUNT(r.return_id) AS lines_returned,
    ROUND(100.0 * COUNT(r.return_id) / COUNT(i.order_item_id), 1) AS return_rate_pct
FROM fact_order_items i
JOIN fact_orders o ON o.order_id = i.order_id
JOIN dim_product p ON p.product_key = i.product_key
LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id
WHERE o.status = 'delivered'
  AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30'
GROUP BY p.category
ORDER BY return_rate_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT p.category, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY p.category
```

### G28 (complex): Which product has the highest return rate? Only count products with at least 30 units sold.

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT p.product_name,
       SUM(i.quantity) AS units_sold,
       COUNT(r.return_id) AS returns,
       ROUND(100.0 * COUNT(r.return_id) / SUM(i.quantity), 1) AS return_rate_pct
FROM fact_order_items i
JOIN fact_orders o ON o.order_id = i.order_id
JOIN dim_product p ON p.product_key = i.product_key
LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id
WHERE o.status = 'delivered'
GROUP BY p.product_key, p.product_name
HAVING SUM(i.quantity) >= 30
ORDER BY return_rate_pct DESC, units_sold DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT p.product_name, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) AS rate FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' GROUP BY p.product_name HAVING COUNT(*) >= 30 ORDER BY rate DESC, p.product_name LIMIT 1
```

### G31 (complex): Of customers who ordered this year, what share ordered more than once, by signup channel?

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH cust_orders AS (
    SELECT o.customer_key, COUNT(*) AS order_count
    FROM fact_orders o
    WHERE o.order_date >= '2026-04-01' AND o.order_date <= '2026-10-31' AND o.status <> 'cancelled'
    GROUP BY o.customer_key
),
channel_repeat AS (
    SELECT c.signup_channel,
           COUNT(*) AS total_customers,
           SUM(CASE WHEN co.order_count > 1 THEN 1 ELSE 0 END) AS repeat_customers
    FROM cust_orders co
    JOIN dim_customer_safe c ON c.customer_key = co.customer_key
    GROUP BY c.signup_channel
)
SELECT signup_channel,
       total_customers,
       repeat_customers,
       ROUND(100.0 * repeat_customers / total_customers, 1) AS repeat_pct
FROM channel_repeat
ORDER BY repeat_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT c.signup_channel, ROUND(100.0 * COUNT(*) FILTER (WHERE t.n > 1) / COUNT(*), 1) FROM (SELECT o.customer_key, COUNT(*) AS n FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY o.customer_key) t JOIN dim_customer_safe c ON c.customer_key = t.customer_key GROUP BY c.signup_channel
```

### G32 (complex): Delivery tickets per 100 orders, by month

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
  d.year_month,
  COUNT(t.ticket_id) AS delivery_tickets,
  COUNT(o.order_id) AS orders,
  ROUND(COUNT(t.ticket_id) * 100.0 / COUNT(o.order_id), 1) AS tickets_per_100_orders
FROM dim_date d
LEFT JOIN fact_orders o ON o.order_date = d.full_date AND o.status <> 'cancelled'
LEFT JOIN fact_tickets t ON t.order_id = o.order_id AND t.category = 'delivery'
WHERE d.full_date BETWEEN '2025-11-01' AND '2026-10-31'
GROUP BY d.year_month
ORDER BY d.year_month LIMIT 200
```

Gold SQL:

```sql
SELECT t.m, ROUND(100.0 * t.n / o.n, 1) FROM (SELECT TO_CHAR(created_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_tickets WHERE category = 'delivery' GROUP BY 1) t JOIN (SELECT TO_CHAR(order_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) o ON o.m = t.m
```

### G35 (complex): Month-over-month growth in orders for the last 3 months

Reasons: agent returned 2 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH monthly AS (
  SELECT
    d.year_month,
    COUNT(*) AS orders
  FROM fact_orders o
  JOIN dim_date d ON d.full_date = o.order_date
  WHERE o.status <> 'cancelled'
    AND d.year_month IN ('2026-08', '2026-09', '2026-10')
  GROUP BY d.year_month
)
SELECT
  m.year_month,
  m.orders,
  CASE
    WHEN prev.orders IS NULL THEN NULL
    ELSE ROUND(100.0 * (m.orders - prev.orders) / prev.orders, 1)
  END AS growth_pct
FROM monthly m
LEFT JOIN monthly prev ON prev.year_month = (
  SELECT m2.year_month FROM monthly m2 WHERE m2.year_month < m.year_month ORDER BY m2.year_month DESC LIMIT 1
)
ORDER BY m.year_month LIMIT 200
```

Gold SQL:

```sql
SELECT m, ROUND(100.0 * (n - prev) / prev, 1) FROM (SELECT m, n, LAG(n) OVER (ORDER BY m) AS prev FROM (SELECT DATE_TRUNC('month', order_date)::date AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) q) t WHERE m >= '2026-08-01' ORDER BY m
```


## Failed questions: ddl (25)

### G01 (simple): How many orders did we get last month?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT COUNT(*) AS order_count
FROM fact_orders fo
JOIN dim_date dd ON fo.order_date = dd.full_date
WHERE dd.year = 2026 AND dd.month = 10 LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_orders o WHERE o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G02 (simple): What was our total revenue last quarter?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT SUM(f.total_inr) AS total_revenue_inr
FROM fact_orders f
JOIN dim_date d ON f.order_date = d.full_date
WHERE d.year = 2026 AND d.quarter = 3 LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G03 (simple): What's the average order value this year?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT avg(total_inr) AS avg_order_value_inr
FROM fact_orders
WHERE order_date >= '2026-01-01' AND order_date < '2027-01-01' LIMIT 200
```

Gold SQL:

```sql
SELECT AVG(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G04 (simple): Which payment method did customers use most last quarter?

Reasons: agent returned 1 numeric columns, gold has 0; extra columns must be non-numeric

Agent SQL:

```sql
SELECT payment_method, COUNT(*) AS order_count
FROM fact_orders
WHERE order_date >= '2026-07-01' AND order_date < '2026-10-01'
GROUP BY payment_method
ORDER BY order_count DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT payment_method FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled' GROUP BY payment_method ORDER BY COUNT(*) DESC, payment_method LIMIT 1
```

### G05 (simple): What share of orders last quarter were cash on delivery?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT
  round(
    100.0 * sum(case when payment_method = 'cash_on_delivery' then 1 else 0 end)
    / count(*), 1
  ) as cod_share_pct
FROM fact_orders
WHERE order_date >= '2026-07-01' AND order_date < '2026-10-01' LIMIT 200
```

Gold SQL:

```sql
SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*), 1) FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G07 (simple): Cancellation rate by payment method this year

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
  fo.payment_method,
  COUNT(*) AS orders,
  COUNT(*) FILTER (WHERE fo.status = 'cancelled') AS cancelled,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE fo.status = 'cancelled') / NULLIF(COUNT(*), 0),
    1
  ) AS cancel_pct
FROM fact_orders fo
JOIN dim_date dd ON fo.order_date = dd.full_date
WHERE dd.year = 2026
GROUP BY fo.payment_method
ORDER BY cancel_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT payment_method, ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'cancelled') / COUNT(*), 1) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' GROUP BY payment_method
```

### G09 (simple): How many orders came through each channel in Bengaluru last month?

Reasons: no agent column matches gold column 2

Agent SQL:

```sql
SELECT channel, COUNT(*) AS orders
FROM fact_orders
WHERE city = 'Bengaluru'
  AND order_date >= '2026-10-01'
  AND order_date < '2026-11-01'
GROUP BY channel
ORDER BY orders DESC LIMIT 200
```

Gold SQL:

```sql
SELECT channel, COUNT(*) FROM fact_orders o WHERE city = 'Bengaluru' AND o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY channel
```

### G10 (simple): Top 5 cities by number of orders this year

Reasons: no agent column matches gold column 2

Agent SQL:

```sql
SELECT city, COUNT(*) AS order_count
FROM fact_orders
WHERE order_date >= '2026-01-01' AND order_date < '2027-01-01'
GROUP BY city
ORDER BY order_count DESC
LIMIT 5
```

Gold SQL:

```sql
SELECT city, COUNT(*) AS n FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY city ORDER BY n DESC, city LIMIT 5
```

### G11 (simple): How much discount did we give on DIWALI15 coupon orders?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT SUM(discount_inr) AS total_discount_inr FROM fact_orders WHERE coupon_code = 'DIWALI15' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(discount_inr) FROM fact_orders o WHERE coupon_code = 'DIWALI15' AND o.status <> 'cancelled'
```

### G12 (simple): Which coupon code was used on the most orders?

Reasons: agent returned 1 numeric columns, gold has 0; extra columns must be non-numeric

Agent SQL:

```sql
SELECT coupon_code, COUNT(*) AS order_count
FROM fact_orders
WHERE coupon_code IS NOT NULL
GROUP BY coupon_code
ORDER BY order_count DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT coupon_code FROM fact_orders o WHERE coupon_code IS NOT NULL AND o.status <> 'cancelled' GROUP BY coupon_code ORDER BY COUNT(*) DESC, coupon_code LIMIT 1
```

### G21 (simple): What is our most expensive grinder?

Reasons: row count differs: gold 1, agent 0

Agent SQL:

```sql
SELECT product_name, list_price_inr
FROM dim_product
WHERE category = 'Grinder'
ORDER BY list_price_inr DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT product_name, list_price_inr FROM dim_product WHERE category = 'grinder' ORDER BY list_price_inr DESC, sku LIMIT 1
```

### G22 (simple): Pichle quarter mein Mumbai se kitne orders aaye?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT COUNT(*) AS mumbai_orders_last_quarter
FROM fact_orders
WHERE city = 'Mumbai'
  AND order_date >= '2026-08-01'
  AND order_date < '2026-11-01' LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_orders o WHERE city = 'Mumbai' AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G24 (simple): Returns by reason this year, most common first

Reasons: no agent column matches gold column 2

Agent SQL:

```sql
SELECT reason, COUNT(*) AS returns
FROM fact_returns
WHERE return_date >= '2026-01-01' AND return_date < '2027-01-01'
GROUP BY reason
ORDER BY returns DESC LIMIT 200
```

Gold SQL:

```sql
SELECT reason, COUNT(*) AS n FROM fact_returns WHERE return_date BETWEEN '2026-04-01' AND '2026-10-31' GROUP BY reason ORDER BY n DESC, reason
```

### G25 (simple): bhai is saal ka total revenue kitna hua?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT SUM(total_inr) AS total_revenue_inr FROM fact_orders WHERE order_date >= '2026-01-01' AND order_date < '2027-01-01' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G26 (complex): What was the return rate for each product category last quarter?

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH sold AS (
  SELECT p.category, COUNT(*) AS items_sold
  FROM fact_order_items oi
  JOIN fact_orders o ON o.order_id = oi.order_id
  JOIN dim_product p ON p.product_key = oi.product_key
  WHERE o.order_date >= '2026-08-01' AND o.order_date < '2026-11-01'
  GROUP BY p.category
),
returned AS (
  SELECT p.category, COUNT(*) AS items_returned
  FROM fact_returns r
  JOIN dim_product p ON p.product_key = r.product_key
  WHERE r.return_date >= '2026-08-01' AND r.return_date < '2026-11-01'
  GROUP BY p.category
)
SELECT s.category,
       s.items_sold,
       COALESCE(r.items_returned, 0) AS items_returned,
       ROUND(100.0 * COALESCE(r.items_returned, 0) / s.items_sold, 2) AS return_rate_pct
FROM sold s
LEFT JOIN returned r ON r.category = s.category
ORDER BY return_rate_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT p.category, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY p.category
```

### G27 (complex): Top 5 beans by revenue in Bengaluru this year

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT p.product_name, SUM(oi.line_total_inr) AS revenue_inr
FROM fact_orders o
JOIN fact_order_items oi ON o.order_id = oi.order_id
JOIN dim_product p ON oi.product_key = p.product_key
JOIN dim_date d ON o.order_date = d.full_date
WHERE o.city = 'Bengaluru' AND d.year = 2026
GROUP BY p.product_name
ORDER BY revenue_inr DESC
LIMIT 5
```

Gold SQL:

```sql
SELECT p.product_name, SUM(i.line_total_inr) AS revenue FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key WHERE p.category = 'beans' AND o.city = 'Bengaluru' AND o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY p.product_name ORDER BY revenue DESC, p.product_name LIMIT 5
```

### G28 (complex): Which product has the highest return rate? Only count products with at least 30 units sold.

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH sold AS (
  SELECT oi.product_key, SUM(oi.quantity) AS units_sold
  FROM fact_order_items oi
  GROUP BY oi.product_key
),
returned AS (
  SELECT r.product_key, COUNT(*) AS units_returned
  FROM fact_returns r
  GROUP BY r.product_key
)
SELECT
  p.product_name,
  s.units_sold,
  COALESCE(r.units_returned, 0) AS units_returned,
  ROUND(100.0 * COALESCE(r.units_returned, 0) / s.units_sold, 2) AS return_rate_pct
FROM sold s
JOIN dim_product p ON p.product_key = s.product_key
LEFT JOIN returned r ON r.product_key = s.product_key
WHERE s.units_sold >= 30
ORDER BY return_rate_pct DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT p.product_name, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) AS rate FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' GROUP BY p.product_name HAVING COUNT(*) >= 30 ORDER BY rate DESC, p.product_name LIMIT 1
```

### G29 (complex): By what percentage did revenue grow from fiscal Q1 to fiscal Q2 of FY2026-27?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
WITH qtr AS (
  SELECT d.fiscal_quarter, SUM(o.total_inr) AS revenue
  FROM fact_orders o
  JOIN dim_date d ON o.order_date = d.full_date
  WHERE d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter IN (1, 2)
  GROUP BY d.fiscal_quarter
)
SELECT
  ROUND(
    100.0 * (MAX(CASE WHEN fiscal_quarter = 2 THEN revenue END)
           - MAX(CASE WHEN fiscal_quarter = 1 THEN revenue END))
    / MAX(CASE WHEN fiscal_quarter = 1 THEN revenue END),
    1
  ) AS qoq_growth_pct
FROM qtr LIMIT 200
```

Gold SQL:

```sql
SELECT ROUND(100.0 * (q2.r - q1.r) / q1.r, 1) FROM (SELECT SUM(o.total_inr) AS r FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.status <> 'cancelled' AND d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 1) q1, (SELECT SUM(o.total_inr) AS r FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.status <> 'cancelled' AND d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 2) q2
```

### G30 (complex): On average, how many orders per day did we get on weekends versus weekdays last quarter?

Reasons: no agent column matches gold column 2

Agent SQL:

```sql
SELECT
  CASE WHEN d.is_weekend THEN 'weekend' ELSE 'weekday' END AS day_type,
  COUNT(o.order_id)::numeric / COUNT(DISTINCT d.full_date) AS avg_orders_per_day
FROM dim_date d
LEFT JOIN fact_orders o ON o.order_date = d.full_date
WHERE d.year = 2026 AND d.quarter = 3
GROUP BY CASE WHEN d.is_weekend THEN 'weekend' ELSE 'weekday' END
ORDER BY day_type LIMIT 200
```

Gold SQL:

```sql
SELECT d.is_weekend, ROUND(COUNT(o.order_id)::numeric / COUNT(DISTINCT d.full_date), 1) FROM dim_date d LEFT JOIN fact_orders o ON o.order_date = d.full_date AND o.status <> 'cancelled' WHERE d.full_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY d.is_weekend
```

### G31 (complex): Of customers who ordered this year, what share ordered more than once, by signup channel?

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH cust_orders AS (
  SELECT
    c.signup_channel,
    o.customer_key,
    COUNT(DISTINCT o.order_id) AS order_count
  FROM fact_orders o
  JOIN dim_customer_safe c ON o.customer_key = c.customer_key
  JOIN dim_date d ON o.order_date = d.full_date
  WHERE d.year = 2026
  GROUP BY c.signup_channel, o.customer_key
),
channel_stats AS (
  SELECT
    signup_channel,
    COUNT(*) AS total_customers,
    SUM(CASE WHEN order_count > 1 THEN 1 ELSE 0 END) AS repeat_customers
  FROM cust_orders
  GROUP BY signup_channel
)
SELECT
  signup_channel,
  total_customers,
  repeat_customers,
  ROUND(100.0 * repeat_customers / total_customers, 1) AS pct_repeat
FROM channel_stats
ORDER BY signup_channel LIMIT 200
```

Gold SQL:

```sql
SELECT c.signup_channel, ROUND(100.0 * COUNT(*) FILTER (WHERE t.n > 1) / COUNT(*), 1) FROM (SELECT o.customer_key, COUNT(*) AS n FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY o.customer_key) t JOIN dim_customer_safe c ON c.customer_key = t.customer_key GROUP BY c.signup_channel
```

### G32 (complex): Delivery tickets per 100 orders, by month

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
  d.year_month,
  COUNT(t.ticket_id) AS delivery_tickets,
  COUNT(o.order_id) AS total_orders,
  ROUND(COUNT(t.ticket_id) * 100.0 / COUNT(o.order_id), 2) AS tickets_per_100_orders
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.full_date
LEFT JOIN fact_tickets t ON o.order_id = t.order_id AND t.category = 'delivery'
GROUP BY d.year_month
ORDER BY d.year_month LIMIT 200
```

Gold SQL:

```sql
SELECT t.m, ROUND(100.0 * t.n / o.n, 1) FROM (SELECT TO_CHAR(created_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_tickets WHERE category = 'delivery' GROUP BY 1) t JOIN (SELECT TO_CHAR(order_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) o ON o.m = t.m
```

### G33 (complex): Gross margin in rupees by product category this year, biggest first

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
  p.category,
  SUM(oi.line_total_inr) AS revenue_inr,
  SUM(oi.quantity * p.unit_cost_inr) AS cost_inr,
  SUM(oi.line_total_inr) - SUM(oi.quantity * p.unit_cost_inr) AS gross_margin_inr
FROM fact_order_items oi
JOIN dim_product p ON oi.product_key = p.product_key
JOIN fact_orders o ON oi.order_id = o.order_id
WHERE o.order_date >= '2026-01-01' AND o.order_date < '2027-01-01'
GROUP BY p.category
ORDER BY gross_margin_inr DESC LIMIT 200
```

Gold SQL:

```sql
SELECT p.category, SUM(i.line_total_inr - i.quantity * p.unit_cost_inr) AS margin FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY p.category ORDER BY margin DESC, p.category
```

### G34 (complex): Revenue in October split between the Diwali run-up days and the other days

Reasons: row count differs: gold 2, agent 0

Agent SQL:

```sql
SELECT
  CASE WHEN d.full_date <= '2025-10-21' THEN 'Diwali run-up (through Oct 21)' ELSE 'Post-Diwali' END AS period,
  SUM(f.total_inr) AS revenue_inr
FROM fact_orders f
JOIN dim_date d ON f.order_date = d.full_date
WHERE d.year = 2025 AND d.month = 10
GROUP BY period
ORDER BY period LIMIT 200
```

Gold SQL:

```sql
SELECT COALESCE(d.festival = 'Diwali Run-up', false) AS diwali_runup, SUM(o.total_inr) FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY COALESCE(d.festival = 'Diwali Run-up', false)
```

### G35 (complex): Month-over-month growth in orders for the last 3 months

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH monthly_orders AS (
  SELECT
    d.year_month,
    COUNT(f.order_id) AS order_count
  FROM fact_orders f
  JOIN dim_date d ON f.order_date = d.full_date
  WHERE d.year_month IN ('2026-08', '2026-09', '2026-10')
  GROUP BY d.year_month
)
SELECT
  year_month,
  order_count,
  LAG(order_count) OVER (ORDER BY year_month) AS prev_month_orders,
  CASE
    WHEN LAG(order_count) OVER (ORDER BY year_month) IS NOT NULL
    THEN ROUND(
      100.0 * (order_count - LAG(order_count) OVER (ORDER BY year_month))
      / LAG(order_count) OVER (ORDER BY year_month), 1
    )
    ELSE NULL
  END AS growth_pct
FROM monthly_orders
ORDER BY year_month LIMIT 200
```

Gold SQL:

```sql
SELECT m, ROUND(100.0 * (n - prev) / prev, 1) FROM (SELECT m, n, LAG(n) OVER (ORDER BY m) AS prev FROM (SELECT DATE_TRUNC('month', order_date)::date AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) q) t WHERE m >= '2026-08-01' ORDER BY m
```

### G40 (refuse): What is our customer acquisition cost for each signup channel?

Reasons: refused for 'write_request', expected one of ['unanswerable'] for kind 'unanswerable'

Agent SQL:

```sql
(none)
```


## Failed questions: floor (34)

### G01 (simple): How many orders did we get last month?

Reasons: no executable SQL (column dd.date_id does not exist)

Agent SQL:

```sql
SELECT COUNT(*) AS order_count
FROM fact_orders fo
JOIN dim_date dd ON fo.order_date = dd.date_id
WHERE dd.month_name = 'October' AND dd.year = 2024 LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_orders o WHERE o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G02 (simple): What was our total revenue last quarter?

Reasons: no executable SQL (column d.date does not exist (hint: Perhaps you meant to reference the column "o.state".))

Agent SQL:

```sql
SELECT SUM(oi.quantity * oi.unit_price) AS total_revenue
FROM fact_orders o
JOIN fact_order_items oi ON o.order_id = oi.order_id
JOIN dim_date d ON o.order_date = d.date
WHERE d.quarter = (SELECT MAX(quarter) - 1 FROM dim_date) LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G03 (simple): What's the average order value this year?

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT AVG(o.order_total) AS avg_order_value
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.date_key
WHERE d.year = 2025 LIMIT 200
```

Gold SQL:

```sql
SELECT AVG(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G04 (simple): Which payment method did customers use most last quarter?

Reasons: no executable SQL (column d.date does not exist (hint: Perhaps you meant to reference the column "o.state".))

Agent SQL:

```sql
SELECT o.payment_method, COUNT(*) AS order_count
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.date
WHERE d.quarter = (SELECT MAX(quarter) FROM dim_date)
GROUP BY o.payment_method
ORDER BY order_count DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT payment_method FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled' GROUP BY payment_method ORDER BY COUNT(*) DESC, payment_method LIMIT 1
```

### G05 (simple): What share of orders last quarter were cash on delivery?

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT
  SUM(CASE WHEN payment_method = 'cash_on_delivery' THEN 1 ELSE 0 END)::numeric / COUNT(*) AS cod_share
FROM fact_orders
WHERE order_date >= '2026-01-01'
  AND order_date < '2026-04-01' LIMIT 200
```

Gold SQL:

```sql
SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE payment_method = 'cod') / COUNT(*), 1) FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G06 (simple): last month kitne orders cancel hue?

Reasons: no executable SQL (column "order_status" does not exist)

Agent SQL:

```sql
SELECT COUNT(*) AS cancelled_orders
FROM fact_orders
WHERE order_status = 'cancelled'
  AND order_date >= '2025-10-01'
  AND order_date < '2025-11-01' LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_orders o WHERE o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status = 'cancelled'
```

### G07 (simple): Cancellation rate by payment method this year

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT
    o.payment_method,
    COUNT(*) AS orders,
    SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
    ROUND(
        100.0 * SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) / COUNT(*),
        1
    ) AS cancel_pct
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.date_key
WHERE d.year = 2025
GROUP BY o.payment_method
ORDER BY cancel_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT payment_method, ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'cancelled') / COUNT(*), 1) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' GROUP BY payment_method
```

### G08 (simple): Average delivery days by shipping method last quarter

Reasons: row count differs: gold 3, agent 0

Agent SQL:

```sql
SELECT
    shipping_method,
    AVG(delivery_days) AS avg_delivery_days
FROM fact_orders
WHERE order_date >= '2025-07-01'
  AND order_date < '2025-10-01'
GROUP BY shipping_method
ORDER BY avg_delivery_days LIMIT 200
```

Gold SQL:

```sql
SELECT shipping_method, AVG(delivery_days) FROM fact_orders o WHERE o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled' GROUP BY shipping_method
```

### G09 (simple): How many orders came through each channel in Bengaluru last month?

Reasons: no executable SQL (column d.date does not exist (hint: Perhaps you meant to reference the column "o.state".))

Agent SQL:

```sql
SELECT o.channel, COUNT(o.order_id) AS order_count
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.date
JOIN dim_customer_safe c ON o.customer_id = c.customer_id
WHERE c.city = 'Bengaluru'
  AND d.date >= '2026-01-01'
  AND d.date < '2026-02-01'
GROUP BY o.channel
ORDER BY order_count DESC LIMIT 200
```

Gold SQL:

```sql
SELECT channel, COUNT(*) FROM fact_orders o WHERE city = 'Bengaluru' AND o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY channel
```

### G10 (simple): Top 5 cities by number of orders this year

Reasons: no executable SQL (column o.customer_id does not exist (hint: Perhaps you meant to reference the column "o.customer_key".))

Agent SQL:

```sql
SELECT c.city, COUNT(o.order_id) AS order_count
FROM fact_orders o
JOIN dim_customer_safe c ON o.customer_id = c.customer_id
JOIN dim_date d ON o.order_date = d.date_key
WHERE d.date_key >= '2026-01-01' AND d.date_key < '2027-01-01'
GROUP BY c.city
ORDER BY order_count DESC
LIMIT 5
```

Gold SQL:

```sql
SELECT city, COUNT(*) AS n FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY city ORDER BY n DESC, city LIMIT 5
```

### G11 (simple): How much discount did we give on DIWALI15 coupon orders?

Reasons: no executable SQL (column "discount" does not exist)

Agent SQL:

```sql
SELECT SUM(discount) AS total_discount
FROM fact_orders
WHERE coupon_code = 'DIWALI15' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(discount_inr) FROM fact_orders o WHERE coupon_code = 'DIWALI15' AND o.status <> 'cancelled'
```

### G12 (simple): Which coupon code was used on the most orders?

Reasons: agent returned 1 numeric columns, gold has 0; extra columns must be non-numeric

Agent SQL:

```sql
SELECT coupon_code, COUNT(*) AS order_count
FROM fact_orders
WHERE coupon_code IS NOT NULL
GROUP BY coupon_code
ORDER BY order_count DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT coupon_code FROM fact_orders o WHERE coupon_code IS NOT NULL AND o.status <> 'cancelled' GROUP BY coupon_code ORDER BY COUNT(*) DESC, coupon_code LIMIT 1
```

### G13 (simple): How many support tickets did we get in August 2026?

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT COUNT(*) AS ticket_count
FROM fact_tickets t
JOIN dim_date d ON t.created_date = d.date_key
WHERE d.year = 2026 AND d.month = 8 LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_tickets WHERE created_date BETWEEN '2026-08-01' AND '2026-08-31'
```

### G14 (simple): Which month had the most support tickets, and how many?

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT
    d.month_name,
    d.year,
    COUNT(*) AS ticket_count
FROM fact_tickets t
JOIN dim_date d ON t.created_date = d.date_key
GROUP BY d.year, d.month_name
ORDER BY ticket_count DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT TO_CHAR(created_date, 'YYYY-MM') AS month, COUNT(*) AS n FROM fact_tickets GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1
```

### G15 (simple): Average first response time in hours for each month

Reasons: no executable SQL (column d.date does not exist)

Agent SQL:

```sql
SELECT
    d.month_name,
    AVG(t.first_response_seconds) / 3600.0 AS avg_first_response_hours
FROM fact_tickets t
JOIN dim_date d ON t.created_date = d.date
GROUP BY d.month_name
ORDER BY d.month_name LIMIT 200
```

Gold SQL:

```sql
SELECT TO_CHAR(created_date, 'YYYY-MM') AS month, ROUND(AVG(first_response_hours), 1) FROM fact_tickets GROUP BY 1
```

### G16 (simple): How many tickets are still open?

Reasons: no executable SQL (column "is_closed" does not exist)

Agent SQL:

```sql
SELECT COUNT(*) AS open_tickets FROM fact_tickets WHERE is_closed = false LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_tickets WHERE resolved = false
```

### G17 (simple): Top 5 ticket categories last quarter by volume

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT t.category, COUNT(*) AS ticket_count
FROM fact_tickets t
JOIN dim_date d ON t.created_date = d.date_key
WHERE d.quarter = (SELECT MAX(quarter) FROM dim_date)
GROUP BY t.category
ORDER BY ticket_count DESC
LIMIT 5
```

Gold SQL:

```sql
SELECT category, COUNT(*) AS n FROM fact_tickets WHERE created_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY category ORDER BY n DESC, category LIMIT 5
```

### G18 (simple): How many customers signed up through each channel?

Reasons: refused: unanswerable

Agent SQL:

```sql
SELECT channel, COUNT(*) AS customers
FROM dim_customer_safe
GROUP BY channel
ORDER BY customers DESC LIMIT 200
```

Gold SQL:

```sql
SELECT signup_channel, COUNT(*) FROM dim_customer_safe GROUP BY signup_channel
```

### G21 (simple): What is our most expensive grinder?

Reasons: no executable SQL (column oi.unit_price does not exist)

Agent SQL:

```sql
SELECT p.product_name, MAX(oi.unit_price) AS max_price
FROM fact_order_items oi
JOIN dim_product p ON oi.product_key = p.product_key
WHERE p.product_category = 'Grinder'
GROUP BY p.product_name
ORDER BY max_price DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT product_name, list_price_inr FROM dim_product WHERE category = 'grinder' ORDER BY list_price_inr DESC, sku LIMIT 1
```

### G22 (simple): Pichle quarter mein Mumbai se kitne orders aaye?

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT COUNT(*) AS orders_mumbai_last_quarter
FROM fact_orders o
JOIN dim_customer_safe c ON o.customer_key = c.customer_key
JOIN dim_date d ON o.order_date = d.date_key
WHERE c.city = 'Mumbai'
  AND d.quarter = (SELECT MAX(quarter) FROM dim_date)
  AND d.year = (SELECT MAX(year) FROM dim_date) LIMIT 200
```

Gold SQL:

```sql
SELECT COUNT(*) FROM fact_orders o WHERE city = 'Mumbai' AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' AND o.status <> 'cancelled'
```

### G23 (simple): How much did we refund to customers last quarter?

Reasons: no executable SQL (column "refund_amount" does not exist)

Agent SQL:

```sql
SELECT SUM(refund_amount) AS total_refunds
FROM fact_returns
WHERE return_date >= '2025-07-01'
  AND return_date < '2025-10-01' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(refund_amount_inr) FROM fact_returns WHERE return_date BETWEEN '2026-07-01' AND '2026-09-30'
```

### G24 (simple): Returns by reason this year, most common first

Reasons: no agent column matches gold column 1

Agent SQL:

```sql
SELECT reason, COUNT(*) AS cnt
FROM fact_returns
WHERE return_date >= '2025-01-01' AND return_date < '2026-01-01'
GROUP BY reason
ORDER BY cnt DESC LIMIT 200
```

Gold SQL:

```sql
SELECT reason, COUNT(*) AS n FROM fact_returns WHERE return_date BETWEEN '2026-04-01' AND '2026-10-31' GROUP BY reason ORDER BY n DESC, reason
```

### G25 (simple): bhai is saal ka total revenue kitna hua?

Reasons: no executable SQL (column o.order_date_id does not exist (hint: Perhaps you meant to reference the column "o.order_date".))

Agent SQL:

```sql
SELECT SUM(oi.quantity * oi.unit_price) AS total_revenue
FROM fact_order_items oi
JOIN fact_orders o ON oi.order_id = o.order_id
JOIN dim_date d ON o.order_date_id = d.date_id
WHERE d.date_value >= '2026-01-01' AND d.date_value < '2027-01-01' LIMIT 200
```

Gold SQL:

```sql
SELECT SUM(total_inr) FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled'
```

### G26 (complex): What was the return rate for each product category last quarter?

Reasons: no executable SQL (column o.product_id does not exist)

Agent SQL:

```sql
WITH orders_by_cat AS (
  SELECT p.category, COUNT(DISTINCT o.order_id) AS order_count
  FROM fact_orders o
  JOIN dim_product p ON o.product_id = p.product_id
  WHERE o.order_date >= '2026-01-01' AND o.order_date < '2026-04-01'
  GROUP BY p.category
),
returns_by_cat AS (
  SELECT p.category, COUNT(DISTINCT r.return_id) AS return_count
  FROM fact_returns r
  JOIN fact_orders o ON r.order_id = o.order_id
  JOIN dim_product p ON o.product_id = p.product_id
  WHERE r.return_date >= '2026-01-01' AND r.return_date < '2026-04-01'
  GROUP BY p.category
)
SELECT o.category,
       o.order_count,
       COALESCE(r.return_count, 0) AS return_count,
       ROUND(100.0 * COALESCE(r.return_count, 0) / NULLIF(o.order_count, 0), 2) AS return_rate_pct
FROM orders_by_cat o
LEFT JOIN returns_by_cat r ON o.category = r.category
ORDER BY return_rate_pct DESC LIMIT 200
```

Gold SQL:

```sql
SELECT p.category, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY p.category
```

### G27 (complex): Top 5 beans by revenue in Bengaluru this year

Reasons: no executable SQL (column d.date does not exist (hint: Perhaps you meant to reference the column "o.state".))

Agent SQL:

```sql
SELECT p.product_name, SUM(oi.quantity * oi.unit_price) AS revenue
FROM fact_orders o
JOIN fact_order_items oi ON o.order_id = oi.order_id
JOIN dim_product p ON oi.product_key = p.product_key
JOIN dim_date d ON o.order_date = d.date
WHERE o.city = 'Bengaluru' AND d.year = 2025
GROUP BY p.product_name
ORDER BY revenue DESC
LIMIT 5
```

Gold SQL:

```sql
SELECT p.product_name, SUM(i.line_total_inr) AS revenue FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key WHERE p.category = 'beans' AND o.city = 'Bengaluru' AND o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY p.product_name ORDER BY revenue DESC, p.product_name LIMIT 5
```

### G28 (complex): Which product has the highest return rate? Only count products with at least 30 units sold.

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
SELECT
    p.product_name,
    SUM(CASE WHEN r.return_id IS NOT NULL THEN 1 ELSE 0 END) AS returns,
    COUNT(*) AS units_sold,
    ROUND(
        100.0 * SUM(CASE WHEN r.return_id IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*),
        2
    ) AS return_rate_pct
FROM fact_order_items oi
JOIN dim_product p ON oi.product_key = p.product_key
LEFT JOIN fact_returns r ON r.order_item_id = oi.order_item_id
GROUP BY p.product_key, p.product_name
HAVING COUNT(*) >= 30
ORDER BY return_rate_pct DESC
LIMIT 1
```

Gold SQL:

```sql
SELECT p.product_name, ROUND(100.0 * COUNT(r.return_id) / COUNT(*), 1) AS rate FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key LEFT JOIN fact_returns r ON r.order_item_id = i.order_item_id WHERE o.status = 'delivered' GROUP BY p.product_name HAVING COUNT(*) >= 30 ORDER BY rate DESC, p.product_name LIMIT 1
```

### G29 (complex): By what percentage did revenue grow from fiscal Q1 to fiscal Q2 of FY2026-27?

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
WITH q1 AS (
    SELECT SUM(oi.quantity * oi.unit_price) AS revenue
    FROM fact_orders o
    JOIN fact_order_items oi ON o.order_id = oi.order_id
    JOIN dim_date d ON o.order_date = d.date_key
    WHERE d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 'Q1'
),
q2 AS (
    SELECT SUM(oi.quantity * oi.unit_price) AS revenue
    FROM fact_orders o
    JOIN fact_order_items oi ON o.order_id = oi.order_id
    JOIN dim_date d ON o.order_date = d.date_key
    WHERE d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 'Q2'
)
SELECT
    q1.revenue AS q1_revenue,
    q2.revenue AS q2_revenue,
    ROUND(((q2.revenue - q1.revenue) / q1.revenue) * 100, 2) AS pct_growth
FROM q1, q2 LIMIT 200
```

Gold SQL:

```sql
SELECT ROUND(100.0 * (q2.r - q1.r) / q1.r, 1) FROM (SELECT SUM(o.total_inr) AS r FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.status <> 'cancelled' AND d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 1) q1, (SELECT SUM(o.total_inr) AS r FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.status <> 'cancelled' AND d.fiscal_year = 'FY2026-27' AND d.fiscal_quarter = 2) q2
```

### G30 (complex): On average, how many orders per day did we get on weekends versus weekdays last quarter?

Reasons: no executable SQL (column d.date_id does not exist)

Agent SQL:

```sql
SELECT
    CASE WHEN d.is_weekend THEN 'weekend' ELSE 'weekday' END AS day_type,
    COUNT(o.order_id) AS total_orders,
    COUNT(DISTINCT d.date_id) AS days,
    ROUND(COUNT(o.order_id)::numeric / COUNT(DISTINCT d.date_id), 2) AS avg_orders_per_day
FROM fact_orders o
JOIN dim_date d ON o.order_date = d.date_id
WHERE d.quarter = 'Q4' AND d.year = 2024
GROUP BY CASE WHEN d.is_weekend THEN 'weekend' ELSE 'weekday' END LIMIT 200
```

Gold SQL:

```sql
SELECT d.is_weekend, ROUND(COUNT(o.order_id)::numeric / COUNT(DISTINCT d.full_date), 1) FROM dim_date d LEFT JOIN fact_orders o ON o.order_date = d.full_date AND o.status <> 'cancelled' WHERE d.full_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY d.is_weekend
```

### G31 (complex): Of customers who ordered this year, what share ordered more than once, by signup channel?

Reasons: no executable SQL (column o.customer_id does not exist (hint: Perhaps you meant to reference the column "o.customer_key".))

Agent SQL:

```sql
WITH cust_orders AS (
    SELECT
        o.customer_id,
        c.signup_channel,
        COUNT(DISTINCT o.order_id) AS order_count
    FROM fact_orders o
    JOIN dim_customer_safe c ON o.customer_id = c.customer_id
    JOIN dim_date d ON o.order_date = d.date_key
    WHERE d.year = 2026
    GROUP BY o.customer_id, c.signup_channel
),
channel_stats AS (
    SELECT
        signup_channel,
        COUNT(*) AS total_customers,
        SUM(CASE WHEN order_count > 1 THEN 1 ELSE 0 END) AS repeat_customers
    FROM cust_orders
    GROUP BY signup_channel
)
SELECT
    signup_channel,
    total_customers,
    repeat_customers,
    ROUND(100.0 * repeat_customers / total_customers, 1) AS pct_repeat
FROM channel_stats
ORDER BY signup_channel LIMIT 200
```

Gold SQL:

```sql
SELECT c.signup_channel, ROUND(100.0 * COUNT(*) FILTER (WHERE t.n > 1) / COUNT(*), 1) FROM (SELECT o.customer_key, COUNT(*) AS n FROM fact_orders o WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY o.customer_key) t JOIN dim_customer_safe c ON c.customer_key = t.customer_key GROUP BY c.signup_channel
```

### G32 (complex): Delivery tickets per 100 orders, by month

Reasons: no executable SQL (column d.date does not exist (hint: Perhaps you meant to reference the column "o.state".))

Agent SQL:

```sql
SELECT
    d.date_part_month,
    COUNT(t.ticket_id) AS tickets,
    COUNT(DISTINCT o.order_id) AS orders,
    ROUND(COUNT(t.ticket_id) * 100.0 / COUNT(DISTINCT o.order_id), 2) AS tickets_per_100_orders
FROM fact_tickets t
JOIN fact_orders o ON t.order_id = o.order_id
JOIN dim_date d ON o.order_date = d.date
GROUP BY d.date_part_month
ORDER BY d.date_part_month LIMIT 200
```

Gold SQL:

```sql
SELECT t.m, ROUND(100.0 * t.n / o.n, 1) FROM (SELECT TO_CHAR(created_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_tickets WHERE category = 'delivery' GROUP BY 1) t JOIN (SELECT TO_CHAR(order_date, 'YYYY-MM') AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) o ON o.m = t.m
```

### G33 (complex): Gross margin in rupees by product category this year, biggest first

Reasons: no executable SQL (column o.order_date_id does not exist (hint: Perhaps you meant to reference the column "o.order_date".))

Agent SQL:

```sql
SELECT
    p.category,
    SUM(oi.quantity * oi.unit_price) AS revenue,
    SUM(oi.quantity * oi.unit_cost) AS cogs,
    SUM(oi.quantity * (oi.unit_price - oi.unit_cost)) AS gross_margin
FROM fact_order_items oi
JOIN dim_product p ON oi.product_key = p.product_key
JOIN fact_orders o ON oi.order_id = o.order_id
JOIN dim_date d ON o.order_date_id = d.date_id
WHERE d.year = 2025
GROUP BY p.category
ORDER BY gross_margin DESC LIMIT 200
```

Gold SQL:

```sql
SELECT p.category, SUM(i.line_total_inr - i.quantity * p.unit_cost_inr) AS margin FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_key = i.product_key WHERE o.order_date BETWEEN '2026-04-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY p.category ORDER BY margin DESC, p.category
```

### G34 (complex): Revenue in October split between the Diwali run-up days and the other days

Reasons: no executable SQL (column d.date_key does not exist)

Agent SQL:

```sql
SELECT
    SUM(CASE WHEN d.is_diwali_runup THEN oi.quantity * oi.unit_price ELSE 0 END) AS diwali_runup_revenue,
    SUM(CASE WHEN d.is_diwali_runup THEN 0 ELSE oi.quantity * oi.unit_price END) AS other_october_revenue
FROM fact_order_items oi
JOIN fact_orders o ON oi.order_id = o.order_id
JOIN dim_date d ON o.order_date = d.date_key
WHERE d.year = 2024 AND d.month = 10 LIMIT 200
```

Gold SQL:

```sql
SELECT COALESCE(d.festival = 'Diwali Run-up', false) AS diwali_runup, SUM(o.total_inr) FROM fact_orders o JOIN dim_date d ON d.full_date = o.order_date WHERE o.order_date BETWEEN '2026-10-01' AND '2026-10-31' AND o.status <> 'cancelled' GROUP BY COALESCE(d.festival = 'Diwali Run-up', false)
```

### G35 (complex): Month-over-month growth in orders for the last 3 months

Reasons: agent returned 3 numeric columns, gold has 1; extra columns must be non-numeric

Agent SQL:

```sql
WITH monthly_orders AS (
    SELECT
        date_trunc('month', o.order_date) AS month,
        COUNT(*) AS order_count
    FROM fact_orders o
    WHERE o.order_date >= '2026-01-01' AND o.order_date < '2026-04-01'
    GROUP BY 1
)
SELECT
    month,
    order_count,
    LAG(order_count) OVER (ORDER BY month) AS prev_month_count,
    CASE
        WHEN LAG(order_count) OVER (ORDER BY month) IS NULL THEN NULL
        ELSE ROUND(
            100.0 * (order_count - LAG(order_count) OVER (ORDER BY month))
            / LAG(order_count) OVER (ORDER BY month),
            2
        )
    END AS growth_pct
FROM monthly_orders
ORDER BY month LIMIT 200
```

Gold SQL:

```sql
SELECT m, ROUND(100.0 * (n - prev) / prev, 1) FROM (SELECT m, n, LAG(n) OVER (ORDER BY m) AS prev FROM (SELECT DATE_TRUNC('month', order_date)::date AS m, COUNT(*) AS n FROM fact_orders WHERE status <> 'cancelled' GROUP BY 1) q) t WHERE m >= '2026-08-01' ORDER BY m
```

### G40 (refuse): What is our customer acquisition cost for each signup channel?

Reasons: refused for 'write_request', expected one of ['unanswerable'] for kind 'unanswerable'

Agent SQL:

```sql
(none)
```
