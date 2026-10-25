"""Redaction of credentials from free text before it is logged, audited, or returned."""

import re
from collections.abc import Iterable

REDACTED = "[redacted]"

_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{10,}=*"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb|redis)(?:\+\w+)?://[^\s/@]*@"),
    re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token)\s*[=:]\s*\S+"),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and common credential shapes in ``text``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text
