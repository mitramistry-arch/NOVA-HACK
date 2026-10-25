"""Baseline system: raw prompt with last 20 messages and no memory."""

import time

from benchmark.counting_llm import ANSWER_PROMPT, CountingLLM, is_abstain
from benchmark.schema import BASELINE_WINDOW, Case
from benchmark.systems.base import Result
from nova.core.llm import LLMClient
from nova.core.schemas import DecisionAction


class BaselineSystem:
    """Baseline system relying only on the recent conversation context window."""

    def __init__(self, llm: LLMClient | CountingLLM, window_size: int = BASELINE_WINDOW) -> None:
        """Initialize baseline system with LLM client and context window size."""
        self.llm = llm if isinstance(llm, CountingLLM) else CountingLLM(llm)
        self.window_size = window_size

    def run(self, case: Case) -> Result:
        """Evaluate case by presenting only the last 20 messages."""
        t0 = time.perf_counter()
        all_msgs = [m for s in case.sessions for m in s]
        window_msgs = all_msgs[-self.window_size :]

        context_lines = [f"{m.role}: {m.text}" for m in window_msgs]
        context_str = "\n".join(context_lines)

        prompt = ANSWER_PROMPT.format(context=context_str, question=case.question)
        answer = self.llm.complete(prompt)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        action = DecisionAction.ABSTAIN if is_abstain(answer) else DecisionAction.ANSWER

        return Result(
            answer=answer,
            action=action,
            retrieved_ids=[],
            latency_ms=elapsed_ms,
            prompt_tokens=self.llm.last_prompt_tokens,
            completion_tokens=self.llm.last_completion_tokens,
        )
