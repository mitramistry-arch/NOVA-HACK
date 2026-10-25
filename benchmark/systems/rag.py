"""RAG system: raw chunk vector retrieval without verification or supersession."""

import time

import numpy as np

from benchmark.counting_llm import ANSWER_PROMPT, CountingLLM, is_abstain
from benchmark.schema import Case
from benchmark.systems.base import Result
from nova.core.llm import LLMClient
from nova.core.schemas import DecisionAction
from nova.retrieval.embedder import Embedder, HashEmbedder


class RAGSystem:
    """Standard RAG baseline retrieving top-k raw message chunks by vector similarity."""

    def __init__(
        self,
        llm: LLMClient | CountingLLM,
        embedder: Embedder | None = None,
        k: int = 5,
    ) -> None:
        """Initialize RAG system with LLM client, vector embedder, and top-k count."""
        self.llm = llm if isinstance(llm, CountingLLM) else CountingLLM(llm)
        self.embedder = embedder or HashEmbedder()
        self.k = k

    def run(self, case: Case) -> Result:
        """Retrieve top-k message chunks by cosine similarity and answer with LLM."""
        t0 = time.perf_counter()
        all_msgs = [m for s in case.sessions for m in s]

        if not all_msgs:
            prompt = ANSWER_PROMPT.format(context="", question=case.question)
            answer = self.llm.complete(prompt)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return Result(
                answer=answer,
                action=DecisionAction.ABSTAIN,
                retrieved_ids=[],
                latency_ms=elapsed_ms,
                prompt_tokens=self.llm.last_prompt_tokens,
                completion_tokens=self.llm.last_completion_tokens,
            )

        q_vec = np.asarray(self.embedder.embed(case.question), dtype=np.float32)
        q_norm = np.linalg.norm(q_vec) + 1e-9

        scored_msgs = []
        for msg in all_msgs:
            m_vec = np.asarray(self.embedder.embed(msg.text), dtype=np.float32)
            m_norm = np.linalg.norm(m_vec) + 1e-9
            score = float(np.dot(q_vec, m_vec) / (q_norm * m_norm))
            scored_msgs.append((score, msg))

        scored_msgs.sort(key=lambda x: x[0], reverse=True)
        top_k = scored_msgs[: self.k]

        retrieved_ids = [m.id for _, m in top_k]
        context_str = "\n".join(f"{m.role}: {m.text}" for _, m in top_k)

        prompt = ANSWER_PROMPT.format(context=context_str, question=case.question)
        answer = self.llm.complete(prompt)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        action = DecisionAction.ABSTAIN if is_abstain(answer) else DecisionAction.ANSWER

        return Result(
            answer=answer,
            action=action,
            retrieved_ids=retrieved_ids,
            latency_ms=elapsed_ms,
            prompt_tokens=self.llm.last_prompt_tokens,
            completion_tokens=self.llm.last_completion_tokens,
        )
