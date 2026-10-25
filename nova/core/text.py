"""Small deterministic text helpers shared by extraction and verification."""

import re

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "we",
        "our",
        "us",
        "i",
        "my",
        "you",
        "your",
        "it",
        "its",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "and",
        "or",
        "that",
        "this",
        "these",
        "those",
        "will",
        "would",
        "should",
        "shall",
        "can",
        "could",
        "may",
        "might",
        "must",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "use",
        "uses",
        "used",
        "using",
        "with",
        "as",
        "from",
        "so",
        "than",
        "then",
        "there",
        "they",
        "their",
        "them",
        "he",
        "she",
        "his",
        "her",
        "not",
        "no",
        "never",
    ]
)

_ANY_NEGATOR = re.compile(r"\b(?:not|never|no longer|cannot|without)\b|n't\b")


def normalize(text: str) -> str:
    """Lowercase text, unify apostrophes, and collapse whitespace."""
    return " ".join(text.replace("\u2019", "'").replace("\u2018", "'").split()).lower()


def stem(token: str) -> str:
    """Return a crude singular form so plural and singular spellings compare equal."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def content_tokens(text: str) -> set[str]:
    """Return the stemmed non-stopword alphanumeric tokens of text."""
    return {stem(t) for t in re.findall(r"[a-z0-9]+", normalize(text)) if t not in STOPWORDS}


def has_negation(text: str) -> bool:
    """Return True when text contains an explicit negation cue."""
    return _ANY_NEGATOR.search(normalize(text)) is not None
