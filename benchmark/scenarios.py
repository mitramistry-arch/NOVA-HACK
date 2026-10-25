"""Scenario builders for Nova benchmark evaluations."""

import random
from datetime import datetime, timedelta

from benchmark.fillers import generate_filler
from benchmark.schema import (
    BASELINE_WINDOW,
    Case,
    Expected,
    Scenario,
    Split,
    compute_context_dropped,
)
from nova.core.schemas import DecisionAction, Message

SYNONYM_BANK: list[tuple[str, str]] = [
    ("database", "datastore"),
    ("engine", "system"),
    ("primary", "main"),
    ("port", "socket"),
    ("cipher", "algorithm"),
    ("payload", "data"),
    ("bucket", "container"),
    ("storage", "repository"),
    ("archive", "cold storage"),
    ("format", "standard"),
    ("limit", "threshold"),
    ("timeout", "deadline"),
    ("rate", "frequency"),
    ("lead", "architect"),
    ("period", "duration"),
    ("cluster", "fleet"),
    ("region", "zone"),
    ("encryption", "cryptography"),
    ("security", "protection"),
    ("selected", "designated"),
    ("confirmed", "verified"),
    ("decided", "established"),
    ("agreed", "settled"),
    ("configured", "set up"),
    ("mandates", "requires"),
    ("inbound", "incoming"),
    ("outbound", "outgoing"),
    ("transmissions", "traffic"),
    ("credentials", "tokens"),
    ("session", "auth context"),
    ("unanswered", "timed out"),
    ("duration", "interval"),
    ("procedure", "routine"),
    ("preserves", "retains"),
    ("records", "entries"),
    ("relational", "SQL"),
    ("analytical", "OLAP"),
    ("distributed", "clustered"),
    ("tolerance", "budget"),
    ("maximum", "ceiling"),
    ("developer", "engineer"),
    ("gateway", "proxy"),
]

BURIED_FACT_TEMPLATES = [
    "We confirmed that the {label} is {value}.",
    "The engineering group agreed on setting the {label} to {value}.",
    "According to yesterday's consensus, the {label} has been defined as {value}.",
]

BURIED_QUESTION_TEMPLATES = [
    "What is the {label}?",
    "Which value was selected for the {label}?",
    "Can you state the {label} chosen by the team?",
]

DECISION_INITIAL_TEMPLATES = [
    "We initially decided that the {label} is {value}.",
    "The earliest decision selected {value} for the {label}.",
    "In the previous sprint, we configured the {label} as {value}.",
]

DECISION_REVERSED_TEMPLATES = [
    "Correction: we reversed that decision, so the {label} is now {value}.",
    "Update on the earlier consensus: we changed the {label} to {value}.",
    "Following an architecture review, the {label} was updated to {value}.",
]

DECISION_CURRENT_QUESTIONS = [
    "What is the current {label}?",
    "What is the active {label} after the update?",
    "Which value currently serves as the {label}?",
]

DECISION_OLD_QUESTIONS = [
    "What was the previous {label} before the correction?",
    "Which original value was chosen for the {label} prior to the update?",
    "What was the old {label} before the decision was reversed?",
]

UNANSWERABLE_QUESTIONS = [
    "What compliance framework did the team choose for auditing?",
    "What is the recovery time objective for disaster recovery?",
    "What KMS key alias encrypts the cold storage backups?",
    "Which external third-party vulnerability scanner is configured?",
]

CONTRADICTING_DRAFT_TEMPLATES = [
    "The official {label} is {value}.",
    "According to current architecture guidelines, the {label} is {value}.",
    "We verified that the {label} is set to {value}.",
]

CONTRADICTING_DRAFT_ANSWERS = [
    "The {label} is currently {value}.",
    "Based on project records, the {label} is configured as {value}.",
    "We are utilizing {value} for the {label}.",
]

PARAPHRASE_PLANTED_TEMPLATES = [
    "The engineering group chose {value} for the main persistence layer.",
    "Network engineers bound {value} for incoming diagnostic transmissions.",
    "Cryptographic policy mandates {value} across outbound wire envelopes.",
]

