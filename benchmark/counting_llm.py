"""Token and latency tracking wrapper over LLMClient."""

import time

from nova.core.llm import LLMClient

ANSWER_PROMPT: str = (
    """You are answering a question based strictly on the provided context.
Context:
{context}

Question: {question}

Answer concisely based strictly on the context. If the context does not """
    """contain enough information to answer the question, state: "I do not """
    """have enough information to answer."
Answer:"""
)

ABSTAIN_PHRASES: list[str] = [
    "i do not have enough information",
    "i don't have enough information",
    "not mentioned in the context",
    "not enough information",
    "cannot be answered",
    "cannot answer",
    "no information",
    "i cannot find",
    "i am unable to answer",
    "insufficient evidence",
    "insufficient information",
    "abstain",
]


def is_abstain(text: str) -> bool:
    """Check if text matches any standard abstain phrase."""
    lower = text.lower()
    return any(phrase in lower for phrase in ABSTAIN_PHRASES)


class CountingLLM:
    """Instrumented wrapper recording token counts and call latency."""

    def __init__(self, llm: LLMClient) -> None:
        """Initialize counting wrapper around an LLMClient."""
        self.llm = llm
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.latency_ms: float = 0.0
        self.call_count: int = 0
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_latency_ms: float = 0.0
        self.estimated_calls: int = 0

    @property
    def tokens_estimated(self) -> bool:
        """True when any counted call used the len//4 estimate instead of provider usage."""
        return self.estimated_calls > 0

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Execute completion and tally token counts and latency."""
        t0 = time.perf_counter()
        response = self.llm.complete(prompt, json=json)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        p_tokens = getattr(response, "prompt_tokens", None)
        c_tokens = getattr(response, "completion_tokens", None)

        resp_str = str(response) if not isinstance(response, str) else response

        if p_tokens is None or c_tokens is None:
            self.estimated_calls += 1
        if p_tokens is None:
            p_tokens = len(prompt) // 4 if prompt else 0
        if c_tokens is None:
            c_tokens = len(resp_str) // 4 if resp_str else 0

        self.last_prompt_tokens = int(p_tokens)
        self.last_completion_tokens = int(c_tokens)
        self.last_latency_ms = elapsed_ms

        self.prompt_tokens += self.last_prompt_tokens
        self.completion_tokens += self.last_completion_tokens
        self.latency_ms += self.last_latency_ms
        self.call_count += 1

        return resp_str

    def reset(self) -> None:
        """Reset cumulative token and latency counters."""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latency_ms = 0.0
        self.call_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_latency_ms = 0.0
        self.estimated_calls = 0
