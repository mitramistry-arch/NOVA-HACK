"""Deterministic checks that a source quote really supports an extracted memory.

A verbatim quote is necessary but not sufficient: the quote may be a question, a proposal, a
negation of the claimed value, or an instruction injected into the conversation. These checks
are heuristics over English text; they err toward rejecting, and every rejection has a reason.
"""

import re

from nova.core.schemas import Memory, Message
from nova.core.text import STOPWORDS, content_tokens, normalize, stem

ALLOWED_ROLES = frozenset({"user", "assistant"})
MAX_SUBJECT = 200

_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instructions?|prompts?|messages?|rules?|context)"
    r"|disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|system|"
    r"instructions?|rules?)"
    r"|you\s+are\s+now\b|new\s+instructions?\s*:|override\s+(?:the\s+)?(?:system|safety|rules?)"
    r"|<\s*/?\s*(?:system|assistant|instructions?|tool)\s*>"
    r"|(?:^|\n)\s*(?:system|assistant|developer)\s*:|\bjailbreak\b|\bdo\s+anything\s+now\b"
    r"|(?:output|return|emit|respond\s+with)\s+(?:the\s+)?following\s+(?:json|memor)"
    r"|\b(?:store|save|record|remember)\s+(?:this\s+)?(?:as\s+)?(?:a\s+)?(?:new\s+)?"
    r"(?:memory|memories)\s*[:=]",
    re.IGNORECASE,
)
_INTERROGATIVE = re.compile(
    r"^(?:should|shall|can|could|would|do|does|did|will)\s+(?:we|i|you|they|it|he|she)\b"
)
_HEDGE = re.compile(
    r"\b(?:maybe|perhaps|possibly|probably|might|may be|not sure|unsure|undecided|"
    r"not decided|tentative(?:ly)?|propos(?:e|ed|al|ing|als)|suggest(?:ed|s|ion|ions|ing)?|"
    r"consider(?:ing|ed)?|thinking (?:about|of)|what if|how about|what about|should we|"
    r"could we|can we|shall we|if we|whether|i wonder|we could|could use|would be nice|"
    r"evaluat(?:e|ing)|explor(?:e|ing)|leaning (?:toward|towards)|i think|i guess|i suppose|"
    r"brainstorm\w*|hypothetical(?:ly)?)\b"
)
_FINAL = re.compile(
    r"\b(?:decided|agreed|final(?:ly|ized|ised)?|confirmed|approved|settled|chose|chosen|"
    r"picked|selected|committed|going with|go with|will use|will be|we'll|official(?:ly)?|"
    r"from now on|resolved|concluded|ruled)\b"
)
_CLAUSE_SPLIT = re.compile(
    r"[,;:()\u2014\u2013]|\s-\s|\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\bwhile\b|"
    r"\bwhereas\b|\band then\b"
)
_PRE_NEGATOR = re.compile(
    r"(?:\b(?:not|never|no longer|cannot|can't|won't|don't|doesn't|didn't|isn't|aren't|"
    r"shouldn't|wouldn't|without|avoid|avoiding)\b)(?:\s+[a-z0-9']+){0,3}\s*$"
)
_PRE_REPLACED = re.compile(
    r"\b(?:stop(?:ped)? using|drop(?:ped)?|dropping|ditch(?:ed)?|remov(?:e|ed|ing)|"
    r"abandon(?:ed)?|deprecat(?:e|ed)|replac(?:e|ed|ing)|instead of|rather than|"
    r"switch(?:ed)? (?:away )?from|migrat(?:e|ed|ing) (?:away )?from|"
    r"mov(?:e|ed|ing) (?:away )?from|away from|out of)\b((?:\s+[a-z0-9']+){0,3})\s*$"
)
_REPLACED_BREAKERS = frozenset(
    ["with", "by", "for", "over", "to", "and", "but", "then", "into", "in"]
)
_POST_NEGATOR = re.compile(
    r"^\s*(?:(?:is|are|was|were|will be|would be|be|been|has been|have been)\s+"
    r"(?:not|never|no longer|deprecated|dropped|removed|abandoned|out|rejected|ruled out|"
    r"off the table)\b|(?:isn't|aren't|wasn't|weren't|won't|wouldn't|shouldn't|doesn't|"
    r"didn't|don't|can't|cannot)\b|no longer\b)"
)
_ANY_NEGATOR = re.compile(r"\b(?:not|never|no longer|cannot|without)\b|n't\b")
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


