"""Embedding protocols, hashing embedder, and neural embedder."""

import hashlib
import re
from typing import Any, Protocol, runtime_checkable

import numpy as np

from nova.core.schemas import EmbeddingRecord

MAX_DIMENSION = 8192


class EmbeddingError(Exception):
    """Raised when an embedder fails or returns unusable vectors."""


@runtime_checkable
class Embedder(Protocol):
    """Protocol for vector embedders.

    ``model_id`` identifies the model/version that makes vectors.
    """

    model_id: str

    def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings."""
        ...


def serialize_embedding(embedding: list[float] | np.ndarray) -> bytes:
    """Serialize embedding vector to float32 BLOB bytes."""
    return np.asarray(embedding, dtype=np.float32).tobytes()


def deserialize_embedding(blob: bytes) -> np.ndarray:
    """Deserialize float32 BLOB bytes into a 1D numpy array."""
    return np.frombuffer(blob, dtype=np.float32)


def embed_records(embedder: Embedder, texts: list[str]) -> list[EmbeddingRecord]:
    """Embed texts and return validated, model-labelled records, or raise EmbeddingError."""
    if not texts:
        return []
    try:
        vectors = embedder.embed_batch(texts)
    except (RuntimeError, OSError, ValueError, ImportError) as e:
        raise EmbeddingError(f"embedding call failed: {type(e).__name__}") from e
    if len(vectors) != len(texts):
        raise EmbeddingError("embedder returned a different number of vectors than texts")
    records: list[EmbeddingRecord] = []
    for vec in vectors:
        arr = np.asarray(vec, dtype=np.float32)
        if arr.ndim != 1 or arr.size == 0 or arr.size > MAX_DIMENSION:
            raise EmbeddingError("embedder returned a vector with an invalid shape")
        if not bool(np.all(np.isfinite(arr))):
            raise EmbeddingError("embedder returned non-finite values")
        records.append(
            EmbeddingRecord(vector=serialize_embedding(arr), model=embedder.model_id, dim=arr.size)
        )
    if len({r.dim for r in records}) != 1:
        raise EmbeddingError("embedder returned vectors of differing dimensions")
    return records


class HashEmbedder:
    """Deterministic 256-dimensional hash-based embedder for testing."""

    DIMENSION = 256
    model_id = "hash-v1"

    def __init__(self, dimension: int = 256) -> None:
        """Initialize hash embedder; the dimension must be between 1 and MAX_DIMENSION."""
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            raise TypeError("dimension must be an integer")
        if dimension < 1 or dimension > MAX_DIMENSION:
            raise ValueError(f"dimension must be between 1 and {MAX_DIMENSION}")
        self.dimension = dimension
        self.model_id = f"hash-v1-{dimension}"

    def embed(self, text: str) -> list[float]:
        """Embed a single text string into a deterministic vector."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings into deterministic unit vectors."""
        results: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self.dimension, dtype=np.float32)
            tokens = re.findall(r"\w+", text.lower())
            for token in tokens:
                idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self.dimension
                sign = (
                    1.0
                    if int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16) % 2 == 0
                    else -1.0
                )
                vec[idx] += sign
                for i in range(len(token) - 2):
                    trigram = token[i : i + 3]
                    t_idx = (
                        int(hashlib.md5(trigram.encode("utf-8")).hexdigest(), 16) % self.dimension
                    )
                    vec[t_idx] += 0.5 * sign

            norm = float(np.linalg.norm(vec))
            if norm > 0.0:
                vec = vec / norm
            results.append(vec.tolist())
        return results


class SentenceTransformerEmbedder:
    """Local neural embedder backed by sentence-transformers."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Initialize embedder with model name."""
        if not model_name or not model_name.strip():
            raise ValueError("model_name must not be empty")
        self.model_name = model_name
        self.model_id = f"st:{model_name}"
        self._model: Any = None

    def _load_model(self) -> Any:
        """Lazily import sentence_transformers and load model."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        """Embed single text using sentence-transformers."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed batch of texts using sentence-transformers."""
        if not texts:
            return []
        model = self._load_model()
        embeddings = model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()
