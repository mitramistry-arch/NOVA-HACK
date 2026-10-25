"""Unit tests for core schemas, protocols, LLM doubles, and configuration."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nova.core.config import Settings, get_settings
from nova.core.llm import FakeLLM, LLMClient
from nova.core.protocols import Extractor, MemoryStore, Retriever, Verifier
from nova.core.schemas import (
    Decision,
    DecisionAction,
    IngestReport,
    Memory,
    MemoryStatus,
    MemoryType,
    Message,
    Verdict,
    VerdictLabel,
)


def test_memory_defaults_and_validation() -> None:
    """Verify Memory creation with defaults and valid enum values."""
    now = datetime.now(UTC)
    mem = Memory(
        id="mem-1",
        type=MemoryType.FACT,
        subject="database",
        value="PostgreSQL",
        source_msg_id="msg-1",
        source_quote="we use postgres",
        session_id="sess-1",
        timestamp=now,
    )
    assert mem.id == "mem-1"
    assert mem.type == "fact"
    assert mem.status == MemoryStatus.CURRENT
    assert mem.confidence == 1.0
    assert mem.reason is None
    assert mem.supersedes_id is None


def test_memory_optional_fields_and_invalid_type() -> None:
    """Verify optional fields in Memory and validation error on invalid type."""
    now = datetime.now(UTC)
    mem = Memory(
        id="mem-2",
        type="decision",
        subject="framework",
        value="FastAPI",
        reason="Speed and type safety",
        source_msg_id="msg-2",
        source_quote="let's use FastAPI",
        session_id="sess-1",
        timestamp=now,
        status="superseded",
        supersedes_id="mem-1",
        confidence=0.9,
    )
    assert mem.reason == "Speed and type safety"
    assert mem.status == "superseded"
    assert mem.supersedes_id == "mem-1"
    assert mem.confidence == 0.9

    with pytest.raises(ValidationError):
        Memory(
            id="mem-invalid",
            type="invalid_type",
            subject="x",
            value="y",
            source_msg_id="m",
            source_quote="q",
            session_id="s",
            timestamp=now,
        )


def test_message_schema() -> None:
    """Verify Message model fields and serialization."""
    now = datetime.now(UTC)
    msg = Message(
        id="msg-1",
        session_id="sess-1",
        role="user",
        text="Hello world",
        timestamp=now,
    )
    assert msg.id == "msg-1"
    assert msg.role == "user"
    assert msg.text == "Hello world"


def test_verdict_schema() -> None:
    """Verify Verdict creation and label validation."""
    verdict = Verdict(
        claim="Database is Postgres",
        label=VerdictLabel.SUPPORTED,
        memory_ids=["mem-1"],
        rationale="Exact match with stored fact",
    )
    assert verdict.label == "supported"
    assert verdict.memory_ids == ["mem-1"]

    with pytest.raises(ValidationError):
        Verdict(claim="Claim", label="unknown_label")


def test_decision_schema() -> None:
    """Verify Decision creation and action types."""
    decision = Decision(
        action=DecisionAction.ANSWER,
        verdicts=[
            Verdict(
                claim="Claim A",
                label=VerdictLabel.SUPPORTED,
                memory_ids=["m1"],
            )
        ],
        final_answer="Confirmed answer",
    )
    assert decision.action == "answer"
    assert len(decision.verdicts) == 1
    assert decision.final_answer == "Confirmed answer"


def test_ingest_report_schema() -> None:
    """Verify IngestReport defaults and custom counts."""
    empty_report = IngestReport()
    assert empty_report.added == 0
    assert empty_report.superseded == 0
    assert empty_report.dropped == 0

    custom_report = IngestReport(added=5, superseded=2, dropped=1)
    assert custom_report.added == 5
    assert custom_report.superseded == 2
    assert custom_report.dropped == 1


def test_protocols_runtime_checkable() -> None:
    """Verify protocol classes are runtime checkable."""

    class DummyExtractor:
        def extract(self, messages: list[Message]) -> list[Memory]:
            return []

    class DummyStore:
        def add(self, memory: Memory) -> None:
            pass

        def add_many(self, memories: list[Memory]) -> None:
            pass

        def get(self, memory_id: str) -> Memory | None:
            return None

        def current_by_subject(self, subject: str) -> Memory | None:
            return None

        def current_all_by_subject(self, subject: str) -> list[Memory]:
            return []

        def chain_by_subject(self, subject: str) -> list[Memory]:
            return []

        def list_by_session(self, session_id: str) -> list[Memory]:
            return []

        def supersede(self, old_id: str, new_id: str) -> None:
            pass

        def link_chain(self, ordered_ids: list[str]) -> int:
            return 0

        def transaction(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    class DummyRetriever:
        def retrieve(
            self,
            query: str,
            k: int = 5,
            include_history: bool = False,
            session_id: str | None = None,
        ) -> list[Memory]:
            return []

    class DummyVerifier:
        def verify(self, question: str, answer: str, memories: list[Memory]) -> list[Verdict]:
            return []

    assert isinstance(DummyExtractor(), Extractor)
    assert isinstance(DummyStore(), MemoryStore)
    assert isinstance(DummyRetriever(), Retriever)
    assert isinstance(DummyVerifier(), Verifier)


def test_fake_llm_behavior() -> None:
    """Verify FakeLLM response handling and protocol conformance."""
    fake = FakeLLM(responses=["response-1", "response-2"], default_response="fallback")
    assert isinstance(fake, LLMClient)

    res1 = fake.complete("Prompt 1")
    res2 = fake.complete("Prompt 2", json=True)
    res3 = fake.complete("Prompt 3")

    assert res1 == "response-1"
    assert res2 == "response-2"
    assert res3 == "fallback"
    assert len(fake.calls) == 3
    assert fake.calls[1] == {"prompt": "Prompt 2", "json": True}


def test_fake_llm_dict_mapping() -> None:
    """Verify FakeLLM prompt-to-response mapping mode."""
    mapping = {"What is DB?": "Postgres", "Port?": "5432"}
    fake = FakeLLM(responses=mapping, default_response="Unknown")

    assert fake.complete("What is DB?") == "Postgres"
    assert fake.complete("Port?") == "5432"
    assert fake.complete("Other?") == "Unknown"


def test_config_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify configuration settings load from environment variables."""
    monkeypatch.setenv("NOVA_API_KEY", "secret-test-key")
    monkeypatch.setenv("NOVA_DB_PATH", "test.db")
    monkeypatch.setenv("NOVA_LLM_PROVIDER", "anthropic")

    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.api_key == "secret-test-key"
    assert settings.db_path == "test.db"
    assert settings.llm_provider == "anthropic"