PARAPHRASE_QUESTION_TEMPLATES = [
    "Which relational datastore stores core entries?",
    "Which socket number listens for remote health signals?",
    "Which symmetric algorithm protects binary packet data?",
]

CROSS_SESSION_PLANTED_TEMPLATES = [
    "In this initial meeting, we finalized the {label} as {value}.",
    "Early planning confirmed that the {label} is {value}.",
    "The opening session concluded with {value} as the {label}.",
]

CROSS_SESSION_QUESTIONS = [
    "What was finalized as the {label} in the earlier discussion?",
    "Which {label} was chosen during the opening session?",
    "What value was recorded for the {label} in the initial conversation?",
]

VALID_ANSWER_PLANTED_TEMPLATES = [
    "We confirmed that the {label} is {value}.",
    "The team settled on {value} for the {label}.",
    "Official specification sets the {label} to {value}.",
]

VALID_DRAFT_TEMPLATES = [
    "The {label} is {value}.",
    "According to the team agreement, the {label} is {value}.",
    "The confirmed {label} is {value}.",
]


def _make_msg(
    msg_id: str,
    session_id: str,
    text: str,
    timestamp: datetime,
    role: str = "user",
) -> Message:
    """Helper to construct a typed Message instance."""
    return Message(
        id=msg_id,
        session_id=session_id,
        role=role,
        text=text,
        timestamp=timestamp,
    )


def build_buried_fact(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a buried_fact benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    template = rng.choice(BURIED_FACT_TEMPLATES)
    planted_text = template.format(label=domain["label"], value=domain["fact_value"])
    question = rng.choice(BURIED_QUESTION_TEMPLATES).format(label=domain["label"])

    planted_depth = rng.randint(0, num_fillers - (BASELINE_WINDOW + 5))
    distractor_depths = sorted(rng.sample(range(num_fillers + 1), num_distractors))

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0
    d_idx = 0
    planted_id = f"msg_{case_id}_planted"

    for f_i in range(num_fillers):
        if d_idx < len(distractor_depths) and f_i == distractor_depths[d_idx]:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, distractor_texts[d_idx], t_cursor))
            m_idx += 1
            d_idx += 1

        if f_i == planted_depth:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))

        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    if planted_depth >= num_fillers:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))

    while d_idx < len(distractor_depths):
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, distractor_texts[d_idx], t_cursor))
        m_idx += 1
        d_idx += 1

    return Case(
        id=case_id,
        scenario=Scenario.BURIED_FACT,
        split=split,
        sessions=[msgs],
        context_dropped=compute_context_dropped([msgs], planted_id),
        question=question,
        draft_answer=None,
        include_history=False,
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact=domain["fact_value"],
            source_msg_id=planted_id,
        ),
    )


def build_decision_reversed(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a decision_reversed benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    ask_old = rng.random() < 0.20

    old_tmpl = rng.choice(DECISION_INITIAL_TEMPLATES)
    old_text = old_tmpl.format(label=domain["label"], value=domain["old_value"])
    rev_tmpl = rng.choice(DECISION_REVERSED_TEMPLATES)
    rev_text = rev_tmpl.format(label=domain["label"], value=domain["fact_value"])

    max_rev = num_fillers - (BASELINE_WINDOW + 5)
    depth_old = rng.randint(5, min(max(6, num_fillers // 3), max_rev - 10))
    depth_rev = rng.randint(depth_old + 10, max_rev)

    old_id = f"planted_{case_id}_old"
    rev_id = f"planted_{case_id}_rev"

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0

    for f_i in range(num_fillers):
        if f_i == depth_old:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(old_id, sess_id, old_text, t_cursor))
        elif f_i == depth_rev:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(rev_id, sess_id, rev_text, t_cursor))

        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    placed = {m.id for m in msgs}
    for pid, ptext in ((old_id, old_text), (rev_id, rev_text)):
        if pid not in placed:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(pid, sess_id, ptext, t_cursor))

    for d_text in distractor_texts:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, d_text, t_cursor))
        m_idx += 1

    if ask_old:
        question = rng.choice(DECISION_OLD_QUESTIONS).format(label=domain["label"])
        key_fact = domain["old_value"]
        source_id = old_id
        include_history = True
    else:
        question = rng.choice(DECISION_CURRENT_QUESTIONS).format(label=domain["label"])
        key_fact = domain["fact_value"]
        source_id = rev_id
        include_history = False

    return Case(
        id=case_id,
        scenario=Scenario.DECISION_REVERSED,
        split=split,
        sessions=[msgs],
        context_dropped=compute_context_dropped([msgs], source_id),
        question=question,
        draft_answer=None,
        include_history=include_history,
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact=key_fact,
            source_msg_id=source_id,
        ),
    )


