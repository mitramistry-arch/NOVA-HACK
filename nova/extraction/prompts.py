"""Prompts for memory extraction and repair."""

EXTRACTION_PROMPT = """Extract durable memories from the conversation turns.
Return a strict JSON list of objects with the following schema:
[
  {
    "type": "fact" | "decision" | "correction" | "preference" | "commitment",
    "subject": "normalized_snake_case_key",
    "value": "clear statement of the memory",
    "reason": "optional rationale or null",
    "source_msg_id": "exact id of the source message",
    "source_quote": "exact verbatim substring from that message",
    "confidence": 1.0
  }
]
Rules:
- Extract only facts, decisions, corrections, preferences, commitments.
- Never extract speculative, hypothetical, or temporary remarks.
- subject must be lowercase snake_case (e.g. project_database).
- source_quote must appear verbatim in the source message.
- Return ONLY valid JSON, no markdown formatting or commentary."""

REPAIR_PROMPT = """The previous output was invalid JSON. Return strictly a valid JSON array matching the requested schema with no commentary or markdown fences."""
