"""Anchor facade integrating extraction, storage, supersession, and retrieval."""

import logging
from typing import Any

from nova.core.protocols import Extractor, MemoryStore, Retriever
from nova.core.schemas import Decision, IngestReport, Message
from nova.extraction.quote_validator import validate_quotes
from nova.retrieval.embedder import EmbeddingError, embed_records
from nova.retrieval.maintenance import memory_text, reembed_missing
from nova.temporal.supersession import apply as apply_supersession
from nova.verification.service import AnchorVerifier

logger = logging.getLogger("nova.pipeline")

BACKFILL_LIMIT = 500


class Anchor:
    """Anchor facade providing unified ingestion, query answering, and verification."""

    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor,
        retriever: Retriever,
        audit_log: Any = None,
        verifier: Any = None,
    ) -> None:
        """Initialize Anchor with underlying store, extractor, and retriever."""
        self.store = store
        self.extractor = extractor
        self.retriever = retriever
        self.audit_log = audit_log
        self._ingested_msg_ids: set[str] = set()
        self.verifier = verifier or AnchorVerifier(
            retriever=retriever,
            audit=audit_log,
            llm=getattr(extractor, "llm", None),
        )

    def ingest(self, session_id: str, messages: list[Message]) -> IngestReport:
        """Extract, validate, store, supersede, and embed memories from conversation turns."""
        if not messages:
            return IngestReport(added=0, superseded=0, dropped=0)

        normalized_messages = [
            msg
            if msg.session_id == session_id
            else msg.model_copy(update={"session_id": session_id})
            for msg in messages
        ]

        msg_ids = [m.id for m in normalized_messages]
        if msg_ids and all(m_id in self._ingested_msg_ids for m_id in msg_ids):
            logger.info(
                "ingest skipped: duplicate messages",
                extra={
                    "session_id": session_id,
                    "messages_count": len(messages),
                    "added": 0,
                    "superseded": 0,
                    "dropped": 0,
                },
            )
            return IngestReport(added=0, superseded=0, dropped=0)

        raw_memories = self.extractor.extract(normalized_messages)
        valid_memories, dropped_count = validate_quotes(raw_memories, normalized_messages)

        new_memories = [m for m in valid_memories if self.store.get(m.id) is None]

        embedder = getattr(self.retriever, "embedder", None)
        records = None
        if embedder is not None and new_memories:
            # Embed before opening the write transaction; a failure here writes nothing.
            records = embed_records(
                embedder, [memory_text(m.subject, m.value) for m in new_memories]
            )

        with self.store.transaction():
            if new_memories:
                self.store.add_many(new_memories, records)
            superseded_count = apply_supersession(self.store, valid_memories)

        if embedder is not None:
            self._backfill_embeddings(embedder, records[0].dim if records else None)

        for m_id in msg_ids:
            self._ingested_msg_ids.add(m_id)

        report = IngestReport(
            added=len(new_memories),
            superseded=superseded_count,
            dropped=dropped_count,
        )

        logger.info(
            "ingest completed",
            extra={
                "session_id": session_id,
                "messages_count": len(messages),
                "added": report.added,
                "superseded": report.superseded,
                "dropped": report.dropped,
            },
        )
        return report

    def _backfill_embeddings(self, embedder: Any, dim: int | None) -> None:
        """Populate embeddings that earlier attempts left missing (best effort, bounded)."""
        try:
            fixed = reembed_missing(self.store, embedder, limit=BACKFILL_LIMIT, dim=dim)  # type: ignore[arg-type]
        except EmbeddingError:
            logger.warning("embedding backfill failed; will retry on a later ingest")
            return
        if fixed:
            logger.info("embedding backfill completed", extra={"embedded": fixed})

    def reembed(self, limit: int | None = None) -> int:
        """Re-embed memories whose vectors are missing or incompatible with the active embedder."""
        embedder = getattr(self.retriever, "embedder", None)
        if embedder is None:
            return 0
        return reembed_missing(self.store, embedder, limit=limit)  # type: ignore[arg-type]

    def ask(
        self,
        question: str,
        draft_answer: str | None = None,
        k: int = 5,
        *,
        include_history: bool = False,
    ) -> Decision:
        """Answer from current or historical memories using the verification service."""
        return self.verifier.ask(question, draft_answer, k=k, include_history=include_history)

    def verify(
        self,
        question: str,
        answer: str,
        k: int = 5,
        *,
        include_history: bool = False,
    ) -> Decision:
        """Verify a draft, optionally treating evidenced superseded values as historical."""
        return self.verifier.verify(question, answer, k=k, include_history=include_history)

    def get_audit(self, audit_id: str) -> Any:
        """Retrieve audit record by id."""
        if self.audit_log is not None:
            return self.audit_log.get(audit_id)
        return None
