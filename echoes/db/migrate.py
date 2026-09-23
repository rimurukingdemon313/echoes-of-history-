"""Forward-only migrations.

Files in ``migrations/`` are applied in filename order and recorded in
``schema_migrations``. There is no down-migration: rolling a schema backward
on a system that has already written production rows loses data more often
than it saves a deploy. To undo, write a new migration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..logging import get_logger
from . import pool

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def pending() -> list[Path]:
    applied = {r["filename"] for r in pool.query("SELECT filename FROM schema_migrations")}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]


def migrate() -> list[str]:
    """Apply every pending migration. Returns the filenames applied."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP)

    applied_rows = pool.query("SELECT filename, checksum FROM schema_migrations")
    applied = {r["filename"]: r["checksum"] for r in applied_rows}

    done: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        digest = _checksum(sql)
        if path.name in applied:
            # An edited migration means the database and the repository
            # disagree about what the schema is. Say so rather than guess.
            if applied[path.name] != digest:
                log.error(
                    "migration file changed after it was applied",
                    extra={"file": path.name, "recorded": applied[path.name],
                           "found": digest},
                )
            continue
        log.info("applying migration", extra={"file": path.name})
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                    (path.name, digest),
                )
        done.append(path.name)
    if not done:
        log.info("schema is up to date")
    return done
