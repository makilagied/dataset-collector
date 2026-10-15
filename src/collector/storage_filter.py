"""Which comments get stored, and how much of each. Applied by every way comments come in
(API backfill, webhooks, scraping), before anything is written.

With STORE_ONLY_HITS (the default) only lexicon hits are stored: comments with a Swahili or
English abusive word scoring >= HIT_THRESHOLD, aimed at someone when the word is only
abusive in context.

Comments longer than MAX_TEXT characters (posts pasted as comments, essays) are not kept
whole: only an excerpt of at most EXCERPT_CHARS characters around the first hit word is
stored, cut at word boundaries. A long comment with no hit word is dropped.
"""

import re
from typing import Any

from collector.config import get_settings
from collector.nlp.highlight import hit_spans
from collector.nlp.matcher import MatchResult, match
from collector.nlp.normalize import normalize, normalize_plain
from collector.pipeline.sample import HIT_THRESHOLD

MAX_TEXT = 200
EXCERPT_CHARS = 150
_WORD = re.compile(r"\S+")


def _strong_hits(text: str) -> MatchResult:
    result = match(normalize(text), text_plain=normalize_plain(text))
    return MatchResult([m for m in result.hits if m.score >= HIT_THRESHOLD])


def is_hit(text: str) -> bool:
    return _strong_hits(text).is_hit


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str | None:
    """At most `limit` characters of `text` around its first hit word, grown a whole word
    at a time on each side in turn. None if there is no hit word."""
    spans = hit_spans(text, _strong_hits(text))
    if not spans:
        return None
    start, end = spans[0]
    if end - start >= limit:
        return text[start : start + limit]
    words = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    left = [w for w in words if w[1] <= start][::-1]  # nearest first
    right = [w for w in words if w[0] >= end]
    lo, hi = start, end
    while left or right:
        grew = False
        if left and hi - left[0][0] <= limit:
            lo = left.pop(0)[0]
            grew = True
        if right and right[0][1] - lo <= limit:
            hi = right.pop(0)[1]
            grew = True
        if not grew:
            break
    return text[lo:hi]


def text_to_store(text: str) -> str | None:
    """The text to store for a comment (whole, or an excerpt if long), or None to drop it."""
    if get_settings().store_only_hits and not is_hit(text):
        return None
    if len(text) <= MAX_TEXT:
        return text
    return excerpt(text)


def prepare(item: dict[str, Any]) -> dict[str, Any] | None:
    """The comment payload to store, with its text cut down if long; None to drop it."""
    text = item.get("text") or ""
    stored = text_to_store(text)
    if stored is None:
        return None
    if stored == text:
        return item
    return {**item, "text": stored, "original_text_length": len(text)}


def should_store(text: str) -> bool:
    return text_to_store(text) is not None
