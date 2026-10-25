"""Schema definitions and migrations for the SQLite and PostgreSQL backends."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from nova.store.base import SchemaMigrationError, SchemaVersionError, StoreError
from nova.store.database import Database, SqliteDatabase
from nova.store.timeutil import format_ts, parse_ts

logger = logging.getLogger("nova.store.schema")

DEFAULT_TENANT = "default"
#: Kept for callers that predate dialect-specific versions; equals the SQLite version.
SCHEMA_VERSION = 3

_SQLITE_V1_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,
    value TEXT NOT NULL,
    reason TEXT,
    source_msg_id TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current',
    supersedes_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    embedding BLOB
)
"""

_MEMORIES_V2 = """
CREATE TABLE {name} (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    type TEXT NOT NULL
        CHECK (type IN ('fact', 'decision', 'correction', 'preference', 'commitment')),
    subject TEXT NOT NULL CHECK (length(subject) > 0),
    value TEXT NOT NULL,
    reason TEXT,
    source_msg_id TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'superseded')),
    supersedes_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    embedding BLOB,
    embedding_model TEXT,
    embedding_dim INTEGER,
    PRIMARY KEY (tenant_id, id),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id),
    CHECK ((embedding IS NULL) = (embedding_dim IS NULL)),
    FOREIGN KEY (tenant_id, supersedes_id) REFERENCES memories (tenant_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""

_AUDIT_V2 = """
CREATE TABLE {name} (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    retrieved_ids TEXT NOT NULL,
    verdicts TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    PRIMARY KEY (tenant_id, id)
)
"""

_SQLITE_V2_INDEXES = (
    "CREATE INDEX idx_memories_tenant_subject ON memories (tenant_id, subject, status)",
    "CREATE INDEX idx_memories_tenant_session ON memories (tenant_id, session_id)",
    (
        "CREATE UNIQUE INDEX uq_memories_successor ON memories (tenant_id, supersedes_id) "
        "WHERE supersedes_id IS NOT NULL"
    ),
    "CREATE INDEX idx_audit_tenant_time ON audit_log (tenant_id, timestamp)",
)

_SQLITE_TRIGGER = """
CREATE TRIGGER {name} BEFORE {event} ON memories
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supersession subject mismatch') WHERE EXISTS (
        SELECT 1 FROM memories p WHERE p.tenant_id = NEW.tenant_id
        AND p.id = NEW.supersedes_id AND p.subject <> NEW.subject);
    SELECT RAISE(ABORT, 'supersession direction invalid') WHERE EXISTS (
        SELECT 1 FROM memories p WHERE p.tenant_id = NEW.tenant_id
        AND p.id = NEW.supersedes_id AND (p.timestamp, p.id) >= (NEW.timestamp, NEW.id));
END
"""


def _table_exists(db: Database, name: str) -> bool:
    """Return True when ``name`` exists in this connection's schema (dialect-aware)."""
    if db.dialect == "sqlite":
        rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        return bool(rows)
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (name,),
    )
    return bool(rows)


def _sanitize_legacy_links(rows: list[dict]) -> int:
    """Null out legacy supersession links that violate the v2 integrity rules."""
    by_id = {r["id"]: r for r in rows}
    claimed: set[str] = set()
    fixed = 0
    for r in sorted(rows, key=lambda x: (x["timestamp"], x["id"])):
        parent_id = r["supersedes_id"]
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        bad = (
            parent is None
            or parent_id == r["id"]
            or parent["subject"] != r["subject"]
            or (parent["timestamp"], parent["id"]) >= (r["timestamp"], r["id"])
            or parent_id in claimed
        )
        if bad:
            r["supersedes_id"] = None
            fixed += 1
        else:
            claimed.add(parent_id)
    return fixed


def _migrate_sqlite_v1(db: Database) -> None:
    db.execute(_SQLITE_V1_MEMORIES)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_subject_status ON memories (subject, status)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories (session_id)")


