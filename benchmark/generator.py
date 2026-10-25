"""Deterministic case generator for Nova evaluation benchmark."""

import hashlib
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from benchmark.scenarios import SCENARIO_BUILDERS, build_unanswerable
from benchmark.schema import Case, Scenario, Split, compute_context_dropped
from nova.core.schemas import Message

DOMAINS: list[dict] = [
    {
        "subject": "primary_database",
        "label": "primary database",
        "fact_value": "PostgreSQL 16.2",
        "old_value": "MySQL 8.4",
        "wrong_value": "MongoDB 7.0",
        "planted_template": "We confirmed that the primary database is {value}.",
        "old_template": "We initially decided that the primary database is {value}.",
        "reversed_template": "Correction: we reversed that decision, so the primary database "
        "is now {value}.",
        "question": "What is the primary database?",
        "current_question": "What is the current primary database?",
        "cross_session_question": "What was confirmed as the primary database in the earlier "
        "discussion?",
        "paraphrase_planted": "The engineering team chose {value} for the main persistence layer.",
        "paraphrase_question": "Which relational engine stores core records?",
        "distractors": [
            "For analytical workloads, the analytics database is ClickHouse 24.3.",
            "The in-memory cache store is configured as Redis 7.2.",
            "The staging database is running CockroachDB 23.2.",
        ],
        "distractor_values": ["ClickHouse 24.3", "Redis 7.2", "CockroachDB 23.2"],
    },
    {
        "subject": "telemetry_port",
        "label": "telemetry port",
        "fact_value": "9443",
        "old_value": "8080",
        "wrong_value": "7070",
        "planted_template": "We agreed that the telemetry port is {value}.",
        "old_template": "We initially designated the telemetry port as {value}.",
        "reversed_template": "Correction: we reversed that choice, and the telemetry port is "
        "now {value}.",
        "question": "What is the telemetry port?",
        "current_question": "What is the current telemetry port?",
        "cross_session_question": "What was agreed as the telemetry port in the earlier "
        "discussion?",
        "paraphrase_planted": "Network engineers bound {value} for incoming diagnostic "
        "transmissions.",
        "paraphrase_question": "Which socket number listens for remote health signals?",
        "distractors": [
            "The service healthcheck port is set to 8081.",
            "The internal metrics port listens on 9090.",
            "The administrative console port is bound to 8888.",
        ],
        "distractor_values": ["8081", "9090", "8888"],
    },
    {
        "subject": "payload_cipher",
        "label": "payload cipher",
        "fact_value": "AES-256-GCM",
        "old_value": "ChaCha20-Poly1305",
        "wrong_value": "Triple-DES",
        "planted_template": "The security review established that the payload cipher is {value}.",
        "old_template": "We initially selected {value} as the payload cipher.",
        "reversed_template": "Correction: we changed our encryption standard, so the payload "
        "cipher is now {value}.",
        "question": "What is the payload cipher?",
        "current_question": "What is the current payload cipher?",
        "cross_session_question": "What was established as the payload cipher in the earlier "
        "discussion?",
        "paraphrase_planted": "Cryptographic policy mandates {value} across outbound wire "
        "envelopes.",
        "paraphrase_question": "Which symmetric standard protects binary packet payloads?",
        "distractors": [
            "The digest calculation uses SHA3-512 for checksums.",
            "The TLS handshake utilizes X25519 for ephemeral key exchange.",
            "The message authentication code relies on HMAC-BLAKE3.",
        ],
        "distractor_values": ["SHA3-512", "X25519", "HMAC-BLAKE3"],
    },
    {
        "subject": "archive_bucket",
        "label": "archive bucket",
        "fact_value": "nova-cold-archive-01",
        "old_value": "nova-glacier-vault-beta",
        "wrong_value": "temp-scratch-bucket",
        "planted_template": "We decided that the archive bucket is {value}.",
        "old_template": "We previously specified the archive bucket as {value}.",
        "reversed_template": "Correction: we updated our cloud config, and the archive bucket "
        "is now {value}.",
        "question": "What is the archive bucket?",
        "current_question": "What is the current archive bucket?",
        "cross_session_question": "What was designated as the archive bucket in the earlier "
        "discussion?",
        "paraphrase_planted": "All quarterly snapshots migrate to storage target {value}.",
        "paraphrase_question": "Where are dormant historical backups preserved remotely?",
        "distractors": [
            "User facing static assets reside in nova-static-assets-us.",
            "Raw application audit logs stream to nova-audit-logs-eu.",
            "Nightly database dumps are staged in nova-db-backups-daily.",
        ],
        "distractor_values": [
            "nova-static-assets-us",
            "nova-audit-logs-eu",
            "nova-db-backups-daily",
        ],
    },
    {
        "subject": "auth_token_format",
        "label": "auth token format",
        "fact_value": "Paseto v4.local",
        "old_value": "JWT-RS256",
        "wrong_value": "OAuth1.0a",
        "planted_template": "The identity team confirmed the auth token format is {value}.",
        "old_template": "We previously decided the auth token format would be {value}.",
        "reversed_template": "Correction: we updated security requirements; the auth token "
        "format is now {value}.",
        "question": "What is the auth token format?",
        "current_question": "What is the current auth token format?",
        "cross_session_question": "What was confirmed as the auth token format in the earlier "
        "discussion?",
        "paraphrase_planted": "Credential issuers sign stateless session credentials using "
        "{value}.",
        "paraphrase_question": "Which cryptographic token scheme verifies client authorization "
        "claims?",
        "distractors": [
            "The session cookie name is standardized as __Secure-NovaSession.",
            "Client developer API keys use the prefix sk_live_9942a.",
            "Enterprise single sign-on integrates with Okta Identity Engine.",
        ],
        "distractor_values": ["__Secure-NovaSession", "sk_live_9942a", "Okta Identity Engine"],
    },
    {
        "subject": "api_rate_limit",
        "label": "API rate limit",
        "fact_value": "750 requests per minute",
        "old_value": "300 requests per minute",
        "wrong_value": "50 requests per minute",
        "planted_template": "The gateway config sets the API rate limit to {value}.",
        "old_template": "We initially capped the API rate limit at {value}.",
        "reversed_template": "Correction: we adjusted gateway capacity; the API rate limit is "
        "now {value}.",
        "question": "What is the API rate limit?",
        "current_question": "What is the current API rate limit?",
        "cross_session_question": "What was set as the API rate limit in the earlier discussion?",
        "paraphrase_planted": "Edge proxy throttles unmetered client throughput at {value}.",
        "paraphrase_question": "How many inbound requests may callers dispatch each sixty seconds?",
        "distractors": [
            "Burst traffic tolerance allows up to 1200 requests per second.",
            "File upload endpoint throughput is limited to 50 megabytes per minute.",
            "Automated web scraper crawling is restricted to 30 requests per minute.",
        ],
        "distractor_values": [
            "1200 requests per second",
            "50 megabytes per minute",
            "30 requests per minute",
        ],
    },
    {
        "subject": "downstream_timeout",
        "label": "downstream timeout",
        "fact_value": "850 milliseconds",
        "old_value": "1500 milliseconds",
        "wrong_value": "100 milliseconds",
        "planted_template": "The client config establishes that the downstream timeout is {value}.",
        "old_template": "We initially agreed the downstream timeout is {value}.",
        "reversed_template": "Correction: we revised the latency budget, so the downstream "
        "timeout is now {value}.",
        "question": "What is the downstream timeout?",
        "current_question": "What is the current downstream timeout?",
        "cross_session_question": "What was established as the downstream timeout in the "
        "earlier discussion?",
        "paraphrase_planted": "Socket callers abort unanswered remote procedure calls after "
        "{value}.",
        "paraphrase_question": "What duration threshold triggers an egress request deadline "
        "cancellation?",
        "distractors": [
            "The initial TCP handshake connection timeout is 150 milliseconds.",
            "The top-level edge gateway timeout is set to 5000 milliseconds.",
            "Persistent HTTP keep-alive connection idle timeout is 30000 milliseconds.",
        ],
        "distractor_values": [
            "150 milliseconds",
            "5000 milliseconds",
            "30000 milliseconds",
        ],
    },
]