def build_unanswerable(
    case_id: str,
    split: Split,
    rng: random.Random,
    unans: dict,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build an unanswerable benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0

    for _ in range(num_fillers):
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    for d_text in distractor_texts:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, d_text, t_cursor))
        m_idx += 1

    question = rng.choice(UNANSWERABLE_QUESTIONS)

    return Case(
        id=case_id,
        scenario=Scenario.UNANSWERABLE,
        split=split,
        sessions=[msgs],
        context_dropped=False,
        question=question,
        draft_answer=None,
        include_history=False,
        expected=Expected(
            action=DecisionAction.ABSTAIN,
            key_fact=None,
            source_msg_id=None,
        ),
    )


def build_contradicting_draft(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a contradicting_draft benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    planted_template = rng.choice(CONTRADICTING_DRAFT_TEMPLATES)
    planted_text = planted_template.format(label=domain["label"], value=domain["fact_value"])
    planted_depth = rng.randint(0, num_fillers)
    planted_id = f"msg_{case_id}_planted"

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0

    for f_i in range(num_fillers):
        if f_i == planted_depth:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    if planted_depth >= num_fillers:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))

    for d_text in distractor_texts:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, d_text, t_cursor))
        m_idx += 1

    draft_val = rng.choice([domain["old_value"], domain["wrong_value"]])
    draft = rng.choice(CONTRADICTING_DRAFT_ANSWERS).format(label=domain["label"], value=draft_val)
    action = DecisionAction.CORRECT if draft_val == domain["old_value"] else DecisionAction.FLAG_CONFLICT

    return Case(
        id=case_id,
        scenario=Scenario.CONTRADICTING_DRAFT,
        split=split,
        sessions=[msgs],
        context_dropped=compute_context_dropped([msgs], planted_id),
        question=rng.choice(BURIED_QUESTION_TEMPLATES).format(label=domain["label"]),
        draft_answer=draft,
        include_history=False,
        expected=Expected(
            action=action,
            key_fact=domain["fact_value"],
            source_msg_id=planted_id,
        ),
    )


def build_paraphrased(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a paraphrased benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    planted_text = domain["paraphrase_planted"].format(value=domain["fact_value"])
    question = domain["paraphrase_question"]
    planted_depth = rng.randint(0, num_fillers - (BASELINE_WINDOW + 5))
    planted_id = f"msg_{case_id}_planted"

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0

    for f_i in range(num_fillers):
        if f_i == planted_depth:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    if planted_depth >= num_fillers:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))

    for d_text in distractor_texts:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, d_text, t_cursor))
        m_idx += 1

    return Case(
        id=case_id,
        scenario=Scenario.PARAPHRASED,
        split=split,
        sessions=[msgs],
        context_dropped=compute_context_dropped([msgs], planted_id),
        question=question,
        draft_answer=None,
        include_history=False,
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact=domain["fact_value"],
            source_msg_id=planted_id,
        ),
    )


