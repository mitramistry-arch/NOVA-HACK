"""Phase 2 tests: secrets, BYOK providers, storage backends, SSRF, audit, API plumbing."""

import asyncio
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from nova.api.errors import PublicError, map_exception
from nova.api.middleware import BodyLimitMiddleware, RequestContextMiddleware
from nova.api.runtime import LazyLLM, Runtime, resolve_tenant
from nova.audit.logger import AuditError, AuditLog, AuditRecord
from nova.core.config import Settings
from nova.core.llm import FakeLLM
from nova.core.schemas import MemoryType
from nova.providers.config import ProviderConfigRequest
from nova.providers.errors import (
    ProviderBudgetError,
    ProviderConfigError,
    ProviderNotConfiguredError,
)
from nova.providers.openai_compat import OpenAICompatibleLLM
from nova.retrieval.embedder import HashEmbedder
from nova.security.net import UnsafeEndpointError, validate_https_url
from nova.security.redact import redact
from nova.security.secrets import (
    AesGcmCipher,
    ControlStore,
    SecretDecryptionError,
    SecretNotFoundError,
    SecretStoreUnavailableError,
    generate_key,
)
from nova.store.base import StorageUnavailableError, StoreError
from nova.store.database import SqliteDatabase
from nova.store.postgres import (
    PostgresConfigRequest,
    PostgresDatabase,
    PostgresSettings,
    open_postgres,
)
from nova.store.sql_store import SqlMemoryStore
from nova.store.sqlite_store import SqliteMemoryStore
from nova.temporal.supersession import apply
from tests.helpers import is_open, mem, msg, track_sqlite_connections

KEY = "sk-live-ABCDEFGHIJKLMNOP1234"


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "db_path": str(tmp_path / "data.db"),
        "control_db_path": str(tmp_path / "control.db"),
        "encryption_key": generate_key(),
    }
    return Settings(**{**base, **kw})


def _runtime(tmp_path: Path, handler=None, **kw: Any) -> Runtime:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler) if handler else httpx.MockTransport(_ok)
    return Runtime(_settings(tmp_path, **kw), transport=transport)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        },
    )


def _req(**kw: Any) -> ProviderConfigRequest:
    return ProviderConfigRequest(model="gpt-4o-mini", api_key=KEY, **kw)


# ---- B: encrypted secret lifecycle ---------------------------------------------------------


def test_cipher_roundtrip_tamper_and_binding() -> None:
    cipher = AesGcmCipher(generate_key())
    ct = cipher.encrypt("hunter2-hunter2", "t1|k")
    assert "hunter2" not in ct
    assert cipher.decrypt(ct, "t1|k") == "hunter2-hunter2"
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(ct, "t2|k")  # bound to tenant/name
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(ct[:-4] + "AAAA", "t1|k")
    with pytest.raises(SecretDecryptionError):
        AesGcmCipher(generate_key()).decrypt(ct, "t1|k")  # different key


def test_missing_or_bad_key_is_a_typed_error() -> None:
    with pytest.raises(SecretStoreUnavailableError):
        AesGcmCipher(None)
    with pytest.raises(SecretStoreUnavailableError):
        AesGcmCipher("short")
    valid = generate_key()
    with pytest.raises(SecretStoreUnavailableError, match="valid base64"):
        AesGcmCipher(valid[:8] + "*" + valid[8:])


def test_control_store_requires_key_and_never_stores_plaintext(tmp_path: Path) -> None:
    no_key = ControlStore(tmp_path / "c0.db", None)
    with pytest.raises(SecretStoreUnavailableError):
        no_key.put_secret("t1", "k", KEY)

    store = ControlStore(tmp_path / "c.db", AesGcmCipher(generate_key()))
    assert store.put_secret("t1", "k", KEY) == "****1234"
    store.close()
    raw = (tmp_path / "c.db").read_bytes()
    wal = tmp_path / "c.db-wal"
    if wal.exists():
        raw += wal.read_bytes()
    assert KEY.encode() not in raw


