"""Database access.

A thin wrapper over psycopg rather than an ORM. The queries this system runs
are few and specific, and the parts that matter -- the partial unique index
that makes stage resumption safe, ``ON CONFLICT DO NOTHING`` on the upload
record -- are easier to get right and to read in SQL than through a mapper.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..errors import ConfigError, ProviderUnavailable
from ..logging import get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def init_pool(database_url: str | None, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """Create the shared pool. Idempotent."""
    global _pool
    if not database_url:
        raise ConfigError(
            "DATABASE_URL is not set. This system keeps every durable "
            "decision -- which topics are taken, which stages completed, "
            "which video ids were uploaded -- in PostgreSQL, because a "
            "container filesystem does not survive a redeploy."
        )
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                database_url,
                min_size=min_size,
                max_size=max_size,
                kwargs={"row_factory": dict_row},
                open=True,
            )
    return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise ConfigError("Database pool not initialised; call init_pool() first")
    return _pool


@contextlib.contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection wrapped in a transaction.

    A failure to reach the database is raised as :class:`ProviderUnavailable`
    so the stage runner treats it as retryable -- but note that the *callers*
    that guard new production work check reachability first and stand down
    rather than retrying into a void.
    """
    pool = get_pool()
    try:
        with pool.connection() as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise ProviderUnavailable(f"database unavailable: {exc}") from exc


def query(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(cur.fetchall())


def query_one(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def healthy() -> bool:
    """True if the database answers. Never raises.

    Rule 7 of the operating policy: if this returns False the system takes on
    no new production work, because a job whose state cannot be written is a
    job that will be silently repeated.
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 - a health check must not raise
        log.warning("database health check failed", extra={"error": str(exc)})
        return False
