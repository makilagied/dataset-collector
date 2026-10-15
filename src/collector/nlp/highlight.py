"""Find where in the ORIGINAL comment text the words behind a lexicon hit are.

The matcher works on normalized text ("M.J.I.N.G.A" -> "mjinga", "m j i n g a" -> "mjinga"),
so its matches can't be located in the raw text directly. Instead each whitespace token of
the raw text is normalized on its own, and the token(s) whose normalization contains a hit
word are the ones to mark. Spaced-out words span several tokens, so if a hit word is in no
single token, the smallest run of neighbouring tokens that contains it is marked.
"""

import re

from collector.nlp.matcher import match
from collector.nlp.normalize import is_protected, normalize, normalize_plain

MAX_RUN = 15  # tokens a spaced-out word may span ("m j i n g a" is 6)
_TOKEN = re.compile(r"\S+")
_LEET_CHARS = "@$*"  # stand-ins for letters ("m@l@y@", "f*ck"); "!" ends sentences


def _contains(text: str, word: str) -> bool:
    pattern = rf"(?<![a-z]){re.escape(word)}(?![a-z])"
    return any(re.search(pattern, form) for form in (normalize(text), normalize_plain(text)))


def _keep(ch: str) -> bool:
    return ch.isalnum() or ch in _LEET_CHARS


def _trim(raw: str, start: int, end: int) -> tuple[int, int]:
    """Drop punctuation and emoji around a word ("mbwa," -> "mbwa"), not leet ("m@l@y@")."""
    while start < end and not _keep(raw[start]):
        start += 1
    while end > start and not _keep(raw[end - 1]):
        end -= 1
    return start, end


def hit_spans(raw: str) -> list[tuple[int, int]]:
    """(start, end) character spans in `raw` of the words that make it a lexicon hit."""
    result = match(normalize(raw), text_plain=normalize_plain(raw))
    words = list(dict.fromkeys(m.token for m in result.hits))
    if not words:
        return []
    tokens = [(m.start(), m.end()) for m in _TOKEN.finditer(raw)]
    runs: dict[int, int] = {}  # first token -> last token of each marked word
    for word in words:
        found = False
        for i, (start, end) in enumerate(tokens):
            text = raw[start:end]
            if not is_protected(text) and _contains(text, word):
                runs.setdefault(i, i)
                found = True
        for size in range(2, MAX_RUN + 1):
            if found:
                break
            for i in range(len(tokens) - size + 1):
                taken = any(a <= j <= b for a, b in runs.items() for j in range(i, i + size))
                if not taken and _contains(raw[tokens[i][0] : tokens[i + size - 1][1]], word):
                    runs[i] = i + size - 1
                    found = True
    return [_trim(raw, tokens[i][0], tokens[j][1]) for i, j in sorted(runs.items())]


def segments(raw: str) -> list[tuple[str, bool]]:
    """The text cut into (piece, is_hit_word) pieces, for display."""
    out: list[tuple[str, bool]] = []
    pos = 0
    for start, end in hit_spans(raw):
        if start > pos:
            out.append((raw[pos:start], False))
        out.append((raw[start:end], True))
        pos = end
    if pos < len(raw) or not out:
        out.append((raw[pos:], False))
    return out


def hit_words(raw: str) -> list[str]:
    """The hit words exactly as written in the comment."""
    return list(dict.fromkeys(raw[s:e] for s, e in hit_spans(raw)))
