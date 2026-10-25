"""Temporal supersession logic for memories.

Memories of a chained type (see ``nova.core.policy``) that share a tenant and subject form
one chain ordered by (timestamp, id). Inserting a memory re-derives the whole chain: every
entry supersedes its predecessor and only the newest stays current. This makes arrival order
irrelevant, resolves timestamp ties deterministically, and heals any legacy chain that had
several current entries. The store owns the transaction; this layer only calls it.
"""

from nova.core.policy import is_chained
from nova.core.protocols import MemoryStore
from nova.core.schemas import Memory, MemoryStatus


def _fresh(mem: Memory) -> Memory:
    """Return the memory with chain-derived fields reset (the chain recomputes them)."""
    return mem.model_copy(update={"status": MemoryStatus.CURRENT, "supersedes_id": None})


def apply(store: MemoryStore, new_memories: list[Memory]) -> int:
    """Store memories and (re)link their supersession chains atomically.

    Returns the number of incoming memories that created at least one new supersession link.
    All writes happen in one store transaction, so a failure part-way leaves no partial chain.
    """
    if not new_memories:
        return 0

    count = 0
    with store.transaction():
        for mem in sorted(new_memories, key=lambda m: (m.timestamp, m.id)):
            if store.get(mem.id) is None:
                store.add(_fresh(mem) if is_chained(mem.type) else mem)
            if not is_chained(mem.type):
                continue
            chain = store.chain_by_subject(mem.subject)
            if store.link_chain([m.id for m in chain]) > 0:
                count += 1
    return count