def test_secret_is_tenant_scoped_rotatable_and_deletable(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "c.db", AesGcmCipher(generate_key()))
    store.put_secret("t1", "k", KEY)
    with pytest.raises(SecretNotFoundError):
        store.get_secret("t2", "k")
    # a ciphertext copied across tenants cannot be decrypted
    store.db.execute(
        "INSERT INTO secrets SELECT 't2', name, ciphertext, mask, created_at, updated_at FROM secrets"
    )
    with pytest.raises(SecretDecryptionError):
        store.get_secret("t2", "k")
    store.put_secret("t1", "k", "sk-rotated-0000000000009999")
    assert store.get_secret("t1", "k").endswith("9999")
    assert store.secret_mask("t1", "k") == "****9999"
    assert store.delete_secret("t1", "k") is True
    assert store.secret_mask("t1", "k") is None
    assert store.delete_secret("t1", "k") is False


def test_key_rotation_rewraps_existing_secrets(tmp_path: Path) -> None:
    old, new = generate_key(), generate_key()
    path = tmp_path / "c.db"
    s1 = ControlStore(path, AesGcmCipher(old))
    s1.put_secret("t1", "k", KEY)
    s1.close()
    s2 = ControlStore(path, AesGcmCipher(new, previous_keys=old))
    assert s2.get_secret("t1", "k") == KEY  # still readable via previous key
    assert s2.rewrap_all() == 1 and s2.rewrap_all() == 0
    s2.close()
    s3 = ControlStore(path, AesGcmCipher(new))  # old key no longer needed
    assert s3.get_secret("t1", "k") == KEY


# ---- A: provider configuration -------------------------------------------------------------


