"""Hybrid retriever combining vector cosine similarity and keyword overlap."""

import re

import numpy as np

from nova.core.protocols import EmbeddingStore
from nova.core.schemas import EmbeddingRecord, Memory
from nova.retrieval.embedder import Embedder, deserialize_embedding, embed_records
from nova.store.base import EmbeddingCompatibilityError

MAX_K = 50


class RetrievalError(ValueError):
    """Raised for invalid retrieval parameters."""


def normalize_k(k: int) -> int:
    """Validate ``k`` (must be an integer >= 1) and cap it at MAX_K."""
    if isinstance(k, bool) or not isinstance(k, int):
        raise RetrievalError("k must be an integer")
    if k < 1:
        raise RetrievalError("k must be at least 1")
    return min(k, MAX_K)


class HybridRetriever:
    """Hybrid retriever scoring memories by 0.6*cosine + 0.4*keyword overlap."""

    def __init__(
        self,
        store: EmbeddingStore,
        embedder: Embedder,
        *,
        accept_unlabeled_embeddings: bool = False,
    ) -> None:
        """Initialize with a store exposing public candidate access and an embedder.

        Stored vectors must have been produced by ``embedder.model_id`` with the same
        dimension; otherwise retrieval raises EmbeddingCompatibilityError instead of
        returning misleading scores. ``accept_unlabeled_embeddings`` admits legacy vectors
        that carry no model label (dimension is still checked).
        """
        if not isinstance(store, EmbeddingStore):
            raise TypeError("store must implement the EmbeddingStore protocol (candidates())")
        self.store = store
        self.embedder = embedder
        self.accept_unlabeled_embeddings = accept_unlabeled_embeddings

    def _candidate_vectors(
        self,
        candidates: list[tuple[Memory, EmbeddingRecord | None]],
        dim: int,
    ) -> list[np.ndarray]:
        """Return one validated vector per candidate, embedding missing ones on the fly."""
        model = self.embedder.model_id
        vectors: list[np.ndarray | None] = []
        incompatible = 0
        missing: list[int] = []
        for idx, (_, rec) in enumerate(candidates):
            if rec is None:
                vectors.append(None)
                missing.append(idx)
                continue
            labelled_ok = rec.model == model or (
                rec.model is None and self.accept_unlabeled_embeddings
            )
            vec = deserialize_embedding(rec.vector)
            if not labelled_ok or rec.dim != dim or vec.size != dim or not np.all(np.isfinite(vec)):
                incompatible += 1
                vectors.append(None)
                continue
            vectors.append(vec)
        if incompatible:
            raise EmbeddingCompatibilityError(
                f"{incompatible} stored embedding(s) are incompatible with embedder "
                f"'{model}' (dimension {dim}); re-embed the memories before retrieving"
            )
        if missing:
            texts = [f"{candidates[i][0].subject} {candidates[i][0].value}" for i in missing]
            for i, rec in zip(missing, embed_records(self.embedder, texts), strict=True):
                if rec.dim != dim:
                    raise EmbeddingCompatibilityError("embedder dimension changed between calls")
                vectors[i] = deserialize_embedding(rec.vector)
        return [v for v in vectors if v is not None]

    def retrieve(
        self,
        query: str,
        k: int = 5,
        include_history: bool = False,
        session_id: str | None = None,
    ) -> list[Memory]:
        """Retrieve top-k memories using hybrid cosine and keyword overlap scoring."""
        k = normalize_k(k)
        candidates = self.store.candidates(include_history, session_id)
        if not candidates:
            return []

        q_rec = embed_records(self.embedder, [query])[0]
        q_vec = deserialize_embedding(q_rec.vector)
        q_norm = float(np.linalg.norm(q_vec))
        q_tokens = set(re.findall(r"\w+", query.lower()))
        cand_vectors = self._candidate_vectors(candidates, q_rec.dim)

        scored: list[tuple[float, Memory]] = []
        for (mem, _), cand_vec in zip(candidates, cand_vectors, strict=True):
            cand_norm = float(np.linalg.norm(cand_vec))
            if q_norm > 0.0 and cand_norm > 0.0:
                cosine = max(0.0, float(np.dot(q_vec, cand_vec) / (q_norm * cand_norm)))
            else:
                cosine = 0.0

            content_tokens = set(
                re.findall(r"\w+", f"{mem.subject} {mem.value} {mem.source_quote}".lower())
            )
            token_overlap = (
                len(q_tokens & content_tokens) / max(1, len(q_tokens)) if q_tokens else 0.0
            )

            subj_words = set(re.findall(r"\w+", mem.subject.lower()))
            subj_match = (
                1.0 if (q_tokens & subj_words or mem.subject.lower() in query.lower()) else 0.0
            )
            keyword_score = min(1.0, 0.7 * token_overlap + 0.3 * subj_match)

            score = 0.6 * cosine + 0.4 * keyword_score
            scored.append((score, mem))

        scored.sort(key=lambda item: (-item[0], -item[1].timestamp.timestamp(), item[1].id))
        return [mem for _, mem in scored[:k]]
