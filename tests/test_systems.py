"""Unit tests for benchmark systems: Baseline, RAG, Anchor adapter, and CountingLLM."""

from datetime import UTC, datetime, timedelta

from benchmark.counting_llm import CountingLLM, is_abstain
from benchmark.schema import Case, Expected, Scenario, Split
from benchmark.systems.anchor import AnchorSystem
from benchmark.systems.base import Result, System
from benchmark.systems.baseline import BaselineSystem
from benchmark.systems.rag import RAGSystem
from nova.core.llm import FakeLLM
from nova.core.schemas import Decision, DecisionAction, IngestReport, Message, Verdict, VerdictLabel
from nova.retrieval.embedder import HashEmbedder


def _create_sample_case(num_fillers: int = 50, planted_idx: int = 0) -> tuple[Case, str, str]:
    """Helper to generate a test case with a planted fact at a specific index."""
    base_time = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    msgs: list[Message] = []
    key_fact = "PostgreSQL 16.2"
    planted_id = "msg_planted_test"

    for i in range(num_fillers):
        t = base_time + timedelta(minutes=i * 2)
        if i == planted_idx:
            msgs.append(Message(id=planted_id, session_id="s1", role="user", text=f"The database is {key_fact}.", timestamp=t))
        else:
            msgs.append(Message(id=f"msg_{i}", session_id="s1", role="user", text=f"Neutral message index {i}.", timestamp=t))

    case = Case(
        id="case_sys_test_01",
        scenario=Scenario.BURIED_FACT,
        split=Split.DEV,
        sessions=[msgs],
        context_dropped=True,
        question="What is the database?",
        expected=Expected(action=DecisionAction.ANSWER, key_fact=key_fact, source_msg_id=planted_id),
    )
    return case, planted_id, key_fact


def test_counting_llm_tracking() -> None:
    """Verify that CountingLLM measures token counts and call latency."""
    fake = FakeLLM(default_response="Short response.")
    counter = CountingLLM(fake)

    prompt = "Hello world, this is a prompt."
    resp = counter.complete(prompt)

    assert resp == "Short response."
    assert counter.call_count == 1
    assert counter.prompt_tokens == len(prompt) // 4
    assert counter.completion_tokens == len("Short response.") // 4
    assert counter.latency_ms >= 0.0

    counter.reset()
    assert counter.call_count == 0
    assert counter.prompt_tokens == 0
    assert counter.completion_tokens == 0


def test_abstain_phrase_detection() -> None:
    """Verify detection of various standard abstain phrases."""
    assert is_abstain("I do not have enough information to answer.")
    assert is_abstain("This was not mentioned in the context provided.")
    assert is_abstain("Insufficient information to determine the result.")
    assert not is_abstain("The primary database is PostgreSQL 16.2.")


def test_baseline_cannot_see_outside_window() -> None:
    """Verify baseline truly cannot see messages outside its 20-message window."""
    case, planted_id, key_fact = _create_sample_case(num_fillers=50, planted_idx=0)
    fake = FakeLLM(default_response="PostgreSQL")
    system = BaselineSystem(fake, window_size=20)

    result = system.run(case)

    assert isinstance(result, Result)
    assert len(fake.calls) == 1
    prompt_sent = fake.calls[0]["prompt"]

    assert key_fact not in prompt_sent
    assert planted_id not in prompt_sent
    assert "Neutral message index 49" in prompt_sent
    assert "Neutral message index 30" in prompt_sent
    assert "Neutral message index 28" not in prompt_sent


def test_baseline_system_actions() -> None:
    """Verify baseline system returns valid Result with appropriate action."""
    case, _, _ = _create_sample_case(num_fillers=10, planted_idx=5)

    fake_ans = FakeLLM(default_response="The database is PostgreSQL 16.2.")
    sys_ans = BaselineSystem(fake_ans, window_size=20)
    res_ans = sys_ans.run(case)
    assert res_ans.action == DecisionAction.ANSWER
    assert res_ans.retrieved_ids == []

    fake_abs = FakeLLM(default_response="I do not have enough information to answer.")
    sys_abs = BaselineSystem(fake_abs, window_size=20)
    res_abs = sys_abs.run(case)
    assert res_abs.action == DecisionAction.ABSTAIN
    assert res_abs.retrieved_ids == []


def test_rag_system_retrieval_and_result() -> None:
    """Verify RAG system retrieves top-k chunks and constructs valid Result."""
    case, planted_id, _key_fact = _create_sample_case(num_fillers=30, planted_idx=15)
    fake = FakeLLM(default_response="The database is PostgreSQL 16.2.")
    embedder = HashEmbedder()
    rag = RAGSystem(fake, embedder=embedder, k=5)

    assert isinstance(rag, System)
    result = rag.run(case)

    assert isinstance(result, Result)
    assert len(result.retrieved_ids) == 5
    assert planted_id in result.retrieved_ids
    assert result.action == DecisionAction.ANSWER
    assert result.prompt_tokens > 0


class StubAnchor:
    """Stub implementation of Anchor interface for benchmark testing."""

    def __init__(self, action: DecisionAction = DecisionAction.ANSWER, answer: str = "PostgreSQL 16.2") -> None:
        """Initialize stub anchor."""
        self.action = action
        self.answer = answer
        self.ingest_calls: list[tuple[str, list[Message]]] = []
        self.ask_calls: list[tuple[str, str | None, bool]] = []

    def ingest(self, session_id: str, messages: list[Message]) -> IngestReport:
        """Record ingestion call."""
        self.ingest_calls.append((session_id, messages))
        return IngestReport(added=len(messages), superseded=0, dropped=0)

    def ask(
        self,
        question: str,
        draft_answer: str | None = None,
        *,
        include_history: bool = False,
    ) -> Decision:
        """Return predetermined decision."""
        self.ask_calls.append((question, draft_answer, include_history))
        verdict = Verdict(
            claim=f"Claim: {question}",
            label=VerdictLabel.SUPPORTED,
            memory_ids=["mem_test_101"],
            rationale="Supported by evidence",
        )
        return Decision(
            action=self.action,
            verdicts=[verdict],
            final_answer=self.answer,
        )


def test_anchor_system_adapter() -> None:
    """Verify AnchorSystem ingests sessions, calls ask, and maps Decision.action directly."""
    case, _planted_id, key_fact = _create_sample_case(num_fillers=10, planted_idx=2)
    stub = StubAnchor(action=DecisionAction.ANSWER, answer=key_fact)
    system = AnchorSystem(stub)

    assert isinstance(system, System)
    result = system.run(case)

    assert len(stub.ingest_calls) == len(case.sessions)
    assert stub.ask_calls[0] == (case.question, case.draft_answer, case.include_history)
    assert result.action == DecisionAction.ANSWER
    assert result.answer == key_fact
    assert "mem_test_101" in result.retrieved_ids


def test_anchor_system_conflict_mapping() -> None:
    """Verify AnchorSystem maps conflict decision action directly."""
    case, _, _ = _create_sample_case(num_fillers=5, planted_idx=1)
    stub = StubAnchor(action=DecisionAction.FLAG_CONFLICT, answer="Conflict detected")
    system = AnchorSystem(stub)

    result = system.run(case)
    assert result.action == DecisionAction.FLAG_CONFLICT
    assert result.answer == "Conflict detected"
