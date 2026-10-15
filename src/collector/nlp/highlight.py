"""Find where in the ORIGINAL comment text the words behind a lexicon hit are.

The matcher works on normalized text ("M.J.I.N.G.A" -> "mjinga", "m j i n g a" -> "mjinga"),
so its matches can't be located in the raw text directly. Instead the raw text is cut into
word tokens (letters, digits and leet stand-ins, so "mumeo,mkundu" is two tokens), each
normalized on its own, and the token(s) whose normalization contains a hit word are the
ones to mark. Spaced or dotted words ("m j i n g a", "M.J.I.N.G.A") span several tokens, so
if a hit word is in no single token, the smallest run of neighbouring tokens containing it
is marked. Mentions and links are never marked.
"""

import re

from collector.nlp.matcher import MatchResult, match
from collector.nlp.normalize import is_protected, normalize, normalize_plain

MAX_RUN = 15  # tokens a spaced-out word may span ("m j i n g a" is 6)
_LEET_CHARS = "@$*"  # stand-ins for letters ("m@l@y@", "f*ck"); "!" ends sentences
_CHUNK = re.compile(r"\S+")
_WORD = re.compile(rf"(?:[^\W_]|[{re.escape(_LEET_CHARS)}])+")


def _tokens(raw: str) -> list[tuple[int, int]]:
    """(start, end) of each word token, skipping @mentions and links."""
    return [
        (chunk.start() + w.start(), chunk.start() + w.end())
        for chunk in _CHUNK.finditer(raw)
        if not is_protected(chunk.group())
        for w in _WORD.finditer(chunk.group())
    ]


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


def _hit_runs(raw: str, result: MatchResult | None) -> list[tuple[int, int, str]]:
    """(start, end, root) of each marked word, in text order."""
    if result is None:
        result = match(normalize(raw), text_plain=normalize_plain(raw))
    roots: dict[str, str] = {}  # matched text -> root
    places: dict[str, set[int]] = {}  # matched text -> normalized token positions
    for m in result.hits:
        roots.setdefault(m.token, m.root)
        places.setdefault(m.token, set()).add(m.token_index)
    if not roots:
        return []
    tokens = _tokens(raw)
    runs: dict[int, int] = {}  # first token -> last token of each marked word
    run_roots: dict[int, str] = {}
    for word, root in roots.items():
        found = 0
        for i, (start, end) in enumerate(tokens):
            if _contains(raw[start:end], word):
                runs.setdefault(i, i)
                run_roots.setdefault(i, root)
                found += 1
        # Occurrences not inside one token are spaced or dotted ("M.J.I.N.G.A"): look for
        # the smallest runs of neighbouring tokens holding them.
        for size in range(2, MAX_RUN + 1):
            if found >= len(places[word]):
                break
            for i in range(len(tokens) - size + 1):
                taken = any(a <= j <= b for a, b in runs.items() for j in range(i, i + size))
                if not taken and _contains(raw[tokens[i][0] : tokens[i + size - 1][1]], word):
                    runs[i] = i + size - 1
                    run_roots[i] = root
                    found += 1
    return [
        (*_trim(raw, tokens[i][0], tokens[j][1]), run_roots[i]) for i, j in sorted(runs.items())
    ]


def hit_spans(raw: str, result: MatchResult | None = None) -> list[tuple[int, int]]:
    """(start, end) character spans in `raw` of the words that make it a lexicon hit.

    `result` is the matcher's result for `raw`, if the caller already has it.
    """
    return [(start, end) for start, end, _ in _hit_runs(raw, result)]


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


def hit_words(raw: str, result: MatchResult | None = None) -> list[str]:
    """The hit words exactly as written in the comment."""
    return list(dict.fromkeys(raw[s:e] for s, e in hit_spans(raw, result)))


def hit_terms(raw: str, result: MatchResult | None = None) -> dict[str, str]:
    """Each distinct hit word, lowercased, mapped to the root it matched."""
    terms: dict[str, str] = {}
    for start, end, root in _hit_runs(raw, result):
        word = raw[start:end].lower()
        if word:
            terms.setdefault(word, root)
    return terms


KEY_SEP = "\x1f"  # never part of a word


def hit_key(raw: str, result: MatchResult | None = None) -> str:
    """The comment's distinct hit words, sorted and joined: equal for two comments with the
    same hit words ("wewe ujinga" and "ujinga mtupu"). Empty when there are none."""
    return KEY_SEP.join(sorted(hit_terms(raw, result)))
