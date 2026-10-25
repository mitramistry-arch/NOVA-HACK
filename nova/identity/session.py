"""Opaque server-side sessions: hashed tokens, idle and absolute expiry, rotation, revocation."""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from nova.identity.errors import SessionInvalid
from nova.identity.models import (
    IssuedSession,
    LoginTransaction,
    Principal,
    SessionRecord,
    hash_csrf_token,
)
from nova.identity.roles import Role
from nova.store.database import Database
from nova.store.timeutil import format_ts, parse_ts

SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
DEFAULT_ABSOLUTE_TTL = timedelta(hours=8)
DEFAULT_IDLE_TTL = timedelta(minutes=30)
LOGIN_TRANSACTION_TTL = timedelta(minutes=5)
MAX_LOGIN_NEXT_CHARS = 512

_SESSION_DDL = (
    (
        "CREATE TABLE IF NOT EXISTS browser_sessions (token_hash TEXT PRIMARY KEY,"
        " subject TEXT NOT NULL, tenant_id TEXT NOT NULL, role TEXT NOT NULL,"
        " email TEXT, csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL,"
        " last_seen_at TEXT NOT NULL, absolute_expires_at TEXT NOT NULL,"
        " revoked_at TEXT)"
    ),
    ("CREATE INDEX IF NOT EXISTS browser_sessions_subject ON browser_sessions (subject)"),
    (
        "CREATE TABLE IF NOT EXISTS identity_logins (state_hash TEXT PRIMARY KEY,"
        " nonce TEXT NOT NULL, code_verifier TEXT NOT NULL, next_path TEXT NOT NULL,"
        " created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    ),
)


def _new_token(nbytes: int) -> str:
    """Return a URL-safe token with at least 256 bits of entropy."""
    return secrets.token_urlsafe(nbytes)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage (the raw token exists only in the browser cookie)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """Persists browser sessions and in-flight login transactions in the control database."""

    def __init__(
        self,
        db: Database,
        *,
        absolute_ttl: timedelta = DEFAULT_ABSOLUTE_TTL,
        idle_ttl: timedelta = DEFAULT_IDLE_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind to the control database and create the identity tables if needed."""
        if absolute_ttl <= timedelta(0) or idle_ttl <= timedelta(0):
            raise ValueError("session lifetimes must be positive")
        self.db = db
        self.absolute_ttl = absolute_ttl
        self.idle_ttl = idle_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.db.transaction():
            for statement in _SESSION_DDL:
                self.db.execute(statement)

    def now(self) -> datetime:
        """Return the current time from the injected clock."""
        return self._clock()

    # ---- sessions -----------------------------------------------------------------------
    def create(self, principal: Principal) -> IssuedSession:
        """Create a session for a principal and return the one-time raw tokens."""
        now = self.now()
        token = _new_token(SESSION_TOKEN_BYTES)
        csrf_token = _new_token(CSRF_TOKEN_BYTES)
        record = SessionRecord(
            token_hash=hash_session_token(token),
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            csrf_hash=hash_csrf_token(csrf_token),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + self.absolute_ttl,
        )
        self._insert(record)
        return IssuedSession(token=token, csrf_token=csrf_token, record=record)

    def _insert(self, record: SessionRecord) -> None:
        """Insert a session row."""
        self.db.execute(
            "INSERT INTO browser_sessions (token_hash, subject, tenant_id, role, email,"
            " csrf_hash, created_at, last_seen_at, absolute_expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record.token_hash,
                record.subject,
                record.tenant_id,
                record.role.value,
                record.email,
                record.csrf_hash,
                format_ts(record.created_at),
                format_ts(record.last_seen_at),
                format_ts(record.absolute_expires_at),
            ),
        )

    def load(self, token: str) -> SessionRecord:
        """Validate a session token and return its record, raising SessionInvalid otherwise."""
        if not token or len(token) > 512:
            raise SessionInvalid("missing_token")
        token_hash = hash_session_token(token)
        rows = self.db.query(
            "SELECT token_hash, subject, tenant_id, role, email, csrf_hash, created_at,"
            " last_seen_at, absolute_expires_at, revoked_at"
            " FROM browser_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        if not rows:
            raise SessionInvalid("unknown")
        row = rows[0]
        if row["revoked_at"] is not None:
            raise SessionInvalid("revoked")
        now = self.now()
        if now >= parse_ts(row["absolute_expires_at"]):
            self._delete(token_hash)
            raise SessionInvalid("absolute_expired")
        if now >= parse_ts(row["last_seen_at"]) + self.idle_ttl:
            self._delete(token_hash)
            raise SessionInvalid("idle_expired")
        self.db.execute(
            "UPDATE browser_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (format_ts(now), token_hash),
        )
        return SessionRecord(
            token_hash=row["token_hash"],
            subject=row["subject"],
            tenant_id=row["tenant_id"],
            role=Role(row["role"]),
            email=row["email"],
            csrf_hash=row["csrf_hash"],
            created_at=parse_ts(row["created_at"]),
            last_seen_at=now,
            absolute_expires_at=parse_ts(row["absolute_expires_at"]),
        )

    def rotate(self, token: str) -> IssuedSession:
        """Replace a session token, invalidating the old one immediately."""
        record = self.load(token)
        self._delete(record.token_hash)
        issued = self.create(record.principal)
        return issued

    def revoke(self, token: str) -> bool:
        """Mark a session revoked so the token cannot be reused; the row is purged later."""
        return (
            self.db.execute(
                "UPDATE browser_sessions SET revoked_at = ?"
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (format_ts(self.now()), hash_session_token(token)),
            )
            > 0
        )

    def revoke_subject(self, subject: str) -> int:
        """Delete every session for one subject (for example after an account change)."""
        return self.db.execute("DELETE FROM browser_sessions WHERE subject = ?", (subject,))

    def _delete(self, token_hash: str) -> bool:
        """Delete one session row by token hash."""
        return (
            self.db.execute("DELETE FROM browser_sessions WHERE token_hash = ?", (token_hash,)) > 0
        )

    def purge_expired(self) -> int:
        """Delete revoked and expired sessions; returns how many rows were removed."""
        now = self.now()
        absolute_cutoff = format_ts(now)
        idle_cutoff = format_ts(now - self.idle_ttl)
        return self.db.execute(
            "DELETE FROM browser_sessions WHERE revoked_at IS NOT NULL"
            " OR absolute_expires_at <= ? OR last_seen_at <= ?",
            (absolute_cutoff, idle_cutoff),
        )

    def active_session_count(self, subject: str) -> int:
        """Return how many live sessions a subject currently holds."""
        rows = self.db.query(
            "SELECT COUNT(*) AS c FROM browser_sessions WHERE subject = ?"
            " AND revoked_at IS NULL AND absolute_expires_at > ? AND last_seen_at > ?",
            (subject, format_ts(self.now()), format_ts(self.now() - self.idle_ttl)),
        )
        return int(rows[0]["c"])

    # ---- login transactions -------------------------------------------------------------
    def start_login(self, *, nonce: str, code_verifier: str, next_path: str) -> str:
        """Store a single-use OIDC login transaction and return its opaque state value."""
        if len(next_path) > MAX_LOGIN_NEXT_CHARS:
            raise ValueError("login redirect target is too long")
        now = self.now()
        state = _new_token(SESSION_TOKEN_BYTES)
        self.db.execute(
            "INSERT INTO identity_logins (state_hash, nonce, code_verifier, next_path,"
            " created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (
                hash_session_token(state),
                nonce,
                code_verifier,
                next_path,
                format_ts(now),
                format_ts(now + LOGIN_TRANSACTION_TTL),
            ),
        )
        return state

    def consume_login(self, state: str) -> LoginTransaction:
        """Consume a login transaction exactly once, raising SessionInvalid if unusable."""
        if not state or len(state) > 512:
            raise SessionInvalid("missing_state")
        state_hash = hash_session_token(state)
        rows = self.db.query(
            "SELECT nonce, code_verifier, next_path, expires_at FROM identity_logins"
            " WHERE state_hash = ?",
            (state_hash,),
        )
        if not rows:
            raise SessionInvalid("unknown_state")
        self.db.execute("DELETE FROM identity_logins WHERE state_hash = ?", (state_hash,))
        row = rows[0]
        expires_at = parse_ts(row["expires_at"])
        if self.now() >= expires_at:
            raise SessionInvalid("state_expired")
        return LoginTransaction(
            nonce=row["nonce"],
            code_verifier=row["code_verifier"],
            next_path=row["next_path"],
            expires_at=expires_at,
        )

    def constant_time_equals(self, left: str, right: str) -> bool:
        """Compare two token strings without leaking timing information."""
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


__all__ = [
    "CSRF_TOKEN_BYTES",
    "DEFAULT_ABSOLUTE_TTL",
    "DEFAULT_IDLE_TTL",
    "LOGIN_TRANSACTION_TTL",
    "SESSION_TOKEN_BYTES",
    "SessionStore",
    "hash_session_token",
]
