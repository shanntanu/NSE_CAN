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

DB_DIR = Path(__file__).resolve().parents[2] / "db"
SCHEMA_FILE = DB_DIR / "schema.postgres.sql"        # shared and small: the website uses it too
LOCAL_SCHEMA_FILE = DB_DIR / "schema.local.sql"     # heavy data, kept on the worker server only
DEFAULT_LOCAL_PATH = Path(__file__).resolve().parents[1] / "data" / "local.db"


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

    async def init_schema(self, schema_file: Path | None = None) -> None:
        sql = (schema_file or SCHEMA_FILE).read_text(encoding="utf-8")
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

    async def size_bytes(self) -> int | None:
        """Size of the database in bytes (Postgres only; None for SQLite)."""
        if self.is_sqlite:
            return None
        row = await self.fetch_one("select pg_database_size(current_database()) as n")
        return int(row["n"]) if row else None

    async def close(self) -> None:
        await self.engine.dispose()


def local_url() -> str:
    """Where the heavy data lives: LOCAL_DATABASE_URL, or a SQLite file in worker/data/."""
    url = os.environ.get("LOCAL_DATABASE_URL", "").strip()
    if url:
        return url
    DEFAULT_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_LOCAL_PATH.as_posix()}"


def open_databases() -> tuple[Database, Database]:
    """(shared, local): the small shared database, and the heavy one kept on the worker server."""
    return Database(), Database(local_url())


async def init_databases(shared: Database, local: Database) -> None:
    await shared.init_schema(SCHEMA_FILE)
    await local.init_schema(LOCAL_SCHEMA_FILE)


class StatusWriter:
    """Writes status keys to the local database every time, and to the shared one at most every few seconds.

    The shared copy is what the website shows, so it only needs a coarse update; writing it less often keeps the
    small hosted database quiet (and lets it sleep when nothing else is happening).
    """

    def __init__(self, shared: Database, local: Database, shared_interval_sec: float = 30.0):
        self.shared = shared
        self.local = local
        self.interval = shared_interval_sec
        self._last_shared: dict[str, float] = {}

    async def set(self, key: str, value: str, force_shared: bool = False) -> None:
        import time

        await self.local.set_status(key, value)
        now = time.monotonic()
        if force_shared or now - self._last_shared.get(key, -1e9) >= self.interval:
            self._last_shared[key] = now
            try:
                await self.shared.set_status(key, value)
            except Exception:
                log.warning("could not write %s to the shared database", key, exc_info=True)
