"""Find lexicon roots in normalized text with three methods, recorded per match:

1. regex    - optional prefix + root, repeated letters, junk between letters
2. skeleton - dropped vowels ("mjng" for "mjinga"); roots with 3+ consonants only
3. fuzzy    - rapidfuzz Levenshtein on the root part of a token (prefix must be exact),
              threshold scaled by root length

Allowlisted words never match. @mentions and URLs are skipped. Context-dependent roots
(animal words) count as hits only near a second-person cue ("wewe ni mbwa").
"""

import re
from dataclasses import dataclass, field

from rapidfuzz.distance import Levenshtein

from collector.nlp.lexicon import VOWELS, Lexicon, Root, load_lexicon, skeleton
from collector.nlp.normalize import is_protected

METHOD_CONFIDENCE = {"regex": 1.0, "skeleton": 0.9, "fuzzy": 0.8}
UNCUED_CONTEXT_FACTOR = 0.5  # context-dependent root with no 2nd-person cue nearby
CUE_WINDOW = 2
_LETTERS = re.compile(r"[a-z]+")


def max_edits(length: int) -> int:
    """Length-scaled fuzzy threshold: short words must match exactly."""
    if length < 5:
        return 0
    if length < 8:
        return 1
    return 2


@dataclass(frozen=True)
class Match:
    root: str
    method: str
    token: str
    token_index: int
    severity: str
    context_dependent: bool
    cued: bool
    score: float

    @property
    def is_hit(self) -> bool:
        return not self.context_dependent or self.cued


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)

    @property
    def hits(self) -> list[Match]:
        return [m for m in self.matches if m.is_hit]

    @property
    def is_hit(self) -> bool:
        return bool(self.hits)

    @property
    def matched_roots(self) -> list[str]:
        return sorted({m.root for m in self.matches})

    @property
    def match_methods(self) -> list[str]:
        return sorted({m.method for m in self.matches})

    @property
    def score(self) -> float:
        return max((m.score for m in self.matches), default=0.0)


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _skeleton_match(token: str, form: str) -> bool:
    if len(token) < 3 or len(token) >= len(form) or token[0] != form[0]:
        return False
    vowels_dropped = sum(c in VOWELS for c in token) < sum(c in VOWELS for c in form)
    return vowels_dropped and skeleton(token) == skeleton(form) and _is_subsequence(token, form)


def _fuzzy_match(token: str, root: Root) -> bool:
    """Typos are allowed in the root only; the prefix must match exactly.

    Scoring the whole form lets the prefix absorb the edit budget: "mahaba" (love) is two
    edits from "makahaba", "mamako" (your mother) one from "matako".
    """
    limit = max_edits(len(root.root))
    if limit == 0:
        return False
    for prefix in root.prefixes:
        if not token.startswith(prefix):
            continue
        rest = token[len(prefix) :]
        if not rest or abs(len(rest) - len(root.root)) > limit:
            continue
        if not prefix and rest[0] != root.root[0]:
            continue  # a bare form is anchored by its first letter instead
        if 0 < Levenshtein.distance(rest, root.root, score_cutoff=limit) <= limit:
            return True
    return False


def _match_root(root: Root, raw_token: str, words: list[str]) -> tuple[str, str] | None:
    """Return (method, matched text) for the first method that fires."""
    m = root.pattern.search(raw_token)
    if m:
        return "regex", m.group(0)
    for word in words:
        if root.skeleton_ok and any(_skeleton_match(word, f) for f in root.forms):
            return "skeleton", word
    for word in words:
        if _fuzzy_match(word, root):
            return "fuzzy", word
    return None


def match(text_norm: str, lexicon: Lexicon | None = None) -> MatchResult:
    lex = lexicon or load_lexicon()
    tokens = text_norm.split()
    # Letter-only words per whitespace token, for cue detection and fuzzy/skeleton.
    words_per_token = [[] if is_protected(t) else _LETTERS.findall(t) for t in tokens]
    cue_positions = {
        i for i, words in enumerate(words_per_token) if any(lex.is_cue(w) for w in words)
    }

    result = MatchResult()
    for i, token in enumerate(tokens):
        words = [w for w in words_per_token[i] if w not in lex.allowlist]
        if not words:  # protected, punctuation-only, or allowlisted
            continue
        for root in lex.roots:
            found = _match_root(root, token, words)
            if found is None:
                continue
            method, text = found
            cued = any(abs(i - j) <= CUE_WINDOW for j in cue_positions if j != i)
            score = root.weight * METHOD_CONFIDENCE[method]
            if root.context_dependent and not cued:
                score *= UNCUED_CONTEXT_FACTOR
            result.matches.append(
                Match(
                    root=root.root,
                    method=method,
                    token=text,
                    token_index=i,
                    severity=root.severity,
                    context_dependent=root.context_dependent,
                    cued=cued,
                    score=round(score, 3),
                )
            )
    return result
