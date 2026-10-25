"""Unit tests for memory extraction and quote validation."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from nova.core.llm import FakeLLM
from nova.core.protocols import Extractor
from nova.core.schemas import Memory, MemoryType, Message
from nova.extraction.extractor import ExtractionError, LLMExtractor
from nova.extraction.quote_validator import generate_memory_id, validate_quotes


def _make_msg(msg_id: str, text: str, session_id: str = "sess-1") -> Message:
    """Helper to create test messages."""
    return Message(
        id=msg_id,
        session_id=session_id,
        role="user",
        text=text,
        timestamp=datetime.now(UTC),
    )


def test_extractor_protocol_conformance() -> None:
    """Verify LLMExtractor satisfies the Extractor protocol."""
    llm = FakeLLM(default_response="[]")
    extractor = LLMExtractor(llm)
    assert isinstance(extractor, Extractor)


def test_valid_extraction() -> None:
    """Verify successful extraction with verbatim quote and deterministic id."""
    msg = _make_msg("msg-1", "We agreed to use PostgreSQL for our database.")
    llm_payload = [
        {
            "type": "decision",
            "subject": "project_database",
            "value": "PostgreSQL",
            "reason": "Team consensus",
            "source_msg_id": "msg-1",
            "source_quote": "use PostgreSQL for our database",
            "confidence": 0.95,
        }
    ]
    llm = FakeLLM(responses=json.dumps(llm_payload))
    extractor = LLMExtractor(llm)

    memories = extractor.extract([msg])
    assert len(memories) == 1
    mem = memories[0]
    expected_id = generate_memory_id("sess-1", "msg-1", "project_database", "PostgreSQL")
    assert mem.id == expected_id
    assert mem.type == MemoryType.DECISION
    assert mem.subject == "project_database"
    assert mem.value == "PostgreSQL"
    assert mem.source_quote == "use PostgreSQL for our database"
    assert mem.confidence == 0.95
    assert mem.session_id == "sess-1"


def test_fabricated_quote_dropped() -> None:
    """Verify memories with fabricated or mismatching quotes are dropped."""
    msg = _make_msg("msg-1", "We will deploy on AWS ECS.")
    mem_valid = Memory(
        id="mem-1",
        type=MemoryType.FACT,
        subject="deploy_target",
        value="AWS ECS",
        source_msg_id="msg-1",
        source_quote="deploy on AWS ECS",
        session_id="sess-1",
        timestamp=msg.timestamp,
    )
    mem_fabricated = Memory(
        id="mem-2",
        type=MemoryType.FACT,
        subject="deploy_target",
        value="Kubernetes",
        source_msg_id="msg-1",
        source_quote="deploy on GCP Kubernetes",
        session_id="sess-1",
        timestamp=msg.timestamp,
    )

    kept, dropped = validate_quotes([mem_valid, mem_fabricated], [msg])
    assert len(kept) == 1
    assert kept[0].value == "AWS ECS"
    assert dropped == 1




def test_source_message_id_ambiguous_when_metadata_differs() -> None:
    """A duplicate source ID with a different role or timestamp must not prove a memory."""
    msg = _make_msg("msg-shared", "We selected PostgreSQL for production.")
    conflicting = msg.model_copy(update={"timestamp": msg.timestamp + timedelta(seconds=10)})
    mem = Memory(
        id="mem-shared",
        type=MemoryType.DECISION,
        subject="database",
        value="PostgreSQL",
        source_msg_id=msg.id,
        source_quote="selected PostgreSQL for production",
        session_id=msg.session_id,
        timestamp=msg.timestamp,
    )

    kept, dropped = validate_quotes([mem], [msg, conflicting])

    assert kept == []
    assert dropped == 1


def test_quote_whitespace_and_case_normalization() -> None:
    """Verify quote matching ignores case and whitespace discrepancies."""
    msg = _make_msg("msg-1", "The   timeout  is\n10 SECONDS for requests.")
    mem = Memory(
        id="mem-1",
        type=MemoryType.FACT,
        subject="request_timeout",
        value="10 seconds",
        source_msg_id="msg-1",
        source_quote="timeout is 10 seconds",
        session_id="sess-1",
        timestamp=msg.timestamp,
    )

    kept, dropped = validate_quotes([mem], [msg])
    assert len(kept) == 1
    assert dropped == 0


def test_bad_json_retry_success() -> None:
    """Verify extractor recovers after a single repair retry on bad JSON."""
    msg = _make_msg("msg-1", "Redis is selected for caching.")
    bad_json = "NOT_JSON_AT_ALL"
    valid_payload = [
        {
            "type": "fact",
            "subject": "cache_system",
            "value": "Redis",
            "source_msg_id": "msg-1",
            "source_quote": "Redis is selected for caching",
            "confidence": 1.0,
        }
    ]
    llm = FakeLLM(responses=[bad_json, json.dumps(valid_payload)])
    extractor = LLMExtractor(llm)

    memories = extractor.extract([msg])
    assert len(memories) == 1
    assert memories[0].subject == "cache_system"
    assert len(llm.calls) == 2


def test_bad_json_retry_failure_raises_extraction_error() -> None:
    """Verify extractor raises ExtractionError when repair retry also fails."""
    msg = _make_msg("msg-1", "Some text")
    llm = FakeLLM(responses=["bad-json-1", "bad-json-2"])
    extractor = LLMExtractor(llm)

    with pytest.raises(ExtractionError):
        extractor.extract([msg])


def test_duplicate_ids_deduplicated() -> None:
    """Verify duplicate memory ids within extracted results are deduplicated."""
    msg = _make_msg("msg-1", "We use python for backend.")
    llm_payload = [
        {
            "type": "fact",
            "subject": "backend_lang",
            "value": "python",
            "source_msg_id": "msg-1",
            "source_quote": "python for backend",
        },
        {
            "type": "fact",
            "subject": "backend_lang",
            "value": "python",
            "source_msg_id": "msg-1",
            "source_quote": "python for backend",
        },
    ]
    llm = FakeLLM(responses=json.dumps(llm_payload))
    extractor = LLMExtractor(llm)

    memories = extractor.extract([msg])
    assert len(memories) == 1


def test_windowing_batches_messages() -> None:
    """Verify messages exceeding 20 are chunked into separate windows."""
    messages = [_make_msg(f"msg-{i}", f"Message text number {i}") for i in range(25)]
    llm = FakeLLM(default_response="[]")
    extractor = LLMExtractor(llm)

    memories = extractor.extract(messages)
    assert memories == []
    assert len(llm.calls) == 2
