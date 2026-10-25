"""Session lifecycle tests: hashed tokens, idle/absolute expiry, rotation, revocation, CSRF."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nova.identity.errors import SessionInvalid
from nova.identity.models import Principal
from nova.identity.roles import Role
from nova.identity.session import SessionStore, hash_session_token
from nova.store.database import SqliteDatabase


class FakeClock:
    """Deterministic clock for expiry tests."""

    def __init__(self, start: datetime | None = None) -> None:
        """Start at a fixed instant."""
        self.current = start or datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.current

    def advance(self, **kwargs: float) -> None:
        """Move time forward."""
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    """Provide a fake clock shared by the store under test."""
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> SessionStore:
    """Provide a store backed by a temporary SQLite control database."""
    db = SqliteDatabase(tmp_path / "control.db")
    return SessionStore(db, clock=clock)


def _principal(tenant: str = "tenant-a", role: Role = Role.EDITOR) -> Principal:
    """Build a principal for session tests."""
    return Principal(subject="subject-1", tenant_id=tenant, role=role, email="user@example.test")


def test_created_session_round_trips_tenant_and_role(store: SessionStore) -> None:
    """A new session validates and returns the principal's tenant and role."""
    issued = store.create(_principal())
    record = store.load(issued.token)
    assert record.tenant_id == "tenant-a"
    assert record.role is Role.EDITOR
    assert record.subject == "subject-1"
    assert record.principal.tenant_id == "tenant-a"
    assert record.principal.role is Role.EDITOR


def test_raw_session_token_is_never_stored(store: SessionStore) -> None:
    """Only the hash of the token is persisted, never the bearer value."""
    issued = store.create(_principal())
    rows = store.db.query("SELECT token_hash FROM browser_sessions")
    assert [row["token_hash"] for row in rows] == [hash_session_token(issued.token)]
    assert issued.token not in {row["token_hash"] for row in rows}
    stored = store.db.query("SELECT * FROM browser_sessions")[0]
    assert issued.token not in repr(stored)
    assert issued.csrf_token not in repr(stored)


def test_absolute_expiry_invalidates_session(store: SessionStore, clock: FakeClock) -> None:
    """A session cannot outlive its absolute lifetime even when used continuously."""
    issued = store.create(_principal())
    for _ in range(23):
        clock.advance(minutes=20)
        store.load(issued.token)
    clock.advance(minutes=21)
    with pytest.raises(SessionInvalid):
        store.load(issued.token)


def test_idle_expiry_invalidates_session(store: SessionStore, clock: FakeClock) -> None:
    """A session unused past the idle window is rejected."""
    issued = store.create(_principal())
    clock.advance(minutes=31)
    with pytest.raises(SessionInvalid):
        store.load(issued.token)


def test_activity_extends_the_idle_window(store: SessionStore, clock: FakeClock) -> None:
    """Use within the idle window keeps the session alive until the absolute limit."""
    issued = store.create(_principal())
    for _ in range(18):
        clock.advance(minutes=25)
        store.load(issued.token)
    clock.advance(minutes=31)
    with pytest.raises(SessionInvalid):
        store.load(issued.token)


def test_expired_session_row_is_removed(store: SessionStore, clock: FakeClock) -> None:
    """Expired sessions are deleted rather than left behind as reusable rows."""
    issued = store.create(_principal())
    clock.advance(minutes=31)
    with pytest.raises(SessionInvalid):
        store.load(issued.token)
    assert store.db.query("SELECT * FROM browser_sessions") == []


def test_revocation_is_immediate(store: SessionStore) -> None:
    """A revoked session cannot be used again."""
    issued = store.create(_principal())
    assert store.revoke(issued.token) is True
    with pytest.raises(SessionInvalid):
        store.load(issued.token)
    assert store.revoke(issued.token) is False


def test_rotation_invalidates_the_previous_token(store: SessionStore) -> None:
    """Rotation returns a new token and kills the old one."""
    issued = store.create(_principal())
    rotated = store.rotate(issued.token)
    assert rotated.token != issued.token
    assert rotated.csrf_token != issued.csrf_token
    with pytest.raises(SessionInvalid):
        store.load(issued.token)
    assert store.load(rotated.token).tenant_id == "tenant-a"


def test_csrf_token_is_bound_to_its_session(store: SessionStore) -> None:
    """A CSRF token from another session must not validate."""
    first = store.create(_principal())
    second = store.create(_principal())
    record = store.load(first.token)
    assert record.verify_csrf(first.csrf_token) is True
    assert record.verify_csrf(second.csrf_token) is False
    assert record.verify_csrf("") is False


def test_revoke_subject_removes_every_session(store: SessionStore) -> None:
    """Account-level revocation clears all sessions for the subject."""
    first = store.create(_principal())
    second = store.create(_principal())
    other = store.create(Principal(subject="subject-2", tenant_id="tenant-a", role=Role.VIEWER))
    assert store.revoke_subject("subject-1") == 2
    for token in (first.token, second.token):
        with pytest.raises(SessionInvalid):
            store.load(token)
    assert store.load(other.token).subject == "subject-2"


def test_purge_removes_only_dead_sessions(store: SessionStore, clock: FakeClock) -> None:
    """Startup cleanup removes idle-expired rows and keeps live ones."""
    live = store.create(_principal())
    dead = store.create(Principal(subject="subject-2", tenant_id="tenant-a", role=Role.VIEWER))
    store.revoke(dead.token)
    clock.advance(seconds=1)
    assert store.purge_expired() == 1
    assert store.load(live.token).subject == "subject-1"


def test_login_transaction_is_single_use(store: SessionStore) -> None:
    """A replayed OIDC state value is rejected (login CSRF / replay defence)."""
    state = store.start_login(nonce="n", code_verifier="v", next_path="/workspace")
    transaction = store.consume_login(state)
    assert transaction.nonce == "n"
    assert transaction.next_path == "/workspace"
    with pytest.raises(SessionInvalid):
        store.consume_login(state)


def test_expired_login_transaction_is_rejected(store: SessionStore, clock: FakeClock) -> None:
    """Login transactions expire quickly and are then unusable."""
    state = store.start_login(nonce="n", code_verifier="v", next_path="/")
    clock.advance(minutes=6)
    with pytest.raises(SessionInvalid):
        store.consume_login(state)


def test_unknown_state_is_rejected(store: SessionStore) -> None:
    """An attacker-supplied state value never starts a login."""
    with pytest.raises(SessionInvalid):
        store.consume_login("not-a-real-state")
    with pytest.raises(SessionInvalid):
        store.consume_login("")


def test_non_positive_lifetimes_are_rejected(tmp_path: Path) -> None:
    """Misconfigured lifetimes fail loudly instead of creating immortal sessions."""
    db = SqliteDatabase(tmp_path / "control.db")
    with pytest.raises(ValueError):
        SessionStore(db, absolute_ttl=timedelta(0))
    with pytest.raises(ValueError):
        SessionStore(db, idle_ttl=timedelta(seconds=-1))
