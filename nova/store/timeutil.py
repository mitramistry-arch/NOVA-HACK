"""Canonical UTC timestamp encoding so lexicographic order equals chronological order."""

from datetime import UTC, datetime


def format_ts(value: datetime) -> str:
    """Encode an aware datetime as a fixed-width UTC ISO string with microseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive timestamps are not accepted")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_ts(text: str) -> datetime:
    """Decode a stored timestamp; legacy naive values are interpreted as UTC."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
