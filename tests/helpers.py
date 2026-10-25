"""Shared helpers for phase-2 tests."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from nova.core.schemas import Memory, MemoryType, Message

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def mem(
    mem_id: str,
    mtype: MemoryType = MemoryType.DECISION,
    subject: str = "db",
    value: str = "PostgreSQL",
    minutes: int = 0,
    session: str = "s1",
) -> Memory:
    """Build a valid Memory at T0 + minutes."""
    return Memory(
        id=mem_id,
        type=mtype,
        subject=subject,
        value=value,
        source_msg_id=f"msg-{mem_id}",
        source_quote=value,
        session_id=session,
        timestamp=T0 + timedelta(minutes=minutes),
    )


def msg(msg_id: str, text: str, minutes: int = 0, session: str = "s1") -> Message:
    """Build a valid user Message."""
    return Message(
        id=msg_id,
        session_id=session,
        role="user",
        text=text,
        timestamp=T0 + timedelta(minutes=minutes),
    )


@contextmanager
def track_sqlite_connections() -> Iterator[list[sqlite3.Connection]]:
    """Record every sqlite3 connection opened inside the block."""
    opened: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def tracking(*args, **kwargs):  # type: ignore[no-untyped-def]
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn

    sqlite3.connect = tracking  # type: ignore[assignment]
    try:
        yield opened
    finally:
        sqlite3.connect = real  # type: ignore[assignment]


def is_open(conn: sqlite3.Connection) -> bool:
    """Return True if the connection is still usable."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True
