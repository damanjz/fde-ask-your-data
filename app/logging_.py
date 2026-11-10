"""Request log: one JSON line per request in logs/requests.jsonl. Never raises."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from app.guardrails import mask_pii

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "requests.jsonl"
_log = logging.getLogger(__name__)


def log_request(record: dict, path: Path | None = None) -> None:
    """Append one masked record (question, sql, rows returned, latency, tokens, refused reason, ...)."""
    try:
        rec = dict(record)
        if isinstance(rec.get("question"), str):
            rec["question"] = mask_pii(rec["question"])
        rec["ts"] = datetime.now(UTC).isoformat(timespec="seconds")
        target = path or LOG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break a request
        _log.warning("could not write request log", exc_info=True)
