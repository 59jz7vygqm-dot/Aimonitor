"""Test-Fixtures.

Die DB-Tests brauchen eine erreichbare PostgreSQL-Instanz. Ohne sie werden
sie übersprungen statt zu scheitern - eine fehlende Testdatenbank ist kein
Defekt im Code.

    AIMON_TEST_DATABASE_URL=postgresql://aimon@127.0.0.1:5432/aimon_test pytest
"""
from __future__ import annotations

import os
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TEST_DSN = os.getenv("AIMON_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def dsn() -> str:
    if not TEST_DSN:
        pytest.skip("AIMON_TEST_DATABASE_URL nicht gesetzt – DB-Tests übersprungen")
    return TEST_DSN


@pytest.fixture
async def db(dsn):
    """Frisches Schema pro Test.

    Kein importlib.reload: `from ..config import settings` bindet das Objekt
    beim Import. Ein Reload erzeugt ein zweites settings-Objekt, und die
    Module halten dann unterschiedliche Instanzen - Änderungen daran (auch per
    monkeypatch) wirken dann nur in der Hälfte des Codes. Stattdessen wird das
    bestehende Objekt verändert.
    """
    from aimon import db as db_mod
    from aimon.config import settings
    from aimon.tsdb import series as series_mod

    await db_mod.close_pool()
    settings.database_url = dsn

    await db_mod.init_pool()
    async with db_mod.pool().acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db_mod.migrate()
    series_mod.cache_clear()
    yield db_mod
    await db_mod.close_pool()
    series_mod.cache_clear()
