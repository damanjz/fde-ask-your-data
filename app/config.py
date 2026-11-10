from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Connection strings come from .env (passwords included). The defaults carry no password, so a missing
    # .env fails closed. The agent connects as analyst_ro (SELECT on the facts, dims and dim_customer_safe only);
    # the loader uses bi_owner (not a superuser) and bootstraps roles as bi_admin.
    analyst_dsn: str = "postgresql://analyst_ro@127.0.0.1:5435/larkfield_bi"
    owner_dsn: str = "postgresql://bi_owner@127.0.0.1:5435/larkfield_bi"
    admin_dsn: str = "postgresql://bi_admin@127.0.0.1:5435/larkfield_bi"

    llm_provider: str = "ollama"  # ollama | anthropic
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "fusion27b:iq3"
    llm_timeout_s: float = 180.0

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    price_in_per_mtok: float = 0.0  # USD per million tokens, cost reporting only
    price_out_per_mtok: float = 0.0

    # The warehouse is a fixed simulated snapshot, so "today" is pinned (not the wall clock).
    today: str = "2026-11-10"
    data_through: str = "2026-10-31"

    max_rows: int = 200  # LIMIT added when absent, and hard cap on fetched rows
    show_rows: int = 50  # rows returned to the caller / shown in the UI
    statement_timeout_ms: int = 5000
    max_corrections: int = 1


settings = Settings()
