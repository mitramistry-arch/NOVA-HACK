"""Anchor system adapter interfacing with Context Anchor pipeline."""

import time
from collections.abc import Callable
from typing import Any

from benchmark.counting_llm import CountingLLM
from benchmark.schema import Case
from benchmark.systems.base import Result


class AnchorSystem:
    """System adapter evaluating benchmark cases using Context Anchor pipeline."""

    def __init__(
        self,
        anchor: Any = None,
        llm: CountingLLM | None = None,
        anchor_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Initialize with an Anchor (or a factory for a fresh one per case) and a counter."""
        if anchor is None and anchor_factory is None:
            raise ValueError("AnchorSystem needs an anchor or an anchor_factory")
        self.anchor = anchor
        self.anchor_factory = anchor_factory
        self.llm = llm

    def run(self, case: Case) -> Result:
        """Ingest all sessions and invoke Anchor.ask, passing the historical-query flag."""
        anchor = self.anchor_factory() if self.anchor_factory else self.anchor
        self.anchor = anchor
        p0 = self.llm.prompt_tokens if self.llm else 0
        c0 = self.llm.completion_tokens if self.llm else 0
        t0 = time.perf_counter()

        for session in case.sessions:
            if session:
                anchor.ingest(session[0].session_id, session)

        decision = anchor.ask(
            case.question,
            draft_answer=case.draft_answer,
            include_history=case.include_history,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        retrieved_ids: list[str] = []
        for verdict in getattr(decision, "verdicts", []):
            for mid in getattr(verdict, "memory_ids", []):
                if mid not in retrieved_ids:
                    retrieved_ids.append(mid)
                if hasattr(anchor, "store"):
                    mem = anchor.store.get(mid)
                    if mem and getattr(mem, "source_msg_id", None):
                        src_id = mem.source_msg_id
                        if src_id not in retrieved_ids:
                            retrieved_ids.append(src_id)

        answer = getattr(decision, "final_answer", None) or ""
        action = getattr(decision, "action", "answer")

        prompt_tokens = self.llm.prompt_tokens - p0 if self.llm else None
        completion_tokens = self.llm.completion_tokens - c0 if self.llm else None

        return Result(
            answer=answer,
            action=action,
            retrieved_ids=retrieved_ids,
            latency_ms=elapsed_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_estimated=self.llm.tokens_estimated if self.llm else True,
        )