def _migrate_sqlite_v2(db: Database) -> None:
    """Add tenants, embedding metadata, integrity constraints, and UTC-normalized timestamps."""
    legacy = db.query("SELECT * FROM memories")
    for r in legacy:
        r["timestamp"] = format_ts(parse_ts(r["timestamp"]))
    repaired = _sanitize_legacy_links(legacy)
    if repaired:
        logger.warning("migration v2 cleared %d invalid supersession links", repaired)

    db.execute("ALTER TABLE memories RENAME TO memories_legacy")
    db.execute(_MEMORIES_V2.format(name="memories"))
    for r in legacy:
        blob = r["embedding"]
        usable = blob is not None and len(blob) > 0 and len(blob) % 4 == 0
        db.execute(
            "INSERT INTO memories (tenant_id, id, type, subject, value, reason, source_msg_id,"
            " source_quote, session_id, timestamp, status, supersedes_id, confidence, embedding,"
            " embedding_model, embedding_dim) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                DEFAULT_TENANT,
                r["id"],
                r["type"],
                r["subject"],
                r["value"],
                r["reason"],
                r["source_msg_id"],
                r["source_quote"],
                r["session_id"],
                r["timestamp"],
                r["status"],
                r["supersedes_id"],
                r["confidence"],
                bytes(blob) if usable else None,
                None,
                len(blob) // 4 if usable else None,
            ),
        )
    db.execute("DROP TABLE memories_legacy")

    had_audit = _table_exists(db, "audit_log")
    if had_audit:
        db.execute("ALTER TABLE audit_log RENAME TO audit_log_legacy")
    db.execute(_AUDIT_V2.format(name="audit_log"))
    if had_audit:
        db.execute(
            "INSERT INTO audit_log (tenant_id, id, timestamp, question, answer, retrieved_ids,"
            " verdicts, decision_action, latency_ms, prompt_tokens, completion_tokens)"
            " SELECT ?, id, timestamp, question, answer, retrieved_ids, verdicts, decision_action,"
            " latency_ms,"
            " CASE WHEN prompt_tokens = 0 AND completion_tokens = 0 THEN NULL"
            " ELSE prompt_tokens END,"
            " CASE WHEN prompt_tokens = 0 AND completion_tokens = 0 THEN NULL"
            " ELSE completion_tokens END FROM audit_log_legacy",
            (DEFAULT_TENANT,),
        )
        db.execute("DROP TABLE audit_log_legacy")

    for stmt in _SQLITE_V2_INDEXES:
        db.execute(stmt)
    db.execute(_SQLITE_TRIGGER.format(name="trg_memories_supersession_ins", event="INSERT"))
    db.execute(
        _SQLITE_TRIGGER.format(
            name="trg_memories_supersession_upd", event="UPDATE OF supersedes_id"
        )
    )


_RECEIPTS_DDL = """
CREATE TABLE {name} (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('records', 'account')),
    actor TEXT NOT NULL CHECK (length(actor) > 0),
    request_id TEXT,
    deleted_at TEXT NOT NULL,
    memories_deleted INTEGER NOT NULL CHECK (memories_deleted >= 0),
    audit_deleted INTEGER NOT NULL CHECK (audit_deleted >= 0),
    receipts_deleted INTEGER NOT NULL DEFAULT 0 CHECK (receipts_deleted >= 0),
    PRIMARY KEY (tenant_id, id)
)
"""

_RECEIPTS_INDEX = (
    "CREATE INDEX idx_receipts_tenant_time ON deletion_receipts (tenant_id, deleted_at)"
)
#: Export and time-ordered reads scan one tenant in timestamp order.
_MEMORIES_TENANT_TIME_INDEX = (
    "CREATE INDEX idx_memories_tenant_time ON memories (tenant_id, timestamp, id)"
)


def _migrate_sqlite_v3(db: Database) -> None:
    """Add content-free deletion receipts so erasure is auditable after the data is gone."""
    db.execute(_RECEIPTS_DDL.format(name="deletion_receipts"))
    db.execute(_RECEIPTS_INDEX)
    db.execute(_MEMORIES_TENANT_TIME_INDEX)


SQLITE_MIGRATIONS: list[tuple[int, Callable[[Database], None]]] = [
    (1, _migrate_sqlite_v1),
    (2, _migrate_sqlite_v2),
    (3, _migrate_sqlite_v3),
]

