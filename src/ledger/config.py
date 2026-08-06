"""Application settings, read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ledger:ledger@localhost:5432/ledger"

    # Connection pool. The concurrency tests fan out well past the SQLAlchemy default of 5.
    pool_size: int = 20
    max_overflow: int = 40

    # How many times a posting re-reads account versions and retries after losing the
    # optimistic compare-and-swap. See services/posting.py.
    max_posting_retries: int = 25
    retry_backoff_base_seconds: float = 0.002
    retry_backoff_cap_seconds: float = 0.05

    # Write a balance snapshot every N entries, per account. 0 disables snapshotting,
    # which makes every balance a full scan of that account's log.
    snapshot_every: int = 100


settings = Settings()