def build_cross_session(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a cross_session benchmark evaluation case."""
    sess1_id = f"sess_{case_id}_1"
    sess2_id = f"sess_{case_id}_2"
    sess3_id = f"sess_{case_id}_3"

    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    n1 = max(15, num_fillers // 3)
    n2 = max(15, num_fillers // 3)
    n3 = max(15, num_fillers - n1 - n2)

    planted_id = f"msg_{case_id}_planted"
    tmpl = rng.choice(CROSS_SESSION_PLANTED_TEMPLATES)
    planted_text = tmpl.format(label=domain["label"], value=domain["fact_value"])
    question = rng.choice(CROSS_SESSION_QUESTIONS).format(label=domain["label"])

    s1_msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0
    p_depth = rng.randint(0, n1)

    for f_i in range(n1):
        if f_i == p_depth:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            s1_msgs.append(_make_msg(planted_id, sess1_id, planted_text, t_cursor))
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        s1_msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess1_id, filler, t_cursor))
        m_idx += 1

    if p_depth >= n1:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        s1_msgs.append(_make_msg(planted_id, sess1_id, planted_text, t_cursor))

    t_cursor += timedelta(hours=2)
    s2_msgs: list[Message] = []
    for _ in range(n2):
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        s2_msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess2_id, filler, t_cursor))
        m_idx += 1
    t_cursor += timedelta(minutes=rng.randint(2, 5))
    s2_msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess2_id, distractor_texts[0], t_cursor))
    m_idx += 1

    t_cursor += timedelta(hours=2)
    s3_msgs: list[Message] = []
    for _ in range(n3):
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        s3_msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess3_id, filler, t_cursor))
        m_idx += 1
    for d_text in distractor_texts[1:]:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        s3_msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess3_id, d_text, t_cursor))
        m_idx += 1

    return Case(
        id=case_id,
        scenario=Scenario.CROSS_SESSION,
        split=split,
        sessions=[s1_msgs, s2_msgs, s3_msgs],
        context_dropped=compute_context_dropped(
            [s1_msgs, s2_msgs, s3_msgs], planted_id
        ),
        question=question,
        draft_answer=None,
        include_history=False,
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact=domain["fact_value"],
            source_msg_id=planted_id,
        ),
    )


def build_valid_answer(
    case_id: str,
    split: Split,
    rng: random.Random,
    domain: dict,
    base_time: datetime,
) -> Case:
    """Build a valid_answer benchmark evaluation case."""
    sess_id = f"sess_{case_id}_1"
    num_fillers = rng.randint(50, 200)
    num_distractors = rng.randint(2, 3)
    distractor_texts = domain["distractors"][:num_distractors]

    forbidden = {
        domain["fact_value"],
        domain["old_value"],
        domain["wrong_value"],
        *domain["distractor_values"],
    }

    tmpl = rng.choice(VALID_ANSWER_PLANTED_TEMPLATES)
    planted_text = tmpl.format(label=domain["label"], value=domain["fact_value"])
    planted_depth = rng.randint(0, num_fillers)
    planted_id = f"msg_{case_id}_planted"

    msgs: list[Message] = []
    t_cursor = base_time
    m_idx = 0

    for f_i in range(num_fillers):
        if f_i == planted_depth:
            t_cursor += timedelta(minutes=rng.randint(2, 5))
            msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        filler = generate_filler(rng, forbidden)
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, filler, t_cursor))
        m_idx += 1

    if planted_depth >= num_fillers:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(planted_id, sess_id, planted_text, t_cursor))

    for d_text in distractor_texts:
        t_cursor += timedelta(minutes=rng.randint(2, 5))
        msgs.append(_make_msg(f"msg_{case_id}_{m_idx}", sess_id, d_text, t_cursor))
        m_idx += 1

    draft = rng.choice(VALID_DRAFT_TEMPLATES).format(label=domain["label"], value=domain["fact_value"])

    return Case(
        id=case_id,
        scenario=Scenario.VALID_ANSWER,
        split=split,
        sessions=[msgs],
        context_dropped=compute_context_dropped([msgs], planted_id),
        question=rng.choice(BURIED_QUESTION_TEMPLATES).format(label=domain["label"]),
        draft_answer=draft,
        include_history=False,
        expected=Expected(
            action=DecisionAction.ANSWER,
            key_fact=domain["fact_value"],
            source_msg_id=planted_id,
        ),
    )


SCENARIO_BUILDERS = {
    Scenario.BURIED_FACT: build_buried_fact,
    Scenario.DECISION_REVERSED: build_decision_reversed,
    Scenario.UNANSWERABLE: build_unanswerable,
    Scenario.CONTRADICTING_DRAFT: build_contradicting_draft,
    Scenario.PARAPHRASED: build_paraphrased,
    Scenario.CROSS_SESSION: build_cross_session,
    Scenario.VALID_ANSWER: build_valid_answer,
}
