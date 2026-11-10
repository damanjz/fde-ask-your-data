"""Create roles, schema, grants and load the generated warehouse. Idempotent: rebuilds everything.

    python -m warehouse.load            # regenerate CSVs, then load
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.config import settings
from warehouse import generate

HERE = Path(__file__).resolve().parent


def _password(dsn: str, who: str) -> str:
    pw = conninfo_to_dict(dsn).get("password")
    if not pw:
        raise RuntimeError(f"no password in the {who} DSN: copy .env.example to .env and set the passwords")
    return pw


def _roles_sql(conn: psycopg.Connection, owner_dsn: str, analyst_dsn: str) -> str:
    text = (HERE / "roles.sql").read_text(encoding="utf-8")
    owner_pw = sql.Literal(_password(owner_dsn, "owner")).as_string(conn)
    analyst_pw = sql.Literal(_password(analyst_dsn, "analyst")).as_string(conn)
    return text.replace("{owner_password}", owner_pw).replace("{analyst_password}", analyst_pw)


def load(regenerate: bool = True) -> dict[str, int]:
    """Bootstrap roles as bi_admin, then build and load everything as the non-superuser bi_owner."""
    if regenerate or not (generate.OUT_DIR / "manifest.json").exists():
        generate.write_csv(generate.generate())
    with psycopg.connect(settings.admin_dsn, autocommit=True) as admin:
        admin.execute(_roles_sql(admin, settings.owner_dsn, settings.analyst_dsn))
    with psycopg.connect(settings.owner_dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
            for table in generate.LOAD_ORDER:
                cols = ", ".join(generate.TABLE_COLUMNS[table])
                path = generate.OUT_DIR / f"{table}.csv"
                copy_sql = f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER)"
                with conn.cursor() as cur, cur.copy(copy_sql) as cp, path.open("rb") as fh:
                    while chunk := fh.read(1 << 16):
                        cp.write(chunk)
        conn.execute("ANALYZE")
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in generate.LOAD_ORDER}
    return counts


def main() -> None:
    counts = load()
    for table, n in counts.items():
        print(f"{table:18s} {n:>7d}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
