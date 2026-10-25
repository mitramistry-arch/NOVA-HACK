"""Validation of memory quotes against source message texts."""

import hashlib

from nova.core.schemas import Memory, MemoryStatus, Message
from nova.core.text import normalize
from nova.extraction.semantics import rejection_reason


def generate_memory_id(
    session_id: str,
    msg_id: str,
    subject: str,
    value: str,
) -> str:
    """Compute deterministic 16-character SHA-1 id for a memory."""
    content = f"{session_id}|{msg_id}|{subject}|{value}"
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace and lowercase text for substring comparison."""
    return normalize(text)


def validate_quotes(
    memories: list[Memory],
    messages: list[Message],
) -> tuple[list[Memory], int]:
    """Keep only source-linked memories whose quotes support finalized statements."""
    by_id: dict[str, Message] = {}
    signatures_by_id: dict[str, set[tuple[str, str, str, str]]] = {}
    for message in messages:
        by_id.setdefault(message.id, message)
        signature = (
            message.session_id,
            message.role.value,
            message.text,
            message.timestamp.isoformat(),
        )
        signatures_by_id.setdefault(message.id, set()).add(signature)

    kept: list[Memory] = []
    dropped_count = 0
    seen_ids: set[str] = set()

    for memory in memories:
        message = by_id.get(memory.source_msg_id)
        if message is not None and len(signatures_by_id[memory.source_msg_id]) > 1:
            reason = "source message id is ambiguous"
        else:
            reason = rejection_reason(memory, message)
        if reason is not None:
            dropped_count += 1
            continue

        assert message is not None
        memory_id = generate_memory_id(
            message.session_id,
            memory.source_msg_id,
            memory.subject,
            memory.value,
        )
        if memory_id in seen_ids:
            dropped_count += 1
            continue
        seen_ids.add(memory_id)
        kept.append(
            memory.model_copy(
                update={
                    "id": memory_id,
                    "session_id": message.session_id,
                    "timestamp": message.timestamp,
                    "status": MemoryStatus.CURRENT,
                    "supersedes_id": None,
                }
            )
        )

    return kept, dropped_count
