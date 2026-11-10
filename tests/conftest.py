import psycopg
import pytest

from app.config import settings


def _db_up() -> bool:
    try:
        with psycopg.connect(settings.analyst_dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1 FROM fact_orders LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


def pytest_collection_modifyitems(config, items):
    if any(i.get_closest_marker("db") for i in items) and not _db_up():
        skip = pytest.mark.skip(reason="warehouse not running/loaded (docker compose up -d db; python -m warehouse.load)")
        for item in items:
            if item.get_closest_marker("db"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_request_log(tmp_path, monkeypatch):
    """Tests never write to the real logs/requests.jsonl."""
    from app import logging_

    monkeypatch.setattr(logging_, "LOG_PATH", tmp_path / "requests.jsonl")
