-- Roles for the BI warehouse. Run as the database admin (bi_admin) by `python -m warehouse.load`; idempotent.
-- {owner_password} and {analyst_password} are filled in by the loader (properly quoted) from OWNER_DSN / ANALYST_DSN.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_owner') THEN
    CREATE ROLE bi_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
    CREATE ROLE analyst_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;

-- The owner loads data and owns the tables; it is not a superuser.
ALTER ROLE bi_owner WITH LOGIN PASSWORD {owner_password} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
-- The analyst only reads. The role carries its own limits; the privileges (schema.sql) are what really hold.
ALTER ROLE analyst_ro WITH LOGIN PASSWORD {analyst_password} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE analyst_ro SET statement_timeout = '5s';
ALTER ROLE analyst_ro SET lock_timeout = '2s';
ALTER ROLE analyst_ro SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE analyst_ro SET default_transaction_read_only = on;
ALTER ROLE analyst_ro SET search_path = public;

-- No default privileges for PUBLIC on the database (this removes CONNECT and TEMP), then explicit grants.
DO $$
BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM analyst_ro', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO bi_owner, analyst_ro', current_database());
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO bi_owner;
GRANT USAGE ON SCHEMA public TO analyst_ro;
