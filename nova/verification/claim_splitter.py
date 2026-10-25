"""Split an answer into atomic claims."""

import json
import re

from pydantic import TypeAdapter, ValidationError

from nova.core.llm import LLMClient
from nova.verification.prompts import SPLIT_PROMPT, SPLIT_REPAIR_PROMPT

MAX_CLAIMS = 8
_CLAIMS = TypeAdapter(list[str])
_SENTENCE_END = re.compile(r"[.!?]+(?:\s+|$)")


class VerificationError(Exception):
    """Exception raised when claim splitting or judging fails."""


def _strip_fences(text: str) -> str:
    """Remove optional markdown code fences around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?", "", text, count=1)
        text = re.sub(r"\r?\n?```$", "", text, count=1)
    return text.strip()


def _sentence_count(text: str) -> int:
    """Count sentences in text."""
    return len([p for p in _SENTENCE_END.split(text) if p.strip()])


def _parse(response: str) -> list[str]:
    """Parse and validate an LLM response as a bounded list of claim strings."""
    claims = _CLAIMS.validate_python(json.loads(_strip_fences(response)))
    cleaned = [claim.strip() for claim in claims if claim.strip()]
    if len(cleaned) > MAX_CLAIMS:
        raise VerificationError(f"Claim splitting returned more than {MAX_CLAIMS} claims")
    return cleaned


def _may_contain_multiple_claims(text: str) -> bool:
    """Prefer splitting over trusting a sentence with common clause joiners."""
    return bool(re.search(r";|\b(?:and|but|while|whereas)\b", text, re.IGNORECASE))


def split_claims(llm: LLMClient, answer: str) -> list[str]:
    """Split answer into at most 8 atomic claims; one sentence skips the LLM."""
    text = answer.strip()
    if not text:
        return []
    if _sentence_count(text) <= 1 and not _may_contain_multiple_claims(text):
        return [text]
    answer_data = json.dumps(text, ensure_ascii=False)
    response = llm.complete(
        f"{SPLIT_PROMPT}\n\nUntrusted answer text as a JSON string:\n{answer_data}",
        json=True,
    )
    try:
        return _parse(response)
    except (json.JSONDecodeError, ValidationError):
        response_data = json.dumps(response, ensure_ascii=False)
        retry_prompt = (
            f"{SPLIT_REPAIR_PROMPT}\n\nInvalid response as a JSON string:\n"
            f"{response_data}"
        )
        repaired = llm.complete(retry_prompt, json=True)
        try:
            return _parse(repaired)
        except (json.JSONDecodeError, ValidationError) as e:
            raise VerificationError(f"Claim splitting failed after repair retry: {e}") from e