POSTGRES_DDL: tuple[str, ...] = (
    """
    CREATE TABLE memories (
        tenant_id TEXT NOT NULL,
        id TEXT NOT NULL,
        type TEXT NOT NULL
            CHECK (type IN ('fact', 'decision', 'correction', 'preference', 'commitment')),
        subject TEXT NOT NULL CHECK (length(subject) > 0),
        value TEXT NOT NULL,
        reason TEXT,
        source_msg_id TEXT NOT NULL,
        source_quote TEXT NOT NULL,
        session_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'superseded')),
        supersedes_id TEXT,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
        embedding BYTEA,
        embedding_model TEXT,
        embedding_dim INTEGER,
        PRIMARY KEY (tenant_id, id),
        CHECK (supersedes_id IS NULL OR supersedes_id <> id),
        CHECK ((embedding IS NULL) = (embedding_dim IS NULL)),
        FOREIGN KEY (tenant_id, supersedes_id) REFERENCES memories (tenant_id, id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    "CREATE INDEX idx_memories_tenant_subject ON memories (tenant_id, subject, status)",
    "CREATE INDEX idx_memories_tenant_session ON memories (tenant_id, session_id)",
    (
        "CREATE UNIQUE INDEX uq_memories_successor ON memories (tenant_id, supersedes_id)"
        " WHERE supersedes_id IS NOT NULL"
    ),
    """
    CREATE TABLE audit_log (
        seq BIGINT GENERATED ALWAYS AS IDENTITY,
        tenant_id TEXT NOT NULL,
        id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        retrieved_ids TEXT NOT NULL,
        verdicts TEXT NOT NULL,
        decision_action TEXT NOT NULL,
        latency_ms DOUBLE PRECISION NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        PRIMARY KEY (tenant_id, id)
    )
    """,
    "CREATE INDEX idx_audit_tenant_time ON audit_log (tenant_id, timestamp)",
    """
    CREATE FUNCTION nova_check_supersession() RETURNS trigger AS $fn$
    DECLARE p RECORD;
    BEGIN
        IF NEW.supersedes_id IS NULL THEN
            RETURN NEW;
        END IF;
        SELECT subject, timestamp, id INTO p FROM memories
            WHERE tenant_id = NEW.tenant_id AND id = NEW.supersedes_id;
        IF FOUND THEN
            IF p.subject <> NEW.subject THEN
                RAISE EXCEPTION 'supersession subject mismatch';
            END IF;
            IF (p.timestamp, p.id) >= (NEW.timestamp, NEW.id) THEN
                RAISE EXCEPTION 'supersession direction invalid';
            END IF;
        END IF;
        RETURN NEW;
    END
    $fn$ LANGUAGE plpgsql
    """,
    (
        "CREATE TRIGGER trg_memories_supersession BEFORE INSERT OR UPDATE OF supersedes_id"
        " ON memories FOR EACH ROW EXECUTE FUNCTION nova_check_supersession()"
    ),
)

POSTGRES_RECEIPTS_DDL: tuple[str, ...] = (
    _RECEIPTS_DDL.format(name="deletion_receipts"),
    _RECEIPTS_INDEX,
    _MEMORIES_TENANT_TIME_INDEX,
)

POSTGRES_MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, POSTGRES_DDL),
    (2, POSTGRES_RECEIPTS_DDL),
]

SQLITE_SCHEMA_VERSION = max(version for version, _ in SQLITE_MIGRATIONS)
POSTGRES_SCHEMA_VERSION = max(version for version, _ in POSTGRES_MIGRATIONS)


def known_version(dialect: str) -> int:
    """Return the highest schema version this build can apply for a dialect."""
    return POSTGRES_SCHEMA_VERSION if dialect == "postgres" else SQLITE_SCHEMA_VERSION


def current_version(db: Database) -> int:
    """Return the schema version recorded in the database, or 0 when uninitialised."""
    try:
        rows = db.query("SELECT MAX(version) AS v FROM schema_version")
    except StoreError:
        return 0
    return int(rows[0]["v"]) if rows and rows[0]["v"] is not None else 0


def migration_plan(db: Database) -> list[int]:
    """Return the versions that ``migrate`` would apply, without applying them."""
    known = (
        [v for v, _ in SQLITE_MIGRATIONS]
        if db.dialect == "sqlite"
        else [v for v, _ in POSTGRES_MIGRATIONS]
    )
    present = current_version(db)
    return [v for v in known if v > present]


def migrate(db: Database) -> int:
    """Apply pending migrations for the database's dialect and return the schema version.

    All pending steps run inside one transaction, so an interrupted or failing migration leaves the
    database exactly as it was. A database written by a newer build is refused rather than used.
    """
    try:
        with db.transaction():
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            rows = db.query("SELECT MAX(version) AS v FROM schema_version")
            current = int(rows[0]["v"]) if rows and rows[0]["v"] is not None else 0
            known = known_version(db.dialect)
            if current > known:
                raise SchemaVersionError(
                    f"database schema version {current} is newer than supported version {known}"
                )
            applied = current
            if db.dialect == "sqlite":
                for version, step in SQLITE_MIGRATIONS:
                    if version > current:
                        step(db)
                        applied = version
                        _record_version(db, version)
            else:
                for version, statements in POSTGRES_MIGRATIONS:
                    if version > current:
                        for stmt in statements:
                            db.execute(stmt)
                        applied = version
                        _record_version(db, version)
            return applied
    except SchemaVersionError:
        raise
    except StoreError as e:
        raise SchemaMigrationError(f"Migration failed: {e}") from e


def _record_version(db: Database, version: int) -> None:
    db.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(UTC).isoformat()),
    )


def open_sqlite(db_path: str) -> SqliteDatabase:
    """Open a SQLite database and migrate it, closing it again if anything fails."""
    db = SqliteDatabase(db_path)
    try:
        migrate(db)
    except BaseException:
        db.close()
        raise
    return db
