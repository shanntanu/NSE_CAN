from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "schema.postgres.sql"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_dt(value: Any) -> datetime | None:
    """Normalise a database timestamp (datetime or ISO text) to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "sqlite+aiosqlite:///./nse_alerts.db"
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def sqlite_schema(sql: str) -> str:
    """Turn the Postgres schema into SQLite-compatible SQL (used for local runs and tests)."""
    sql = re.sub(r"bigserial\s+primary\s+key", "integer primary key autoincrement", sql, flags=re.I)
    sql = re.sub(r"\btimestamptz\b", "text", sql, flags=re.I)
    sql = re.sub(r"default\s+now\(\)", "default CURRENT_TIMESTAMP", sql, flags=re.I)
    return sql


def split_statements(sql: str) -> list[str]:
    lines = [re.sub(r"--.*$", "", ln) for ln in sql.splitlines()]  # comments may contain semicolons
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


class Database:
    def __init__(self, url: str | None = None):
        self.url = normalise_url(url if url is not None else os.environ.get("DATABASE_URL", ""))
        self.is_sqlite = self.url.startswith("sqlite")
        kwargs: dict[str, Any] = {}
        if self.is_sqlite:
            kwargs["connect_args"] = {"timeout": 60}  # wait for a busy file instead of failing
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True)
        self.engine: AsyncEngine = create_async_engine(self.url, **kwargs)

    async def init_schema(self) -> None:
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        if self.is_sqlite:
            sql = sqlite_schema(sql)
        async with self.engine.begin() as conn:
            for stmt in split_statements(sql):
                await conn.execute(text(stmt))

    def _coerce(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.is_sqlite:
            return params
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in params.items()}

    async def fetch_all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            result = await conn.execute(text(sql), self._coerce(params))
            return [dict(r._mapping) for r in result]

    async def fetch_one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, **params)
        return rows[0] if rows else None

    async def execute(self, sql: str, **params: Any) -> int:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), self._coerce(params))
            return result.rowcount

    async def insert_returning_id(self, sql: str, **params: Any) -> int | None:
        """Run an INSERT ... RETURNING id; None when a conflict swallowed the row."""
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), self._coerce(params))
            row = result.first()
            return int(row[0]) if row else None

    async def set_status(self, key: str, value: str) -> None:
        await self.execute(
            "insert into system_status (key, value, updated_at) values (:k, :v, :now) "
            "on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
            k=key, v=value, now=utcnow(),
        )

    async def close(self) -> None:
        await self.engine.dispose()