UNANSWERABLE_DOMAINS: list[dict] = [
    {
        "subject": "compliance_framework",
        "question": "What compliance framework did the team choose for auditing?",
    },
    {
        "subject": "disaster_recovery_rto",
        "question": "What is the recovery time objective for disaster recovery?",
    },
    {
        "subject": "backup_encryption_key",
        "question": "What KMS key alias encrypts the cold storage backups?",
    },
]

SCENARIOS: list[Scenario] = [
    Scenario.BURIED_FACT,
    Scenario.DECISION_REVERSED,
    Scenario.UNANSWERABLE,
    Scenario.CONTRADICTING_DRAFT,
    Scenario.PARAPHRASED,
    Scenario.CROSS_SESSION,
    Scenario.VALID_ANSWER,
]


def get_split_by_hash(case_id: str) -> Split:
    """Deterministically determine dev or test partition by SHA-256 hash parity."""
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return Split.DEV if int(digest, 16) % 2 == 0 else Split.TEST


def validate_case(case: Case) -> None:
    """Validate a single benchmark case against consistency rules."""
    expected_split = get_split_by_hash(case.id)
    if case.split != expected_split:
        raise ValueError(
            f"Case {case.id} split mismatch: got {case.split}, expected {expected_split} from hash"
        )

    msg_ids: set[str] = set()
    all_msgs: list[Message] = []
    for session in case.sessions:
        for msg in session:
            if msg.id in msg_ids:
                raise ValueError(f"Duplicate message id {msg.id} in case {case.id}")
            msg_ids.add(msg.id)
            all_msgs.append(msg)

    if case.expected.source_msg_id is not None and case.expected.source_msg_id not in msg_ids:
        raise ValueError(f"source_msg_id {case.expected.source_msg_id} not found in case {case.id}")

    if case.expected.key_fact is not None:
        key_fact_lower = case.expected.key_fact.lower()
        containing_msg_ids = [m.id for m in all_msgs if key_fact_lower in m.text.lower()]
        if not containing_msg_ids:
            raise ValueError(
                f"key_fact '{case.expected.key_fact}' does not appear in any message for case "
                f"{case.id}"
            )
        if case.expected.source_msg_id is not None:
            if case.expected.source_msg_id not in containing_msg_ids:
                raise ValueError(
                    f"key_fact '{case.expected.key_fact}' missing from source_msg_id "
                    f"{case.expected.source_msg_id}"
                )
            for mid in containing_msg_ids:
                if mid != case.expected.source_msg_id and not mid.startswith("planted_"):
                    raise ValueError(
                        f"key_fact '{case.expected.key_fact}' leaked in message {mid} for case "
                        f"{case.id}"
                    )

    expected_dropped = compute_context_dropped(case.sessions, case.expected.source_msg_id)
    if case.context_dropped != expected_dropped:
        raise ValueError(
            f"Case {case.id} context_dropped={case.context_dropped} but source message is "
            f"{'outside' if expected_dropped else 'inside'} the baseline context window"
        )


