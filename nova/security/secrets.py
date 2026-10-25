"""Encrypted secret storage and per-tenant integration configuration (the control plane).

API keys and database passwords are encrypted with AES-256-GCM under a deployment-managed key
(``NOVA_ENCRYPTION_KEY``; older keys in ``NOVA_ENCRYPTION_KEYS_PREVIOUS`` still decrypt and are
re-wrapped by ``rewrap_all``). Each ciphertext is bound to its tenant and name through the AEAD
associated data, so a row copied to another tenant cannot be decrypted. Plaintext secrets exist
only in memory while a request is being served; there is no plaintext fallback. A KMS can be
used by providing another ``SecretCipher`` implementation.
"""

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nova.core.tenant import validate_tenant_id
from nova.store.base import StoreError
from nova.store.database import Database, SqliteDatabase


class SecretStoreError(Exception):
    """Base class for secret storage failures (messages never contain secret values)."""


class SecretStoreUnavailableError(SecretStoreError):
    """No usable encryption key is configured."""


class SecretNotFoundError(SecretStoreError):
    """The referenced secret does not exist for this tenant."""


class SecretDecryptionError(SecretStoreError):
    """A stored secret could not be decrypted (wrong key or tampered data)."""


class SecretCipher(Protocol):
    """Encrypts and decrypts secrets; implement this to plug in a KMS."""

    def encrypt(self, plaintext: str, aad: str) -> str:
        """Return an opaque ciphertext string bound to ``aad``."""
        ...

    def decrypt(self, ciphertext: str, aad: str) -> str:
        """Return the plaintext, raising SecretDecryptionError on failure."""
        ...

    def is_current(self, ciphertext: str) -> bool:
        """Return True if the ciphertext uses the primary key."""
        ...


