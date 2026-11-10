"""FastAPI service: POST /ask, GET /health, GET /schema and the local HTML page (port 8400)."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent, db, schema_docs
from app.config import settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
app = FastAPI(title="Larkfield ask-your-data agent (simulated customer)")

PUBLIC_KEYS = (
    "question", "status", "plan", "sql", "columns", "rows", "row_count", "truncated", "summary", "refused",
    "refused_reason", "refusal_message", "error", "guardrail_blocks", "summary_faithful", "usage",
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=agent.MAX_QUESTION_CHARS)


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    result = agent.ask(req.question)
    return {k: result[k] for k in PUBLIC_KEYS}


@app.get("/health")
def health() -> dict:
    try:
        db.run_query("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001 - any failure means unhealthy
        db_ok = False
    try:
        ollama_ok = httpx.get(f"{settings.ollama_url}/api/tags", timeout=3).status_code == 200
    except httpx.HTTPError:
        ollama_ok = False
    return {"status": "ok" if db_ok and ollama_ok else "degraded", "db": db_ok, "ollama": ollama_ok,
            "model": settings.ollama_model}


@app.get("/schema")
def schema() -> dict:
    docs = schema_docs.load()
    return {"as_of": docs.as_of, "today": docs.today,
            "tables": {t.name: [c.name for c in t.columns] for t in docs.tables.values()}}


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
