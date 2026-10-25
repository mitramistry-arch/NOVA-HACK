"""HTTP-level phase 2 tests (require FastAPI; skipped where it is not installed)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from nova.api.main import app, close_runtime
from nova.security.secrets import generate_key

KEY = "sk-live-ABCDEFGHIJKLMNOP1234"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NOVA_DB_PATH", str(tmp_path / "data.db"))
    monkeypatch.setenv("NOVA_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("NOVA_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("NOVA_ALLOW_PRIVATE_ENDPOINTS", "1")
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_ALLOW_FAKE_LLM", raising=False)
    close_runtime()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    close_runtime()


def _msg(i: int = 1, text: str = "We use PostgreSQL.") -> dict:
    return {
        "id": f"m{i}",
        "session_id": "s",
        "role": "user",
        "text": text,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def test_oversized_body_is_rejected_with_413(client: TestClient) -> None:
    res = client.post(
        "/ingest", content=b"x" * 2_000_000, headers={"content-type": "application/json"}
    )
    assert res.status_code == 413 and res.json()["error"] == "request_too_large"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ingest", {"session_id": "s", "messages": [_msg(i) for i in range(201)]}),
        ("/ingest", {"session_id": "s", "messages": []}),
        ("/ingest", {"session_id": " ", "messages": [_msg()]}),
        ("/ingest", {"session_id": "s", "messages": [{**_msg(), "role": "wizard"}]}),
        (
            "/ingest",
            {"session_id": "s", "messages": [{**_msg(), "timestamp": "2026-01-01T00:00:00"}]},
        ),
        ("/ingest", {"session_id": "s", "messages": [_msg(text="x" * 20_001)]}),
        ("/ask", {"question": "q" * 2_001}),
        ("/ask", {"question": "ok", "k": 0}),
        ("/ask", {"question": "ok", "k": -3}),
        ("/ask", {"question": "ok", "k": 10_000}),
        ("/ask", {"question": "ok", "draft_answer": "a" * 10_001}),
        ("/verify", {"question": "ok", "answer": ""}),
        ("/verify", {"question": "ok", "answer": "a" * 10_001}),
    ],
)
def test_invalid_or_oversized_inputs_return_422(client: TestClient, path: str, body: dict) -> None:
    res = client.post(path, json=body)
    assert res.status_code == 422 and res.json()["error"] == "validation_error"
    assert "input" not in res.text.lower() or "x" * 50 not in res.text  # inputs are not echoed


def test_missing_provider_is_a_typed_409_never_fake(client: TestClient) -> None:
    res = client.post("/ingest", json={"session_id": "s", "messages": [_msg()]})
    assert res.status_code == 409 and res.json()["error"] == "provider_not_configured"
    assert res.headers["x-request-id"] == res.json()["request_id"]


def test_error_bodies_do_not_leak(client: TestClient, tmp_path: Path) -> None:
    res = client.post("/ingest", json={"session_id": "s", "messages": [_msg()]})
    for leaked in (str(tmp_path), "Traceback", "sqlite", ".db"):
        assert leaked not in res.text


def test_provider_lifecycle_never_returns_key(client: TestClient) -> None:
    body = {"model": "m", "api_key": KEY, "base_url": "http://127.0.0.1:9/v1"}
    put = client.put("/provider", json=body)
    assert (
        put.status_code == 200 and KEY not in put.text and put.json()["api_key_mask"] == "****1234"
    )
    assert KEY not in client.get("/provider").text
    tested = client.post("/provider/test")
    assert tested.status_code == 200 and KEY not in tested.text and not tested.json()["ok"]
    assert client.get("/provider", headers={"X-Tenant-ID": "other"}).json()["configured"] is False
    assert client.delete("/provider").status_code == 204
    assert client.get("/provider").json()["configured"] is False


def test_storage_endpoints_and_delete_confirmation(client: TestClient) -> None:
    assert client.get("/storage").json() == {
        "kind": "sqlite",
        "configured": False,
        "host": None,
        "port": None,
        "database": None,
        "user": None,
        "sslmode": None,
        "password_mask": None,
    }
    bad = client.put(
        "/storage",
        json={
            "host": "db.example.com",
            "database": "d",
            "user": "u",
            "password": "p",
            "sslmode": "disable",
        },
    )
    assert bad.status_code == 422
    assert client.delete("/data").status_code == 400
    assert client.delete("/data?confirm=delete-all-data").status_code == 200
    assert "memories" in client.get("/data/export").json()


def test_liveness_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"


def test_repeated_requests_do_not_leak_connections(client: TestClient) -> None:
    from tests.helpers import is_open, track_sqlite_connections

    with track_sqlite_connections() as opened:
        for _ in range(15):
            client.get("/audit/nope")
        assert not any(is_open(c) for c in opened)
    assert json.loads(client.get("/audit/nope").text)["error"] == "http_error"
