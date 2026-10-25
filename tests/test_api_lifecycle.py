"""Lifecycle and fail-closed startup tests for the tenant-aware API runtime."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nova.api.main as api_main
from nova.api.main import app, close_runtime, get_anchor
from nova.core.config import ConfigError
from nova.store.database import SqliteDatabase


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set isolated database paths for a single API lifecycle test."""
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_DB_PATH", str(tmp_path / "data.db"))
    monkeypatch.setenv("NOVA_CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_TENANT_API_KEYS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NOVA_LLM_PROVIDER", raising=False)
    close_runtime()
    return tmp_path / "data.db"


def test_request_dependency_closes_tenant_session(db_env: Path) -> None:
    """The request-scoped anchor dependency releases its shared database session."""
    dependency = get_anchor("tenant-a")
    anchor = next(dependency)
    db = anchor.store.db
    assert not db.closed
    assert db.query("SELECT 1 AS ok")[0]["ok"] == 1
    dependency.close()
    assert db.closed


def test_request_dependency_closes_session_when_endpoint_fails(db_env: Path) -> None:
    """The request-scoped dependency runs its finally block when an exception is thrown."""
    dependency = get_anchor("tenant-a")
    anchor = next(dependency)
    db = anchor.store.db
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("simulated endpoint failure"))
    assert db.closed


def test_production_startup_fails_closed_without_authentication(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No production server starts or opens a database without configured authentication."""
    monkeypatch.setenv("NOVA_ENV", "production")
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.delenv("NOVA_TENANT_API_KEYS", raising=False)
    with pytest.raises(ConfigError, match="API authentication"), TestClient(app):
        pytest.fail("production startup unexpectedly succeeded")
    assert not db_env.exists()


def test_development_startup_allows_no_api_key(db_env: Path) -> None:
    """Unauthenticated operation is available only when development/test mode is explicit."""
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.get("/readyz")
        assert response.status_code == 200


def test_shared_api_key_cannot_select_other_tenant(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment-wide key is not an authority to impersonate arbitrary tenants."""
    key = "shared-test-api-key"
    monkeypatch.setenv("NOVA_API_KEY", key)
    from nova.api.errors import PublicError
    from nova.api.runtime import resolve_tenant
    from nova.core.config import Settings

    settings = Settings(environment="test", api_key=key)
    assert resolve_tenant(settings, key, None) == "default"
    with pytest.raises(PublicError) as exc:
        resolve_tenant(settings, key, "tenant-a")
    assert exc.value.status == 403


def test_tenant_specific_key_owns_exactly_one_tenant() -> None:
    """Tenant API keys define ownership; a spoofed tenant header cannot override the mapping."""
    from nova.api.errors import PublicError
    from nova.api.runtime import resolve_tenant
    from nova.core.config import Settings

    settings = Settings(
        environment="test", tenant_api_keys={"tenant-a-key": "tenant-a", "tenant-b-key": "tenant-b"}
    )
    assert resolve_tenant(settings, "tenant-b-key", "tenant-a") == "tenant-b"
    with pytest.raises(PublicError):
        resolve_tenant(settings, None, "tenant-a")


def test_runtime_closes_control_database_on_shutdown(db_env: Path) -> None:
    """Normal app shutdown releases the shared control database connection."""

    async def enter_and_exit() -> None:
        async with app.router.lifespan_context(app):
            runtime = api_main.get_runtime()
            control_db = runtime.control.db
            assert isinstance(control_db, SqliteDatabase)
            assert not control_db.closed
        assert control_db.closed

    asyncio.run(enter_and_exit())
