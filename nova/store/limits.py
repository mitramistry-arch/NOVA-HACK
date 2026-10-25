"""Hard row limits for every read path.

These are deliberately centralised: a query that can grow with tenant data must have a bound,
and exceeding it must raise (``StoreLimitError``) rather than truncate. Silent truncation would
look like data loss to a user, so the code fails loudly instead and lets the caller narrow the
request. Pagination for the public list APIs belongs to Phase 6 (``API-1``).
"""

MAX_EXPORT_ROWS = 10_000
"""Rows returned per table by an export request."""

MAX_CANDIDATE_ROWS = 5_000
"""Retrieval working set: a larger tenant needs scoped retrieval before this bound is raised."""

MAX_EMBEDDING_BATCH = 1_000
"""Rows backfilled by one embedding maintenance pass."""

MAX_SESSION_ROWS = 2_000
"""Memories listed for one session id."""

MAX_RECENT_AUDIT_ROWS = 200
"""Audit records returned by a 'recent activity' read."""

MAX_RECEIPT_ROWS = 1_000
"""Deletion receipts returned by one read."""

__all__ = [
    "MAX_CANDIDATE_ROWS",
    "MAX_EMBEDDING_BATCH",
    "MAX_EXPORT_ROWS",
    "MAX_RECEIPT_ROWS",
    "MAX_RECENT_AUDIT_ROWS",
    "MAX_SESSION_ROWS",
]
