"""Integration tests for FastAPI gateway endpoints."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nova.api.main import app, get_anchor
from nova.audit.logger import AuditLog, AuditRecord
from nova.core.llm import FakeLLM
from nova.core.schemas import Decision, DecisionAction
from nova.extraction.extractor import ExtractionError, LLMExtractor
from nova.pipeline import Anchor
from nova.retrieval.embedder import HashEmbedder
from nova.retrieval.hybrid import HybridRetriever
from nova.store.sqlite_store import SqliteMemoryStore, StoreError


def _setup_test_anchor(
    tmp_path: Path, llm_responses: list[str] | str = "[]"
) -> tuple[Anchor, AuditLog]:
    """Helper creating an Anchor backed by SQLite and FakeLLM."""
    db_file = tmp_path / "api_test.db"
    store = SqliteMemoryStore(db_file)
    llm = FakeLLM(responses=llm_responses)
    extractor = LLMExtractor(llm)
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)
    audit_log = AuditLog(db_file)
    anchor = Anchor(store=store, extractor=extractor, retriever=retriever, audit_log=audit_log)
    return anchor, audit_log


def test_healthz_endpoint() -> None:
    """Verify health check endpoint returns 200 and ok status."""
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_endpoint(tmp_path: Path) -> None:
    """Verify POST /ingest processes conversation messages into memories."""
    llm_payload = [
        {
            "type": "decision",
            "subject": "project_db",
            "value": "PostgreSQL",
            "reason": "ACID compliance",
            "source_msg_id": "m1",
            "source_quote": "use PostgreSQL for database",
            "confidence": 1.0,
        }
    ]
    anchor, _ = _setup_test_anchor(tmp_path, json.dumps(llm_payload))
    app.dependency_overrides[get_anchor] = lambda: anchor

    client = TestClient(app)
    payload = {
        "session_id": "sess-api",
        "messages": [
            {
                "id": "m1",
                "session_id": "sess-api",
                "role": "user",
                "text": "We will use PostgreSQL for database storage.",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ],
    }

    res = client.post("/ingest", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["added"] == 1
    assert data["superseded"] == 0
    assert data["dropped"] == 0

    re_res = client.post("/ingest", json=payload)
    assert re_res.status_code == 200
    assert re_res.json()["added"] == 0
    app.dependency_overrides.clear()


def test_ask_and_verify_endpoints(tmp_path: Path) -> None:
    """Verify POST /ask and /verify delegate to Anchor."""
    anchor, _ = _setup_test_anchor(tmp_path)

    expected_decision = Decision(action=DecisionAction.ANSWER, final_answer="PostgreSQL")
    anchor.ask = lambda question, draft_answer=None: expected_decision  # type: ignore[assignment]
    anchor.verify = lambda question, answer: expected_decision  # type: ignore[assignment]
    app.dependency_overrides[get_anchor] = lambda: anchor

    client = TestClient(app)

    ask_res = client.post("/ask", json={"question": "what db?"})
    assert ask_res.status_code == 200
    assert ask_res.json()["action"] == "answer"
    assert ask_res.json()["final_answer"] == "PostgreSQL"

    verify_res = client.post("/verify", json={"question": "what db?", "answer": "PostgreSQL"})
    assert verify_res.status_code == 200
    assert verify_res.json()["action"] == "answer"
    app.dependency_overrides.clear()


def test_ask_not_implemented_maps_to_501(tmp_path: Path) -> None:
    """Verify stubbed or unimplemented ask raises NotImplementedError and returns clean 501."""
    anchor, _ = _setup_test_anchor(tmp_path)
    anchor.ask = lambda *a, **k: (_ for _ in ()).throw(
        NotImplementedError("Feature not implemented")
    )  # type: ignore[assignment]
    app.dependency_overrides[get_anchor] = lambda: anchor

    client = TestClient(app)
    res = client.post("/ask", json={"question": "what db?"})
    assert res.status_code == 501
    assert res.json()["error"] == "NotImplementedError"
    assert "Traceback" not in res.text
    app.dependency_overrides.clear()


def test_audit_endpoint(tmp_path: Path) -> None:
    """Verify GET /audit/{id} returns saved audit record or 404."""
    anchor, audit_log = _setup_test_anchor(tmp_path)
    app.dependency_overrides[get_anchor] = lambda: anchor

    record = AuditRecord(
        question="Which db?",
        answer="PostgreSQL",
        decision_action=DecisionAction.ANSWER,
    )
    audit_id = audit_log.write(record)

    client = TestClient(app)
    res = client.get(f"/audit/{audit_id}")
    assert res.status_code == 200
    assert res.json()["id"] == audit_id
    assert res.json()["question"] == "Which db?"

    res_missing = client.get("/audit/missing-id")
    assert res_missing.status_code == 404
    app.dependency_overrides.clear()


def test_api_key_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify optional X-API-Key authentication protects endpoints."""
    monkeypatch.setenv("NOVA_API_KEY", "correct-key")
    anchor, _ = _setup_test_anchor(tmp_path)
    app.dependency_overrides[get_anchor] = lambda: anchor

    client = TestClient(app)

    res_unauth = client.get("/audit/any-id")
    assert res_unauth.status_code == 401

    res_wrong = client.get("/audit/any-id", headers={"X-API-Key": "wrong-key"})
    assert res_wrong.status_code == 401

    res_health = client.get("/healthz")
    assert res_health.status_code == 200

    res_auth = client.get("/audit/nonexistent", headers={"X-API-Key": "correct-key"})
    assert res_auth.status_code == 404
    app.dependency_overrides.clear()


def test_typed_exception_handlers(tmp_path: Path) -> None:
    """Verify typed exceptions return structured error JSON without stack traces."""
    anchor, _ = _setup_test_anchor(tmp_path)

    def failing_extract(*args: Any, **kwargs: Any) -> Any:
        raise ExtractionError("Invalid extraction JSON format")

    def failing_store(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("Database locked error")

    anchor.extractor.extract = failing_extract  # type: ignore[assignment]
    app.dependency_overrides[get_anchor] = lambda: anchor

    client = TestClient(app)
    msg_payload = {
        "session_id": "s1",
        "messages": [
            {
                "id": "m1",
                "session_id": "s1",
                "role": "user",
                "text": "hello",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ],
    }

    res_extract = client.post("/ingest", json=msg_payload)
    assert res_extract.status_code == 422
    assert res_extract.json()["error"] == "ExtractionError"
    assert "Invalid extraction JSON format" not in res_extract.text  # raw detail stays server-side
    assert res_extract.json()["request_id"]
    assert "Traceback" not in res_extract.text

    anchor.extractor.extract = lambda msgs: []  # type: ignore[assignment]
    anchor.store.add_many = failing_store  # type: ignore[assignment]
    anchor.ingest = lambda s, m: (_ for _ in ()).throw(StoreError("Store failed"))  # type: ignore[assignment]

    res_store = client.post("/ingest", json=msg_payload)
    assert res_store.status_code == 500
    assert res_store.json()["error"] == "StoreError"
    assert "Store failed" not in res_store.text
    assert "Traceback" not in res_store.text
    app.dependency_overrides.clear()
