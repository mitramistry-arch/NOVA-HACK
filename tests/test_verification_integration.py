"""End-to-end integration tests for verification pipeline across evidence conditions."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nova.audit.logger import AuditLog
from nova.core.llm import FakeLLM
from nova.core.schemas import DecisionAction, MemoryStatus, Message
from nova.extraction.extractor import LLMExtractor
from nova.pipeline import Anchor
from nova.retrieval.embedder import HashEmbedder
from nova.retrieval.hybrid import HybridRetriever
from nova.store.sqlite_store import SqliteMemoryStore


def _setup_conversation(tmp_path: Path) -> tuple[Anchor, AuditLog, FakeLLM]:
    """Helper ingesting a changed-decision conversation and returning configured Anchor."""
    store = SqliteMemoryStore(tmp_path / "store.db")
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)
    audit_log = AuditLog(tmp_path / "audit.db")

    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=10)

    msg1 = Message(
        id="m1",
        session_id="sess-1",
        role="user",
        text="We decided to use MySQL for data storage.",
        timestamp=t1,
    )
    msg2 = Message(
        id="m2",
        session_id="sess-1",
        role="user",
        text="Correction: actually use PostgreSQL for our database.",
        timestamp=t2,
    )

    ingest_payload = [
        {
            "type": "decision",
            "subject": "database",
            "value": "MySQL",
            "reason": "initial",
            "source_msg_id": "m1",
            "source_quote": "use MySQL for data storage",
            "confidence": 1.0,
        },
        {
            "type": "correction",
            "subject": "database",
            "value": "PostgreSQL",
            "reason": "correction",
            "source_msg_id": "m2",
            "source_quote": "actually use PostgreSQL for our database",
            "confidence": 1.0,
        },
    ]

    llm = FakeLLM(responses=[json.dumps(ingest_payload)])
    extractor = LLMExtractor(llm)
    anchor = Anchor(store=store, extractor=extractor, retriever=retriever, audit_log=audit_log)

    report = anchor.ingest("sess-1", [msg1, msg2])
    assert report.added == 2
    assert report.superseded == 1
    return anchor, audit_log, llm


def test_condition_supported_answer(tmp_path: Path) -> None:
    """Verify supported claim resolves to ANSWER action with source quotes."""
    anchor, _audit_log, llm = _setup_conversation(tmp_path)
    cur_mem = anchor.retriever.retrieve("database", include_history=False)[0]

    judge_output = [
        {
            "claim_idx": 0,
            "label": "supported",
            "memory_ids": [cur_mem.id],
            "rationale": "PostgreSQL is current database",
        }
    ]
    llm._responses_list.append(json.dumps(judge_output))

    decision = anchor.verify("What database do we use?", "We use PostgreSQL.")
    assert decision.action == DecisionAction.ANSWER
    assert "actually use PostgreSQL for our database" in decision.final_answer
    assert len(decision.verdicts) == 1
    assert decision.verdicts[0].label == "supported"


def test_condition_outdated_correction(tmp_path: Path) -> None:
    """Verify outdated claim resolves to CORRECT action noting newer value."""
    anchor, _audit_log, llm = _setup_conversation(tmp_path)
    all_mems = anchor.retriever.retrieve("database", include_history=True)
    old_mem = next(m for m in all_mems if m.status == MemoryStatus.SUPERSEDED)

    judge_output = [
        {
            "claim_idx": 0,
            "label": "outdated",
            "memory_ids": [old_mem.id],
            "rationale": "MySQL was replaced",
        }
    ]
    llm._responses_list.append(json.dumps(judge_output))

    decision = anchor.verify("What database do we use?", "We use MySQL.")
    assert decision.action == DecisionAction.CORRECT
    assert "Current: PostgreSQL" in decision.final_answer
    assert "replaced MySQL" in decision.final_answer


def test_condition_conflict_flagged(tmp_path: Path) -> None:
    """Verify contradicted claim resolves to FLAG_CONFLICT action."""
    anchor, _audit_log, llm = _setup_conversation(tmp_path)
    cur_mem = anchor.retriever.retrieve("database", include_history=False)[0]

    judge_output = [
        {
            "claim_idx": 0,
            "label": "contradicted",
            "memory_ids": [cur_mem.id],
            "rationale": "Current db is PostgreSQL, not Oracle",
        }
    ]
    llm._responses_list.append(json.dumps(judge_output))

    decision = anchor.verify("What database do we use?", "We use Oracle database.")
    assert decision.action == DecisionAction.FLAG_CONFLICT
    assert "Conflict between sources" in decision.final_answer


def test_condition_unsupported_abstain(tmp_path: Path) -> None:
    """Verify unsupported claim resolves to ABSTAIN action with standard text."""
    anchor, _audit_log, llm = _setup_conversation(tmp_path)

    judge_output = [
        {
            "claim_idx": 0,
            "label": "unsupported",
            "memory_ids": [],
            "rationale": "No evidence for cloud provider",
        }
    ]
    llm._responses_list.append(json.dumps(judge_output))

    decision = anchor.verify("Where is it hosted?", "We deploy on AWS ECS.")
    assert decision.action == DecisionAction.ABSTAIN
    assert decision.final_answer == "Insufficient evidence in memory to answer."


def test_ask_generates_and_verifies_answer(tmp_path: Path) -> None:
    """Verify ask without draft answers from retrieved memories and verifies."""
    anchor, audit_log, llm = _setup_conversation(tmp_path)
    cur_mem = anchor.retriever.retrieve("database", include_history=False)[0]

    draft_answer = "We use PostgreSQL."
    judge_output = [
        {
            "claim_idx": 0,
            "label": "supported",
            "memory_ids": [cur_mem.id],
            "rationale": "verified against current memory",
        }
    ]
    llm._responses_list.extend([draft_answer, json.dumps(judge_output)])

    decision = anchor.ask("What database do we use?")
    assert decision.action == DecisionAction.ANSWER
    assert "PostgreSQL" in decision.final_answer

    recent_audits = audit_log.recent(n=5)
    assert len(recent_audits) >= 1
    assert recent_audits[0].decision_action == DecisionAction.ANSWER
