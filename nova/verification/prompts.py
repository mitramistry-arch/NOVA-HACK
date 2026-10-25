"""Prompts for answer verification."""

SPLIT_PROMPT = """Split the answer into atomic, checkable claims.
Return a strict JSON list of strings, e.g. ["claim one", "claim two"].
Rules:
- Each claim states exactly one verifiable fact and is self-contained.
- Resolve pronouns and references so each claim stands alone.
- Drop opinions, greetings, hedges, and filler.
- Do not add facts that are not in the answer.
- Return at most 8 claims.
- Return ONLY the JSON list, no markdown or commentary."""

SPLIT_REPAIR_PROMPT = """The previous output was invalid. Return a valid JSON list of strings.
Use at most 8 claims, no commentary or markdown fences."""
