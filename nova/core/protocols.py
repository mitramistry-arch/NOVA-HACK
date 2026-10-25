"""Protocol interfaces for core Nova components."""

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from nova.core.schemas import EmbeddingRecord, Memory, Message, Verdict


@runtime_checkable
class Extractor(Protocol):
    """Protocol for extracting structured memories from conversation messages."""

    def extract(self, messages: list[Message]) -> list[Memory]:
        """Extract memories from a sequence of messages."""
        ...


@runtime_checkable
class MemoryStore(Protocol):
    """Protocol for persistent memory storage with supersession support."""

    def transaction(self) -> AbstractContextManager[None]:
        """Return a context manager making the enclosed operations atomic."""
        ...

    def add(self, memory: Memory, embedding: bytes | EmbeddingRecord | None = None) -> None:
        """Add a single memory to the store."""
        ...

    def add_many(
        self,
        memories: list[Memory],
        embeddings: list[bytes | EmbeddingRecord | None] | None = None,
    ) -> None:
        """Add multiple memories to the store."""
        ...

    def get(self, memory_id: str) -> Memory | None:
        """Retrieve a memory by id."""
        ...

    def current_by_subject(self, subject: str) -> Memory | None:
        """Retrieve the newest current memory for a subject (deterministic on ties)."""
        ...

    def current_all_by_subject(self, subject: str) -> list[Memory]:
        """Retrieve every current memory for a subject, newest first."""
        ...

    def chain_by_subject(self, subject: str) -> list[Memory]:
        """Retrieve supersession-eligible memories of a subject, oldest first."""
        ...

    def list_by_session(self, session_id: str) -> list[Memory]:
        """List all memories associated with a session id."""
        ...

    def supersede(self, old_id: str, new_id: str) -> None:
        """Mark an older memory as superseded by a newer memory id."""
        ...

    def link_chain(self, ordered_ids: list[str]) -> int:
        """Make the ids one supersession chain; returns the number of new links."""
        ...


@runtime_checkable
class EmbeddingStore(MemoryStore, Protocol):
    """A MemoryStore that also persists embeddings and exposes retrieval candidates."""

    def candidates(
        self,
        include_history: bool = False,
        session_id: str | None = None,
    ) -> list[tuple[Memory, EmbeddingRecord | None]]:
        """Return memories with their stored embeddings."""
        ...

    def set_embeddings(self, records: dict[str, EmbeddingRecord]) -> int:
        """Attach embeddings to existing memories."""
        ...

    def missing_embeddings(self, model: str, dim: int, limit: int | None = None) -> list[Memory]:
        """Return memories lacking an embedding compatible with model/dim."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """Protocol for memory retrieval."""

    def retrieve(
        self,
        query: str,
        k: int = 5,
        include_history: bool = False,
        session_id: str | None = None,
    ) -> list[Memory]:
        """Retrieve relevant memories matching query."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Protocol for answer claim verification against memories."""

    def verify(
        self,
        question: str,
        answer: str,
        memories: list[Memory],
    ) -> list[Verdict]:
        """Verify claims in answer against retrieved memories."""
        ...
