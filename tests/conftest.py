"""Shared test isolation and SQLite resource cleanup."""

from collections.abc import Iterator
from typing import Any

import pytest

from nova.store.database import SqliteDatabase


@pytest.fixture(autouse=True)
def _test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use explicit test mode unless a test deliberately overrides it."""
    monkeypatch.setenv("NOVA_ENV", "test")


@pytest.fixture(autouse=True)
def _close_sqlite_databases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close SQLite database objects created by each test, even after assertion failures."""
    opened: list[SqliteDatabase] = []
    original = SqliteDatabase.__init__

    def tracking_init(self: SqliteDatabase, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(SqliteDatabase, "__init__", tracking_init)
    yield
    for db in reversed(opened):
        db.close()