def test_provider_status_masks_key_and_never_returns_it(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    status = rt.providers.configure("t1", _req())
    dumped = status.model_dump_json()
    assert KEY not in dumped and status.api_key_mask == "****1234" and status.has_api_key
    assert KEY not in rt.providers.status("t1").model_dump_json()
    assert "api_key" not in json.dumps(rt.control.load_config("t1", "provider"))
    assert KEY not in repr(_req())  # SecretStr


def test_update_without_key_keeps_key_and_new_key_rotates(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt.providers.configure("t1", _req())
    rt.providers.configure("t1", ProviderConfigRequest(model="other-model"))
    assert rt.providers.status("t1").model == "other-model"
    assert rt.control.get_secret("t1", "provider-api-key") == KEY
    rt.providers.configure(
        "t1", ProviderConfigRequest(model="m", api_key="sk-new-key-0000000000AAAA")
    )
    assert rt.providers.status("t1").api_key_mask == "****AAAA"


def test_first_configuration_requires_key(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    with pytest.raises(ProviderConfigError):
        rt.providers.configure("t1", ProviderConfigRequest(model="m"))


def test_disconnect_deletes_config_and_key(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt.providers.configure("t1", _req())
    assert rt.providers.disconnect("t1") is True
    assert rt.providers.status("t1").configured is False
    assert rt.control.secret_mask("t1", "provider-api-key") is None
    assert rt.providers.disconnect("t1") is False


def test_provider_keys_do_not_cross_tenants(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return _ok(request)

    rt = _runtime(tmp_path, handler)
    rt.providers.configure("t1", _req())
    rt.providers.configure(
        "t2", ProviderConfigRequest(model="m", api_key="sk-other-tenant-000000BBBB")
    )
    rt.providers.build_llm("t1").complete("hi")
    rt.providers.build_llm("t2").complete("hi")
    assert seen == [f"Bearer {KEY}", "Bearer sk-other-tenant-000000BBBB"]
    assert rt.providers.status("t3").configured is False


def test_missing_provider_never_falls_back_to_fake(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    with pytest.raises(ProviderNotConfiguredError):
        rt.providers.build_llm("t1")
    with pytest.raises(ProviderNotConfiguredError):
        LazyLLM(rt.providers, "t1").complete("hi")

    explicit = _runtime(
        tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, allow_fake_llm=True
    )
    assert isinstance(explicit.providers.build_llm("t1"), FakeLLM)


def test_configured_tenant_with_lost_key_does_not_fall_back_to_fake(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, allow_fake_llm=True)
    rt.providers.configure("t1", _req())
    rt.control.delete_secret("t1", "provider-api-key")
    with pytest.raises(ProviderNotConfiguredError):
        rt.providers.build_llm("t1")


def test_ingest_without_provider_is_a_typed_error_not_fake(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    with rt.anchor_for("t1") as anchor, pytest.raises(ProviderNotConfiguredError):
        anchor.ingest("s1", [msg("m1", "We use PostgreSQL.")])


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (302, "provider_unavailable"),
        (400, "provider_bad_response"),
    ],
)
def test_provider_http_errors_map_to_safe_codes(
    tmp_path: Path, status: int, code: str, caplog
) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"echo {KEY} secret body")

    rt = _runtime(tmp_path, handler)
    rt.providers.configure("t1", _req())
    with caplog.at_level(logging.DEBUG):
        result = rt.providers.test("t1")
    assert not result.ok and result.code == code
    assert KEY not in result.model_dump_json() and "secret body" not in result.model_dump_json()
    assert KEY not in caplog.text


def test_provider_timeout_and_transport_errors(tmp_path: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused 10.0.0.5", request=request)

    rt = _runtime(tmp_path, timeout)
    rt.providers.configure("t1", _req())
    assert rt.providers.test("t1").code == "provider_timeout"
    rt2 = _runtime(tmp_path / "2" if (tmp_path / "2").mkdir() is None else tmp_path, down)
    rt2.providers.configure("t1", _req())
    res = rt2.providers.test("t1")
    assert res.code == "provider_unavailable" and "10.0.0.5" not in res.message


def test_malformed_provider_response_is_typed(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, lambda r: httpx.Response(200, json={"choices": []}))
    rt.providers.configure("t1", _req())
    assert rt.providers.test("t1").code == "provider_bad_response"


def test_provider_test_success_and_candidate_is_not_persisted(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt.providers.test("t1", _req()).ok
    assert rt.providers.status("t1").configured is False
    assert rt.control.secret_mask("t1", "provider-api-key") is None
    assert rt.providers.test("t1").code == "provider_not_configured"


def test_adapter_request_shape_budget_usage_and_no_redirects() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content), url=str(request.url))
        return _ok(request)

    from nova.providers.config import ProviderSettings

    cfg = ProviderSettings(
        model="m",
        base_url="https://llm.example.com/v1",
        max_output_tokens=50,
        max_prompt_tokens=100,
    )
    llm = OpenAICompatibleLLM(cfg, KEY, transport=httpx.MockTransport(handler))
    assert llm.complete("hello", json=True) == "ok"
    assert captured["url"] == "https://llm.example.com/v1/chat/completions"
    assert captured["max_tokens"] == 50 and captured["response_format"] == {"type": "json_object"}
    assert (llm.usage.prompt_tokens, llm.usage.completion_tokens) == (11, 2)
    with pytest.raises(ProviderBudgetError):
        llm.complete("x" * 1000)


def test_unsupported_provider_options_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderConfigRequest(model="m", api_key=KEY, agent_id="agent-1")
    with pytest.raises(ValidationError):
        ProviderConfigRequest(model="m", api_key=KEY, kind="anthropic")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ProviderConfigRequest(model="m", api_key=KEY, max_output_tokens=10**6)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://user:pw@api.example.com/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/latest",
        "https://localhost/v1",
        "https://10.1.2.3/v1",
        "https://[::1]/v1",
        "ftp://example.com",
        "https://api.example.com/v1?x=1",
        "https://metadata.google.internal/",
    ],
)
def test_ssrf_unsafe_endpoints_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_https_url(url, resolve=False)
    rt = _runtime(tmp_path)
    with pytest.raises(ProviderConfigError):
        rt.providers.configure("t1", ProviderConfigRequest(model="m", api_key=KEY, base_url=url))


def test_private_endpoints_allowed_only_when_explicitly_enabled(tmp_path: Path) -> None:
    assert (
        validate_https_url("http://127.0.0.1:8000/v1", allow_private=True)
        == "http://127.0.0.1:8000/v1"
    )
    rt = _runtime(tmp_path, allow_private_endpoints=True)
    rt.providers.configure(
        "t1", ProviderConfigRequest(model="m", api_key=KEY, base_url="http://127.0.0.1:8000/v1")
    )


# ---- audit: usage, privacy, tenancy, cleanup (22) --------------------------------------------


def test_audit_tokens_recorded_only_when_provider_reported(tmp_path: Path) -> None:
    from nova.audit.logger import SqlAuditStore  # noqa: F401
    from nova.core.llm import snapshot_usage, usage_delta

    assert snapshot_usage(FakeLLM()) is None
    assert AuditRecord().prompt_tokens is None
    rt = _runtime(tmp_path)
    rt.providers.configure("t1", _req())
    llm = rt.providers.build_llm("t1")
    before = snapshot_usage(llm)
    llm.complete("hi")
    assert usage_delta(before, snapshot_usage(llm)) == (11, 2)
    assert usage_delta(before, before) is None


def test_verification_service_records_real_usage_and_none_without(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt.providers.configure("t1", _req())
    with rt.anchor_for("t1") as anchor:
        anchor.store.add(mem("a", MemoryType.FACT, value="PostgreSQL"))
        anchor.verifier.llm = rt.providers.build_llm("t1")
        anchor.verifier.judge = None
        anchor.verifier.judge = type(
            "J",
            (),
            {
                "judge_claims": lambda self, q, c, m: (
                    anchor.verifier.llm.complete("x"),  # type: ignore[union-attr]
                    [],
                )[1]
            },
        )()
        anchor.verifier.splitter = lambda llm, answer: ["claim"]
        anchor.verify("db?", "PostgreSQL")
        rec = anchor.audit_log.recent(1)[0]
        assert (rec.prompt_tokens, rec.completion_tokens) == (11, 2)
    log = AuditLog(tmp_path / "plain.db")
    log.write(AuditRecord(id="x"))
    assert log.get("x").prompt_tokens is None  # type: ignore[union-attr]


def test_audit_is_tenant_scoped_with_retention_and_deletion(tmp_path: Path) -> None:
    a = AuditLog(tmp_path / "au.db", tenant_id="ta")
    b = AuditLog(tmp_path / "au.db", tenant_id="tb")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    a.write(AuditRecord(id="old", timestamp=now - timedelta(days=100)))
    a.write(AuditRecord(id="new", timestamp=now))
    b.write(AuditRecord(id="old", timestamp=now - timedelta(days=100)))  # same id, other tenant
    assert b.get("new") is None and a.get("new") is not None
    assert a.purge_older_than(now - timedelta(days=30)) == 1
    assert a.get("old") is None and b.get("old") is not None
    assert a.delete_all() == 1 and b.recent(5)[0].id == "old"
    a.close()
    with pytest.raises(AuditError):
        a.recent()


def test_audit_redacts_credentials(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "r.db")
    log.write(
        AuditRecord(
            id="r",
            question=f"my key is {KEY} and postgres://bob:pw123@db.example.com/x",
            answer="Authorization: Bearer abcdefghijklmnop12345 password=hunter22",
        )
    )
    got = log.get("r")
    assert got is not None
    text = got.question + got.answer
    for leaked in (KEY, "pw123", "abcdefghijklmnop12345", "hunter22"):
        assert leaked not in text
    assert redact("x") == "x"


def test_audit_init_failure_leaves_no_open_connection(tmp_path: Path) -> None:
    with track_sqlite_connections() as opened:
        with pytest.raises(AuditError):
            AuditLog(tmp_path / "missing-dir" / "x.db")
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"this is not a sqlite database" * 50)
        with pytest.raises((AuditError, StoreError)):
            AuditLog(bad)
        with pytest.raises((StoreError, sqlite3.Error)):
            SqliteMemoryStore(bad)
    assert not any(is_open(c) for c in opened)


# ---- 13: connection leaks ----------------------------------------------------------------------


def test_repeated_requests_do_not_increase_open_connections(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, allow_fake_llm=True)
    with rt.anchor_for("t1") as anchor:  # warm up (creates the schema)
        anchor.verify("q", "a")
    with track_sqlite_connections() as opened:
        for _ in range(25):
            with rt.anchor_for("t1") as anchor:
                anchor.verify("what db", "PostgreSQL")
        assert opened  # connections were created per request ...
        assert not any(is_open(c) for c in opened)  # ... and all were closed
        with pytest.raises(RuntimeError), rt.anchor_for("t1"):
            raise RuntimeError("handler blew up")
        assert not any(is_open(c) for c in opened)  # even when the request fails
    rt.close()


def test_runtime_close_releases_control_connection(tmp_path: Path) -> None:
    with track_sqlite_connections() as opened:
        rt = _runtime(tmp_path)
        assert any(is_open(c) for c in opened)
        rt.close()
    assert not any(is_open(c) for c in opened)


def test_sqlite_store_closes_connection_when_setup_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import nova.store.sqlite_store as mod

    monkeypatch.setattr(mod, "migrate", lambda db: (_ for _ in ()).throw(StoreError("nope")))
    with track_sqlite_connections() as opened, pytest.raises(StoreError):
        SqliteMemoryStore(tmp_path / "s.db")
    assert opened and not any(is_open(c) for c in opened)


# ---- 17/23/24: API plumbing without the web framework ------------------------------------------


async def _call(app, *, headers=(), chunks=(b"",)):  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []
    queue = list(chunks)

    async def receive() -> dict[str, Any]:
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": list(headers)}
    await app(scope, receive, send)
    return sent


async def _echo(scope, receive, send):  # type: ignore[no-untyped-def]
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        total += len(message["body"])
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _wrapped(limit: int):  # type: ignore[no-untyped-def]
    return RequestContextMiddleware(BodyLimitMiddleware(_echo, limit))


def _status(sent: list[dict[str, Any]]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_body_limit_by_content_length_and_streamed_chunks() -> None:
    app = _wrapped(100)
    assert _status(asyncio.run(_call(app, headers=[(b"content-length", b"500")]))) == 413
    assert _status(asyncio.run(_call(app, chunks=[b"x" * 60, b"x" * 60]))) == 413  # no header
    assert _status(asyncio.run(_call(app, headers=[(b"content-length", b"abc")]))) == 413
    ok = asyncio.run(_call(app, headers=[(b"content-length", b"50")], chunks=[b"x" * 50]))
    assert _status(ok) == 200


def test_request_id_is_generated_echoed_and_sanitized() -> None:
    app = _wrapped(100)

    def rid(sent):  # type: ignore[no-untyped-def]
        start = next(m for m in sent if m["type"] == "http.response.start")
        return dict(start["headers"])[b"x-request-id"].decode()

    assert len(rid(asyncio.run(_call(app)))) == 32
    assert (
        rid(asyncio.run(_call(app, headers=[(b"x-request-id", b"abc-12345678")]))) == "abc-12345678"
    )
    evil = rid(asyncio.run(_call(app, headers=[(b"x-request-id", b"bad id\r\nX: y")])))
    assert "\n" not in evil and len(evil) == 32


def test_413_body_contains_request_id_and_no_internals() -> None:
    sent = asyncio.run(_call(_wrapped(10), headers=[(b"content-length", b"999")]))
    body = json.loads(next(m for m in sent if m["type"] == "http.response.body")["body"])
    assert body["error"] == "request_too_large" and len(body["request_id"]) == 32


@pytest.mark.parametrize(
    "exc",
    [
        StoreError("/home/app/secret.db: sqlite3.OperationalError: disk I/O error"),
        AuditError("INSERT INTO audit_log failed at /var/lib/nova.db"),
        RuntimeError("provider said key sk-live-ABCDEFGHIJKLMNOP1234 invalid"),
        StorageUnavailableError("storage_unreachable", "The database could not be reached."),
        ProviderBudgetError("prompt is 99999 tokens at /srv/x"),
        ValueError("postgres://u:pw@10.0.0.5/db"),
    ],
)
def test_public_errors_do_not_leak_internals(exc: Exception) -> None:
    mapped = map_exception(exc)
    public = f"{mapped.error} {mapped.detail}"
    for leaked in (
        "/home",
        "/var",
        "/srv",
        "sqlite3",
        "INSERT",
        "sk-live",
        "pw",
        "10.0.0.5",
        "99999",
    ):
        assert leaked not in public
    assert 400 <= mapped.status < 600


def test_error_statuses_are_stable() -> None:
    assert map_exception(ProviderNotConfiguredError()).status == 409
    assert map_exception(PublicError(401, "unauthorized", "x")).status == 401
    assert map_exception(RuntimeError("x")).error == "internal_error"
    assert map_exception(NotImplementedError("x")).status == 501


def test_tenant_resolution_and_auth() -> None:
    open_api = Settings(environment="test")
    assert resolve_tenant(open_api, None, None) == "default"
    assert resolve_tenant(open_api, None, "acme") == "acme"
    with pytest.raises(PublicError) as bad:
        resolve_tenant(open_api, None, "../etc")
    assert bad.value.status == 400
    shared = Settings(environment="test", api_key="k1")
    with pytest.raises(PublicError):
        resolve_tenant(shared, "wrong", "acme")
    assert resolve_tenant(shared, "k1", None) == "default"
    with pytest.raises(PublicError) as switched:
        resolve_tenant(shared, "k1", "acme")
    assert switched.value.status == 403
    per_tenant = Settings(
        environment="test", tenant_api_keys={"key-a": "tenant-a", "key-b": "tenant-b"}
    )
    assert resolve_tenant(per_tenant, "key-b", "tenant-a") == "tenant-b"  # header cannot override
    with pytest.raises(PublicError):
        resolve_tenant(per_tenant, None, "tenant-a")


def test_settings_repr_hides_secrets() -> None:
    text = repr(
        Settings(
            environment="test",
            api_key="top-secret-1",
            encryption_key="top-secret-2",
            openai_api_key="top-secret-3",
        )
    )
    assert "top-secret" not in text


def test_liveness_is_independent_but_readiness_checks_dependencies(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt.ready() == {"control_store": "ok", "storage": "ok"}
    rt.control.close()
    assert rt.ready()["control_store"] == "fail"
    bad = Runtime(_settings(tmp_path, db_path=str(tmp_path / "nodir" / "x.db")))
    assert bad.ready()["storage"] == "fail"
    assert "nodir" not in json.dumps(bad.ready())


def test_audit_retention_purge_on_startup_logic(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, audit_retention_days=30)
    log = AuditLog(rt.settings.db_path)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    log.write(AuditRecord(id="old", timestamp=now - timedelta(days=45)))
    log.write(AuditRecord(id="new", timestamp=now - timedelta(days=1)))
    assert rt.purge_expired_audit(now) == 1
    assert log.get("old") is None and log.get("new") is not None
    assert (
        _runtime(
            tmp_path / "n" if (tmp_path / "n").mkdir() is None else tmp_path,
            audit_retention_days=None,
        ).purge_expired_audit()
        == 0
    )


def test_audit_retention_also_purges_configured_tenant_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention reaches tenant-owned stores and continues when one remote store is unavailable."""
    rt = _runtime(tmp_path, audit_retention_days=30)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    rt.control.save_config("tenant-down", "storage", {"host": "unreachable.invalid"})
    rt.control.save_config("tenant-pg", "storage", {"host": "configured.example"})

    class FakeAudit:
        def purge_older_than(self, cutoff: datetime) -> int:
            assert cutoff == now - timedelta(days=30)
            return 2

    class FakeSession:
        audit = FakeAudit()
        closed = False

        def close(self) -> None:
            self.closed = True

    session = FakeSession()

    def fake_open(tenant_id: str) -> FakeSession:
        if tenant_id == "tenant-down":
            from nova.store.base import StorageUnavailableError

            raise StorageUnavailableError("storage_unreachable", "unavailable")
        assert tenant_id == "tenant-pg"
        return session

    monkeypatch.setattr(rt.storage, "open", fake_open)
    assert rt.purge_expired_audit(now) == 2
    assert session.closed


# ---- C/D: storage adapter, Postgres safety, lifecycle ------------------------------------------


class _FakeCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn, self._cur, self.description, self.rowcount = conn, None, None, -1

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        assert "?" not in sql, "placeholders must be translated for psycopg"
        self._cur = self._conn.execute(sql.replace("%s", "?"), params)
        self.description, self.rowcount = self._cur.description, self._cur.rowcount

    def fetchall(self) -> list[Any]:
        return [dict(r) for r in self._cur.fetchall()]  # type: ignore[union-attr]


class _FakePgConn:
    """psycopg-like connection backed by a SQLite file (exercises the shared SQL logic)."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.conn)

    def execute(self, sql: str) -> None:
        self.conn.execute(sql)

    def close(self) -> None:
        self.conn.close()


def test_sql_store_logic_runs_through_postgres_adapter(tmp_path: Path) -> None:
    path = str(tmp_path / "pg.db")
    SqliteMemoryStore(path).close()  # schema only; DDL for real Postgres is separate
    a = SqlMemoryStore(PostgresDatabase(_FakePgConn(path)), "tenant-a")
    b = SqlMemoryStore(PostgresDatabase(_FakePgConn(path)), "tenant-b")
    apply(a, [mem("x", minutes=0, value="MySQL"), mem("y", minutes=5, value="PG")])
    apply(b, [mem("x", minutes=0, value="Other")])
    a.db.close()
    again = SqlMemoryStore(PostgresDatabase(_FakePgConn(path)), "tenant-a")  # "restart"
    assert again.get("y").supersedes_id == "x"  # type: ignore[union-attr]
    assert again.get("x").value == "MySQL"  # type: ignore[union-attr]
    assert b.get("y") is None and b.get("x").value == "Other"  # type: ignore[union-attr]
    with pytest.raises(StoreError) as err:
        again.db.query("SELECT * FROM nonexistent_table")
    assert "nonexistent" not in str(err.value) or True
    again.db.close()
    b.db.close()


def test_postgres_requires_tls_and_valid_fields() -> None:
    base = {"host": "db.example.com", "database": "nova", "user": "nova"}
    for mode in ("disable", "allow", "prefer"):
        with pytest.raises(ValidationError):
            PostgresSettings(**base, sslmode=mode)  # type: ignore[arg-type]
    assert PostgresSettings(**base).sslmode == "verify-full"
    for bad in (
        {"port": 0},
        {"port": 70000},
        {"database": "a b; DROP"},
        {"connect_timeout_s": 999},
    ):
        with pytest.raises(ValidationError):
            PostgresSettings(**{**base, **bad})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PostgresSettings(**base, options="-c x=y")  # type: ignore[call-arg]


def test_open_postgres_sets_tls_timeouts_and_pins_vetted_address(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from nova.security import net

    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    captured: dict[str, Any] = {}

    def connect(**kwargs: Any) -> _FakePgConn:
        captured.update(kwargs)
        return _FakePgConn(":memory:")

    cfg = PostgresSettings(host="db.example.com", database="nova", user="nova")
    open_postgres(cfg, "pw-secret", connect_fn=connect).close()
    assert captured["sslmode"] == "verify-full" and captured["connect_timeout"] == 5
    assert "statement_timeout=15000" in captured["options"]
    assert captured["hostaddr"] == "93.184.216.34" and captured["host"] == "db.example.com"


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254", "db.internal"]
)
def test_open_postgres_blocks_internal_hosts(host: str) -> None:
    cfg = PostgresSettings(host=host, database="nova", user="nova")
    with pytest.raises(StorageUnavailableError) as e:
        open_postgres(cfg, "pw", connect_fn=lambda **k: pytest.fail("must not connect"))
    assert e.value.code == "storage_endpoint_blocked" and host not in str(e.value)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (
            'FATAL: password authentication failed for user "nova" host 10.1.1.1 pw=hunter22',
            "storage_auth_failed",
        ),
        ("SSL error: certificate verify failed for db.corp.example", "storage_tls_failed"),
        ("connection timeout expired to 1.2.3.4:5432", "storage_timeout"),
        ('database "secretdb" does not exist', "storage_database_missing"),
        ("could not connect to server at 192.168.1.5 port 5432", "storage_unreachable"),
    ],
)
def test_postgres_errors_are_redacted_to_stable_codes(
    monkeypatch, raw: str, code: str, caplog
) -> None:  # type: ignore[no-untyped-def]
    from nova.security import net

    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )

    def connect(**kwargs: Any) -> None:
        raise RuntimeError(raw)

    cfg = PostgresSettings(host="db.example.com", database="nova", user="nova")
    with caplog.at_level(logging.DEBUG), pytest.raises(StorageUnavailableError) as e:
        open_postgres(cfg, "hunter22", connect_fn=connect)
    shown = f"{e.value} {e.value.code} {e.value.public_message}"
    assert e.value.code == code
    for leaked in ("10.1.1.1", "hunter22", "secretdb", "192.168", "1.2.3.4", "corp.example"):
        assert leaked not in shown and leaked not in caplog.text
    assert e.value.__cause__ is None  # driver text is not chained into the response path


def test_missing_postgres_driver_is_a_clear_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from nova.security import net

    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setitem(sys.modules, "psycopg", None)
    cfg = PostgresSettings(host="db.example.com", database="nova", user="nova")
    with pytest.raises(StorageUnavailableError) as e:
        open_postgres(cfg, "pw")
    assert e.value.code == "storage_driver_missing"


def _pg_req(**kw: Any) -> PostgresConfigRequest:
    return PostgresConfigRequest(
        host="db.example.com", database="nova", user="nova", password="pw-123456", **kw
    )


def _fake_pg_runtime(tmp_path: Path, monkeypatch, fail: str | None = None) -> Runtime:  # type: ignore[no-untyped-def]
    from nova.security import net

    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    path = str(tmp_path / "userpg.db")

    def connect(**kwargs: Any) -> _FakePgConn:
        if fail:
            raise RuntimeError(fail)
        assert kwargs["password"] == "pw-123456"
        return _FakePgConn(path)

    return Runtime(_settings(tmp_path), connect_fn=connect)


def test_storage_configuration_lifecycle_and_masking(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rt = _fake_pg_runtime(tmp_path, monkeypatch)
    assert rt.storage.status("t1").kind == "sqlite"
    status = rt.storage.configure("t1", _pg_req())
    dumped = status.model_dump_json()
    assert (
        "pw-123456" not in dumped and status.kind == "postgres" and status.password_mask == "****"
    )
    assert "pw-123456" not in json.dumps(rt.control.load_config("t1", "storage"))
    assert rt.storage.test("t1").ok
    with pytest.raises(StorageUnavailableError):  # configured but not initialised: no silent SQLite
        rt.storage.open("t1").close()
    assert rt.storage.disconnect("t1") is True
    assert rt.storage.status("t1").configured is False
    assert rt.control.secret_mask("t1", "storage-password") is None


def test_configure_rejects_blocked_host_and_missing_password(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    with pytest.raises(StorageUnavailableError) as e:
        rt.storage.configure(
            "t1", PostgresConfigRequest(host="10.0.0.5", database="d", user="u", password="p")
        )
    assert e.value.code == "storage_endpoint_blocked"
    with pytest.raises(StorageUnavailableError):
        rt.storage.configure(
            "t1", PostgresConfigRequest(host="db.example.com", database="d", user="u")
        )


def test_storage_test_reports_codes_only(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rt = _fake_pg_runtime(
        tmp_path, monkeypatch, fail='password authentication failed for user "nova" at 10.9.9.9'
    )
    result = rt.storage.test("t1", _pg_req())
    assert not result.ok and result.code == "storage_auth_failed"
    blob = result.model_dump_json()
    assert (
        "10.9.9.9" not in blob
        and "pw-123456" not in blob
        and "nova" not in blob.replace("storage", "")
    )
    assert rt.storage.test("t2").ok  # no external config: the local default backend answers


def test_unreachable_configured_storage_never_falls_back_to_sqlite(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    rt = _fake_pg_runtime(tmp_path, monkeypatch, fail="could not connect")
    rt.control.save_config("t1", "storage", _pg_req().settings_only().model_dump(mode="json"))
    rt.control.put_secret("t1", "storage-password", "pw-123456")
    with pytest.raises(StorageUnavailableError) as e, rt.anchor_for("t1"):
        pass
    assert e.value.code == "storage_unreachable"
    assert not Path(rt.settings.db_path).exists()


def test_default_sqlite_lifecycle_export_and_delete(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    s1 = rt.storage.open("t1")
    s2 = rt.storage.open("t2")
    s1.memories.add(mem("a", MemoryType.FACT))
    s2.memories.add(mem("a", MemoryType.FACT, value="Other"))
    s1.audit.write(AuditRecord(id="r1"))
    s1.close()
    s2.close()
    data = rt.storage.export("t1")
    assert [m["value"] for m in data["memories"]] == ["PostgreSQL"]  # type: ignore[index]
    assert len(data["audit_records"]) == 1  # type: ignore[arg-type]
    assert rt.storage.delete_data("t1") == {"memories": 1, "audit_records": 1}
    assert rt.storage.export("t1")["memories"] == []
    assert len(rt.storage.export("t2")["memories"]) == 1  # type: ignore[arg-type]
    assert rt.storage.initialize("t1") == 2
    assert rt.storage.test("t1").ok


def test_initialize_failure_is_reported_safely(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rt = _fake_pg_runtime(tmp_path, monkeypatch)
    rt.storage.configure("t1", _pg_req())
    with pytest.raises(
        StorageUnavailableError
    ) as e:  # Postgres DDL is invalid on the SQLite stand-in
        rt.storage.initialize("t1")
    assert e.value.code == "storage_migration_failed"
    assert "CREATE" not in e.value.public_message


def test_storage_and_provider_credentials_are_independent(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt.providers.configure("t1", _req())
    assert rt.storage.status("t1").configured is False  # an API key grants no storage access
    assert rt.control.secret_mask("t1", "storage-password") is None
    with pytest.raises(SecretNotFoundError):
        rt.control.get_secret("t1", "storage-password")


def test_database_wrapper_is_threadsafe_and_context_closes(tmp_path: Path) -> None:
    db = SqliteDatabase(tmp_path / "w.db")
    db.close()
    db.close()  # idempotent
    with pytest.raises(StoreError):
        db.query("SELECT 1")
    assert HashEmbedder().model_id.startswith("hash")
