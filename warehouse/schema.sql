-- Larkfield Coffee Co. BI warehouse (simulated). Star schema; owner loads, analyst_ro only reads.
DROP VIEW IF EXISTS dim_customer_safe;
DROP TABLE IF EXISTS fact_tickets, fact_returns, fact_order_items, fact_orders,
                     dim_customer, dim_product, dim_date CASCADE;

CREATE TABLE dim_date (
  full_date      date PRIMARY KEY,
  year           smallint NOT NULL,
  quarter        smallint NOT NULL,
  month          smallint NOT NULL,
  month_name     text NOT NULL,
  year_month     text NOT NULL,
  week_of_year   smallint NOT NULL,
  day_of_week    smallint NOT NULL,
  day_name       text NOT NULL,
  is_weekend     boolean NOT NULL,
  fiscal_year    text NOT NULL,
  fiscal_quarter smallint NOT NULL,
  festival       text
);

CREATE TABLE dim_product (
  product_key    smallint PRIMARY KEY,
  sku            text NOT NULL UNIQUE,
  product_name   text NOT NULL,
  category       text NOT NULL,
  list_price_inr integer NOT NULL,
  unit_cost_inr  integer NOT NULL,
  warranty_months smallint NOT NULL
);

CREATE TABLE dim_customer (
  customer_key   integer PRIMARY KEY,
  name           text NOT NULL,
  email          text NOT NULL,
  phone          text NOT NULL,
  city           text NOT NULL,
  state          text NOT NULL,
  signup_date    date NOT NULL,
  signup_channel text NOT NULL
);

CREATE TABLE fact_orders (
  order_id        integer PRIMARY KEY,
  customer_key    integer NOT NULL REFERENCES dim_customer,
  order_date      date NOT NULL REFERENCES dim_date,
  channel         text NOT NULL,
  city            text NOT NULL,
  state           text NOT NULL,
  payment_method  text NOT NULL,
  shipping_method text NOT NULL,
  coupon_code     text,
  subtotal_inr    integer NOT NULL,
  discount_inr    integer NOT NULL,
  shipping_fee_inr integer NOT NULL,
  total_inr       integer NOT NULL,
  status          text NOT NULL,
  delivery_days   smallint
);

CREATE TABLE fact_order_items (
  order_item_id  integer PRIMARY KEY,
  order_id       integer NOT NULL REFERENCES fact_orders,
  product_key    smallint NOT NULL REFERENCES dim_product,
  quantity       smallint NOT NULL,
  unit_price_inr integer NOT NULL,
  line_total_inr integer NOT NULL
);

CREATE TABLE fact_returns (
  return_id         integer PRIMARY KEY,
  order_item_id     integer NOT NULL UNIQUE REFERENCES fact_order_items,
  order_id          integer NOT NULL REFERENCES fact_orders,
  product_key       smallint NOT NULL REFERENCES dim_product,
  return_date       date NOT NULL REFERENCES dim_date,
  reason            text NOT NULL,
  refund_amount_inr integer NOT NULL
);

CREATE TABLE fact_tickets (
  ticket_id            integer PRIMARY KEY,
  customer_key         integer NOT NULL REFERENCES dim_customer,
  order_id             integer REFERENCES fact_orders,
  created_date         date NOT NULL REFERENCES dim_date,
  category             text NOT NULL,
  channel              text NOT NULL,
  first_response_hours numeric(7,1) NOT NULL,
  resolved             boolean NOT NULL,
  resolution_hours     numeric(7,1),
  csat_score           smallint
);

CREATE INDEX ON fact_orders (order_date);
CREATE INDEX ON fact_orders (customer_key);
CREATE INDEX ON fact_order_items (order_id);
CREATE INDEX ON fact_order_items (product_key);
CREATE INDEX ON fact_returns (order_item_id);
CREATE INDEX ON fact_tickets (created_date);

-- PII-free view of customers. analyst_ro is granted this and nothing on dim_customer.
CREATE VIEW dim_customer_safe AS
  SELECT customer_key, city, state, signup_date, signup_channel FROM dim_customer;

-- Privileges: deny by default, then grant the analyst the safe surface only.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON dim_customer FROM analyst_ro;
GRANT SELECT ON dim_date, dim_product, fact_orders, fact_order_items, fact_returns, fact_tickets,
                dim_customer_safe TO analyst_ro;