def validate_cases(cases: list[Case]) -> None:
    """Validate a collection of benchmark cases."""
    seen_case_ids: set[str] = set()
    dev_count = 0
    test_count = 0

    for case in cases:
        if case.id in seen_case_ids:
            raise ValueError(f"Duplicate case id: {case.id}")
        seen_case_ids.add(case.id)
        validate_case(case)
        if case.split == Split.DEV:
            dev_count += 1
        elif case.split == Split.TEST:
            test_count += 1

    if len(cases) % 2 == 0:
        if dev_count != test_count:
            raise ValueError(
                f"Dev/test split is not 50/50: {dev_count} dev, {test_count} test out of "
                f"{len(cases)} cases"
            )
    elif abs(dev_count - test_count) > 1:
        raise ValueError(
            f"Dev/test split is not 50/50: {dev_count} dev, {test_count} test out of "
            f"{len(cases)} cases"
        )


def _make_msg(
    msg_id: str,
    session_id: str,
    text: str,
    timestamp: datetime,
    role: str = "user",
) -> Message:
    """Helper to create a conversation message turn."""
    return Message(
        id=msg_id,
        session_id=session_id,
        role=role,
        text=text,
        timestamp=timestamp,
    )


def generate(
    seed: int,
    n: int,
    output_dir: Path | str | None = None,
) -> list[Case]:
    """Generate reproducible benchmark evaluation cases."""
    rng = random.Random(seed)
    cases: list[Case] = []
    base_time = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    domain_rng = random.Random(seed * 1_000_003 + 17)
    domain_orders = [domain_rng.sample(range(len(DOMAINS)), len(DOMAINS)) for _ in SCENARIOS]

    for i in range(n):
        target_split = Split.DEV if i % 2 == 0 else Split.TEST
        salt = 0
        while True:
            candidate_id = f"case_{seed}_{i:04d}_{salt}"
            if get_split_by_hash(candidate_id) == target_split:
                case_id = candidate_id
                break
            salt += 1

        scenario = SCENARIOS[i % len(SCENARIOS)]
        round_idx = i // len(SCENARIOS)
        order = domain_orders[i % len(SCENARIOS)]
        domain = DOMAINS[order[round_idx % len(DOMAINS)]]

        t_cursor = base_time + timedelta(hours=i * 24)

        if scenario == Scenario.UNANSWERABLE:
            unans = UNANSWERABLE_DOMAINS[i % len(UNANSWERABLE_DOMAINS)]
            case = build_unanswerable(case_id, target_split, rng, unans, domain, t_cursor)
        else:
            builder = SCENARIO_BUILDERS[scenario]
            case = builder(case_id, target_split, rng, domain, t_cursor)

        case = case.model_copy(update={"domain": domain["subject"]})
        validate_case(case)
        cases.append(case)

    validate_cases(cases)

    target_dir = Path(output_dir) if output_dir is not None else Path("benchmark/cases")
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"{seed}.jsonl"

    with open(out_file, "w", encoding="utf-8") as f:
        for case in cases:
            f.write(case.model_dump_json() + "\n")

    return cases
