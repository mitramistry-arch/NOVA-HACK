"""Minimal SQL database abstraction shared by the SQLite and PostgreSQL backends.

SQL is written once with ``?`` placeholders. Each dialect adapts placeholders and DDL.
A single re-entrant transaction scope gives callers one place that owns atomicity.
"""

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nova.store.base import StoreError

Row = dict[str, Any]


class Database(ABC):
    """A single connection with explicit, re-entrant transactions."""

    dialect: str = "abstract"
    order_column: str = "rowid"

    def __init__(self) -> None:
        """Initialize the transaction bookkeeping."""
        self._lock = threading.RLock()
        self._depth = 0
        self._failed = False
        self._closed = False

    @abstractmethod
    def _run(self, sql: str, params: Sequence[Any]) -> tuple[list[Row], int]:
        """Execute one statement and return (rows, rowcount)."""

    @abstractmethod
    def _begin(self) -> None:
        """Start a transaction."""

    @abstractmethod
    def _commit(self) -> None:
        """Commit the open transaction."""

    @abstractmethod
    def _rollback(self) -> None:
        """Roll back the open transaction."""

    @abstractmethod
    def _close(self) -> None:
        """Close the underlying connection."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement and return the number of affected rows."""
        with self._lock:
            return self._run(sql, params)[1]

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Run a query and return all rows as dictionaries."""
        with self._lock:
            return self._run(sql, params)[0]

    @property
    def in_transaction(self) -> bool:
        """Return True while a transaction scope is open."""
        return self._depth > 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Open a transaction scope; nested scopes join the outermost one.

        Any exception leaving an inner scope poisons the whole transaction, so the outermost
        scope rolls back even if the caller swallowed the inner error.
        """
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._failed = False
                self._begin()
            self._depth += 1
            try:
                yield
            except BaseException:
                self._failed = True
                raise
            finally:
                self._depth -= 1
                if outer:
                    if self._failed:
                        self._rollback()
                    else:
                        try:
                            self._commit()
                        except BaseException:
                            self._rollback()
                            raise

    def close(self) -> None:
        """Close the connection; safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._close()

    @property
    def closed(self) -> bool:
        """Return True once close() has been called."""
        return self._closed


class SqliteDatabase(Database):
    """SQLite connection in autocommit mode with explicit transactions."""

    dialect = "sqlite"
    order_column = "rowid"

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Open the database file (WAL mode for files)."""
        super().__init__()
        self.db_path = str(db_path)
        self.conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
                isolation_level=None,
            )
            self.conn = conn
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
        except sqlite3.Error as e:
            self.close()
            raise StoreError(f"Failed to open SQLite database: {e}") from e

    def _run(self, sql: str, params: Sequence[Any]) -> tuple[list[Row], int]:
        if self.conn is None:
            raise StoreError("Database connection is closed")
        try:
            cur = self.conn.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            return rows, cur.rowcount
        except sqlite3.ProgrammingError as e:
            raise StoreError(f"Database error: {e}") from e
        except sqlite3.Error as e:
            raise StoreError(f"Database error: {e}") from e

    def _begin(self) -> None:
        self._run("BEGIN IMMEDIATE", ())

    def _commit(self) -> None:
        self._run("COMMIT", ())

    def _rollback(self) -> None:
        if self.conn is not None and self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def _close(self) -> None:
        if self.conn is not None:
            self.conn.close()
