"""Request size limits enforced at the API boundary."""

from typing import Annotated

from pydantic import StringConstraints

from nova.core.schemas import Identifier

MAX_MESSAGES = 200
MAX_QUESTION_CHARS = 2_000
MAX_ANSWER_CHARS = 10_000

Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]
Answer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANSWER_CHARS)
]

__all__ = [
    "MAX_ANSWER_CHARS",
    "MAX_MESSAGES",
    "MAX_QUESTION_CHARS",
    "Answer",
    "Identifier",
    "Question",
]
