"""Embedding maintenance: backfill missing vectors and re-embed incompatible ones."""

from nova.core.protocols import EmbeddingStore
from nova.retrieval.embedder import Embedder, embed_records


def memory_text(subject: str, value: str) -> str:
    """Text embedded for a memory (kept identical for write and read paths)."""
    return f"{subject} {value}"


def reembed_missing(
    store: EmbeddingStore,
    embedder: Embedder,
    *,
    limit: int | None = None,
    batch_size: int = 64,
    dim: int | None = None,
) -> int:
    """Embed memories lacking a compatible vector and persist them; returns the count fixed.

    Raises EmbeddingError (from the embedder) without altering stored data on failure.
    """
    if dim is None:
        dim = embed_records(embedder, ["dimension probe"])[0].dim
    pending = store.missing_embeddings(embedder.model_id, dim, limit)
    done = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        records = embed_records(embedder, [memory_text(m.subject, m.value) for m in chunk])
        done += store.set_embeddings({m.id: r for m, r in zip(chunk, records, strict=True)})
    return done
