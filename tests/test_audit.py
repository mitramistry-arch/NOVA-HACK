"""Tests for the audit log."""

from datetime import UTC, datetime, timedelta

import pytest

from nova.audit.logger import MAX_TEXT, AuditError, AuditLog, AuditRecord, timed
from nova.core.schemas import DecisionAction, Verdict, VerdictLabel

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def rec(i: int = 0, **kw) -> AuditRecord:
    return AuditRecord(id=f"r{i}", timestamp=T0 + timedelta(minutes=i), **kw)


def test_roundtrip():
    log = AuditLog(":memory:")
    r = rec(
        question="q",
        answer="a",
        retrieved_ids=["m1", "m2"],
        verdicts=[
            Verdict(claim="c", label=VerdictLabel.OUTDATED, memory_ids=["m1"], rationale="x")
        ],
        decision_action=DecisionAction.CORRECT,
        latency_ms=12.5,
        prompt_tokens=7,
        completion_tokens=3,
    )
    assert log.write(r) == "r0"
    assert log.get("r0") == r
    assert log.get("missing") is None


def test_default_id_and_timestamp():
    log = AuditLog(":memory:")
    rid = log.write(AuditRecord())
    assert log.get(rid).id == rid


def test_truncation():
    log = AuditLog(":memory:")
    long = "x" * (MAX_TEXT + 500)
    v = Verdict(claim=long, label=VerdictLabel.SUPPORTED, rationale=long)
    log.write(rec(question=long, answer=long, verdicts=[v]))
    got = log.get("r0")
    assert len(got.question) == len(got.answer) == MAX_TEXT
    assert len(got.verdicts[0].claim) == len(got.verdicts[0].rationale) == MAX_TEXT


def test_api_keys_redacted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    log = AuditLog(":memory:")
    log.write(rec(question="use sk-secret now"))
    assert "sk-secret" not in log.get("r0").question


def test_recent_ordering():
    log = AuditLog(":memory:")
    for i in (1, 0, 2):
        log.write(rec(i))
    assert [r.id for r in log.recent(2)] == ["r2", "r1"]
    assert [r.id for r in log.recent()] == ["r2", "r1", "r0"]
    assert log.recent(0) == []


def test_default_path_from_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVA_DB_PATH", str(tmp_path / "n.db"))
    AuditLog().write(rec())
    assert AuditLog().get("r0") is not None


def test_errors(tmp_path):
    with pytest.raises(AuditError):
        AuditLog(tmp_path / "no" / "dir" / "x.db")
    log = AuditLog(":memory:")
    log.write(rec())
    with pytest.raises(AuditError):
        log.write(rec())
    log.conn.close()
    with pytest.raises(AuditError):
        log.recent()


def test_timed():
    with timed() as t:
        pass
    assert t.elapsed_ms >= 0.0
