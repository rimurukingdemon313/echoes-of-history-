from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echoes.clock import FrozenClock
from echoes.config import load_settings
from echoes.logging import configure

configure("ERROR")

TEST_DB = os.environ.get("ECHOES_TEST_DATABASE_URL")


@pytest.fixture
def clock() -> FrozenClock:
    """A pinned clock. Nothing in this system reads the wall clock directly."""
    return FrozenClock(datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
def settings(tmp_path):
    return load_settings().with_(
        data_dir=tmp_path, llm_provider="offline", tts_provider="silent",
        research_providers=["fixtures"], image_providers=["synthetic"],
        notify_provider="noop", storage_provider="local",
        database_url=TEST_DB, api_token="test-token",
    )


@pytest.fixture
def db():
    """A clean schema per test. Skipped when no test database is configured."""
    if not TEST_DB:
        pytest.skip("set ECHOES_TEST_DATABASE_URL to run database tests")
    from echoes.db import migrate, pool
    pool.close_pool()
    pool.init_pool(TEST_DB)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate.migrate()
    yield pool
    pool.close_pool()
