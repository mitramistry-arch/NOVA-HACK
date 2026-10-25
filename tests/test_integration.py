"""End-to-end integration tests for Anchor pipeline ingestion and retrieval."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nova.core.llm import FakeLLM
from nova.core.schemas import MemoryStatus, Message
from nova.extraction.extractor import LLMExtractor
from nova.pipeline import Anchor
from nova.retrieval.embedder import HashEmbedder
from nova.retrieval.hybrid import HybridRetriever
from nova.store.sqlite_store import SqliteMemoryStore


def test_anchor_ingest_and_retrieval_integration(tmp_path: Path) -> None:
    """Verify end-to-end extraction, supersession, storage, embedding, and retrieval."""
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=10)
    t3 = t2 + timedelta(minutes=10)

    msg1 = Message(
        id="msg-1",
        session_id="sess-prod",
        role="user",
        text="Let's use MongoDB for data storage.",
        timestamp=t1,
    )
    msg2 = Message(
        id="msg-2",
        session_id="sess-prod",
        role="user",
        text="Correction: actually use PostgreSQL for our database.",
        timestamp=t2,
    )
    msg3 = Message(
        id="msg-3",
        session_id="sess-prod",
        role="user",
        text="Don't forget to submit timesheets today.",
        timestamp=t3,
    )

    llm_output = [
        {
            "type": "decision",
            "subject": "database",
            "value": "MongoDB",
            "reason": "initial choice",
            "source_msg_id": "msg-1",
            "source_quote": "use MongoDB for data storage",
            "confidence": 1.0,
        },
        {
            "type": "correction",
            "subject": "database",
            "value": "PostgreSQL",
            "reason": "correction to postgres",
            "source_msg_id": "msg-2",
            "source_quote": "actually use PostgreSQL for our database",
            "confidence": 1.0,
        },
    ]

    llm = FakeLLM(responses=[json.dumps(llm_output), "[]"])
    store = SqliteMemoryStore(tmp_path / "integration.db")
    extractor = LLMExtractor(llm)
    embedder = HashEmbedder()
    retriever = HybridRetriever(store, embedder)

    anchor = Anchor(store=store, extractor=extractor, retriever=retriever)

    report = anchor.ingest("sess-prod", [msg1, msg2, msg3])
    assert report.added == 2
    assert report.superseded == 1
    assert report.dropped == 0

    cur_results = anchor.retriever.retrieve("which database?", include_history=False)
    assert len(cur_results) == 1
    assert cur_results[0].value == "PostgreSQL"
    assert cur_results[0].status == MemoryStatus.CURRENT

    all_results = anchor.retriever.retrieve("which database?", include_history=True)
    assert len(all_results) == 2
    vals = {m.value: m.status for m in all_results}
    assert vals["PostgreSQL"] == MemoryStatus.CURRENT
    assert vals["MongoDB"] == MemoryStatus.SUPERSEDED

    re_report = anchor.ingest("sess-prod", [msg1, msg2, msg3])
    assert re_report.added == 0
    assert re_report.superseded == 0

    store.close()