def _decode_key(text: str) -> bytes:
    """Decode a URL-safe key strictly; malformed characters must not be silently ignored."""
    cleaned = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cleaned):
        raise SecretStoreUnavailableError("encryption key is not valid base64")
    unpadded = cleaned.rstrip("=")
    supplied_padding = cleaned[len(unpadded) :]
    required_padding = "=" * (-len(unpadded) % 4)
    if len(required_padding) > 2 or (supplied_padding and supplied_padding != required_padding):
        raise SecretStoreUnavailableError("encryption key is not valid base64")
    try:
        key = base64.b64decode(
            (unpadded + required_padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as e:
        raise SecretStoreUnavailableError("encryption key is not valid base64") from e
    if len(key) != 32:
        raise SecretStoreUnavailableError("encryption key must decode to 32 bytes")
    return key


def _kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def generate_key() -> str:
    """Return a new random encryption key suitable for NOVA_ENCRYPTION_KEY."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class AesGcmCipher:
    """AES-256-GCM with a primary key and optional previous keys for rotation."""

    def __init__(self, primary_key: str | None, previous_keys: str | None = None) -> None:
        """Load keys; raises SecretStoreUnavailableError when no primary key is configured."""
        if not primary_key:
            raise SecretStoreUnavailableError("NOVA_ENCRYPTION_KEY is not configured")
        primary = _decode_key(primary_key)
        self._primary_id = _kid(primary)
        self._keys: dict[str, bytes] = {self._primary_id: primary}
        for part in (previous_keys or "").split(","):
            if part.strip():
                old = _decode_key(part)
                self._keys[_kid(old)] = old

    def encrypt(self, plaintext: str, aad: str) -> str:
        """Encrypt under the primary key with a fresh random nonce."""
        nonce = os.urandom(12)
        ct = AESGCM(self._keys[self._primary_id]).encrypt(nonce, plaintext.encode(), aad.encode())
        return f"v1:{self._primary_id}:{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, ciphertext: str, aad: str) -> str:
        """Decrypt with whichever configured key produced the ciphertext."""
        try:
            version, kid, payload = ciphertext.split(":", 2)
            key = self._keys[kid]
            raw = base64.urlsafe_b64decode(payload)
            if version != "v1":
                raise KeyError(version)
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
        except (ValueError, KeyError, InvalidTag) as e:
            raise SecretDecryptionError("stored secret could not be decrypted") from e

    def is_current(self, ciphertext: str) -> bool:
        """Return True if encrypted under the primary key."""
        return ciphertext.split(":", 2)[1:2] == [self._primary_id]


def mask_secret(value: str) -> str:
    """Return a masked form that reveals at most the last four characters of long secrets."""
    return f"****{value[-4:]}" if len(value) >= 16 else "****"


_CONTROL_DDL = (
    (
        "CREATE TABLE IF NOT EXISTS secrets (tenant_id TEXT NOT NULL, name TEXT NOT NULL,"
        " ciphertext TEXT NOT NULL, mask TEXT NOT NULL, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, name))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS integration_configs (tenant_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (tenant_id, kind))"
    ),
)


class ControlStore:
    """Deployment-side SQLite store for encrypted secrets and tenant integration configs."""

    def __init__(self, db_path: str | Path, cipher: SecretCipher | None) -> None:
        """Open the control database; ``cipher`` may be None (secret operations then fail)."""
        self._cipher = cipher
        self.db: Database = SqliteDatabase(db_path)
        try:
            with self.db.transaction():
                for stmt in _CONTROL_DDL:
                    self.db.execute(stmt)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        """Close the control database."""
        self.db.close()

    def check(self) -> None:
        """Raise StoreError if the control database is unusable."""
        self.db.query("SELECT 1 AS ok")

    def _require_cipher(self) -> SecretCipher:
        if self._cipher is None:
            raise SecretStoreUnavailableError("NOVA_ENCRYPTION_KEY is not configured")
        return self._cipher

    @staticmethod
    def _aad(tenant_id: str, name: str) -> str:
        return f"{tenant_id}|{name}"

    # ---- secrets ------------------------------------------------------------------------
    def put_secret(self, tenant_id: str, name: str, value: str) -> str:
        """Encrypt and store (or rotate) a secret; returns its masked form."""
        validate_tenant_id(tenant_id)
        cipher = self._require_cipher()
        now = datetime.now(UTC).isoformat()
        mask = mask_secret(value)
        ct = cipher.encrypt(value, self._aad(tenant_id, name))
        self.db.execute(
            "INSERT INTO secrets (tenant_id, name, ciphertext, mask, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (tenant_id, name) DO UPDATE SET"
            " ciphertext = excluded.ciphertext, mask = excluded.mask,"
            " updated_at = excluded.updated_at",
            (tenant_id, name, ct, mask, now, now),
        )
        return mask

    def get_secret(self, tenant_id: str, name: str) -> str:
        """Decrypt and return a secret for server-side use only."""
        cipher = self._require_cipher()
        rows = self.db.query(
            "SELECT ciphertext FROM secrets WHERE tenant_id = ? AND name = ?", (tenant_id, name)
        )
        if not rows:
            raise SecretNotFoundError(f"secret '{name}' is not set")
        return cipher.decrypt(rows[0]["ciphertext"], self._aad(tenant_id, name))

    def secret_mask(self, tenant_id: str, name: str) -> str | None:
        """Return the masked readback of a secret, or None if unset."""
        rows = self.db.query(
            "SELECT mask FROM secrets WHERE tenant_id = ? AND name = ?", (tenant_id, name)
        )
        return rows[0]["mask"] if rows else None

    def delete_secret(self, tenant_id: str, name: str) -> bool:
        """Delete a secret; returns True if one existed."""
        return (
            self.db.execute(
                "DELETE FROM secrets WHERE tenant_id = ? AND name = ?", (tenant_id, name)
            )
            > 0
        )

    def rewrap_all(self) -> int:
        """Re-encrypt every secret under the primary key; returns how many were rewritten."""
        cipher = self._require_cipher()
        rewritten = 0
        with self.db.transaction():
            for row in self.db.query("SELECT tenant_id, name, ciphertext FROM secrets"):
                if cipher.is_current(row["ciphertext"]):
                    continue
                aad = self._aad(row["tenant_id"], row["name"])
                fresh = cipher.encrypt(cipher.decrypt(row["ciphertext"], aad), aad)
                self.db.execute(
                    "UPDATE secrets SET ciphertext = ? WHERE tenant_id = ? AND name = ?",
                    (fresh, row["tenant_id"], row["name"]),
                )
                rewritten += 1
        return rewritten

    # ---- non-secret integration config -------------------------------------------------
    def save_config(self, tenant_id: str, kind: str, body: dict[str, object]) -> None:
        """Store a tenant's non-secret configuration document."""
        validate_tenant_id(tenant_id)
        self.db.execute(
            "INSERT INTO integration_configs (tenant_id, kind, body, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT (tenant_id, kind) DO UPDATE SET body = excluded.body,"
            " updated_at = excluded.updated_at",
            (tenant_id, kind, json.dumps(body), datetime.now(UTC).isoformat()),
        )

    def load_config(self, tenant_id: str, kind: str) -> dict[str, object] | None:
        """Return a tenant's configuration document, or None."""
        rows = self.db.query(
            "SELECT body FROM integration_configs WHERE tenant_id = ? AND kind = ?",
            (tenant_id, kind),
        )
        return json.loads(rows[0]["body"]) if rows else None

    def delete_config(self, tenant_id: str, kind: str) -> bool:
        """Delete a configuration document; returns True if one existed."""
        return (
            self.db.execute(
                "DELETE FROM integration_configs WHERE tenant_id = ? AND kind = ?",
                (tenant_id, kind),
            )
            > 0
        )


__all__ = [
    "AesGcmCipher",
    "ControlStore",
    "SecretCipher",
    "SecretDecryptionError",
    "SecretNotFoundError",
    "SecretStoreError",
    "SecretStoreUnavailableError",
    "StoreError",
    "generate_key",
    "mask_secret",
]