def looks_like_instruction(text: str) -> bool:
    """Return True when text resembles an attempt to instruct the model or forge roles."""
    return _INJECTION.search(text) is not None


def _sentence_around(norm_text: str, norm_quote: str) -> str:
    """Return the sentence(s) of norm_text that contain norm_quote."""
    idx = norm_text.find(norm_quote)
    if idx < 0:
        return norm_quote
    start = 0
    for m in _SENTENCE_END.finditer(norm_text, 0, idx):
        start = m.end()
    end_idx = idx + len(norm_quote)
    tail = _SENTENCE_END.search(norm_text, max(idx, end_idx - 1))
    end = tail.end() if tail else len(norm_text)
    return norm_text[start:end].strip()


def _is_question(sentence: str, quote: str) -> bool:
    """Return True when the quote or its sentence is a question."""
    if quote.rstrip().endswith("?") or sentence.rstrip().endswith("?"):
        return True
    return _INTERROGATIVE.match(quote) is not None or _INTERROGATIVE.match(sentence) is not None


def _value_clause(sentence: str, value_tokens: set[str]) -> str:
    """Return the clause of sentence that overlaps most with the value tokens."""
    best, best_hits = sentence, -1
    for clause in _CLAUSE_SPLIT.split(sentence):
        hits = len(content_tokens(clause) & value_tokens)
        if hits > best_hits:
            best, best_hits = clause.strip(), hits
    return best


def _locate(clause: str, norm_value: str, tokens: set[str]) -> tuple[int, int] | None:
    """Return the (start, end) offsets of the value inside clause, if it can be located."""
    exact = re.search(rf"(?<![a-z0-9]){re.escape(norm_value)}(?![a-z0-9])", clause)
    if exact:
        return exact.start(), exact.end()
    spans = [
        m.span()
        for m in re.finditer(r"[a-z0-9]+", clause)
        if stem(m.group()) in tokens and m.group() not in STOPWORDS
    ]
    return (spans[0][0], spans[-1][1]) if spans else None


def _is_negated(clause: str, norm_value: str, tokens: set[str]) -> bool:
    """Return True when the clause negates, replaces, or rules out the value."""
    span = _locate(clause, norm_value, tokens)
    if span is None:
        return _ANY_NEGATOR.search(clause) is not None
    pre, post = clause[: span[0]], clause[span[1] :]
    if _PRE_NEGATOR.search(pre) or _POST_NEGATOR.match(post):
        return True
    replaced = _PRE_REPLACED.search(pre)
    if replaced is not None:
        between = set(replaced.group(1).split())
        return not (between & _REPLACED_BREAKERS)
    return False


def rejection_reason(memory: Memory, message: Message | None) -> str | None:
    """Return why the memory is not supported by its source message, or None if it is."""
    if message is None:
        return "source message not found"
    if message.role.strip().lower() not in ALLOWED_ROLES:
        return "source message role is not trusted for extraction"
    quote = memory.source_quote.strip()
    if not quote:
        return "empty source quote"
    subject = memory.subject.strip()
    if not subject or len(subject) > MAX_SUBJECT or "\n" in subject:
        return "invalid subject"
    norm_text, norm_quote = normalize(message.text), normalize(quote)
    if norm_quote not in norm_text:
        return "quote is not a verbatim span of the source message"
    sentence = _sentence_around(norm_text, norm_quote)
    if looks_like_instruction(message.text):
        return "source message contains instruction-like text"
    if _is_question(sentence, norm_quote):
        return "source text is a question"
    tokens = content_tokens(memory.value)
    if not tokens:
        return "value has no content"
    if not tokens <= content_tokens(quote):
        return "value is not stated in the quote"
    clause = _value_clause(sentence, tokens)
    if _HEDGE.search(clause) and not _FINAL.search(clause):
        return "source text is a proposal or hypothetical, not a finalized statement"
    if _is_negated(clause, normalize(memory.value), tokens):
        return "source text negates or replaces the value"
    return None
