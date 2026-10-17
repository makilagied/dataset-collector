"""Load the root lexicons (sw_roots.yaml, en_roots.yaml) and build per-root match patterns.

Each file declares its `language`. Swahili roots are matched against fully normalized text;
English roots against text normalized without the Swahili shorthand step, which would
otherwise rewrite English spellings ("fuck" -> "fusik"). See matcher.match.

Verb roots (`verb: true`) are written in their dictionary form ("tomba"). Any chain of
subject, tense and object prefixes may come before the stem ("nitakutomba"), and the final
"a" is replaced by one of the verb endings ("tombe", "tombwa", "tombana").
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = {"low": 0.4, "medium": 0.7, "high": 1.0}
VOWELS = set("aeiou")
# Up to two non-letter characters may sit between letters ("mj#inga", "mji'nga").
JUNK = r"[^\sa-z]{0,2}"
# Before a verb stem: subject + tense + object markers, e.g. "ni-ta-ku", "wa-me-m".
VERB_PREFIX = r"[a-z]{0,10}"
# Endings that replace a verb's final "a": subjunctive, passive, reciprocal, stative.
VERB_ENDINGS = ("a", "e", "wa", "we", "ana", "ewa", "eka")
# "2" is shorthand for "tu" (just/only) and is often glued on: "kutomba2" normalizes to
# "kutombatu". Swahili roots may carry it.
TU_TAIL = "(?:tu)?"


class LexiconError(ValueError):
    pass


def skeleton(word: str) -> str:
    return "".join(ch for ch in word if ch not in VOWELS)


@dataclass(frozen=True)
class Root:
    root: str
    prefixes: tuple[str, ...]
    severity: str
    context_dependent: bool
    notes: str
    pattern: re.Pattern[str]
    suffixes: tuple[str, ...] = ("",)
    lang: str = "sw"
    fuzzy: bool = True  # allow typo (Levenshtein) matches
    verb: bool = False

    @property
    def stem(self) -> str:
        """What the suffixes attach to: the root, or a verb without its final "a"."""
        return self.root[:-1] if self.verb else self.root

    @property
    def forms(self) -> tuple[str, ...]:
        """Every surface form: prefix + stem + suffix (verbs: with no prefix)."""
        return tuple(p + self.stem + s for p in self.prefixes for s in self.suffixes)

    @property
    def weight(self) -> float:
        return SEVERITIES[self.severity]

    @property
    def skeleton_ok(self) -> bool:
        """Dropped-vowel matching is only safe for roots with 3+ consonants, and not for
        verbs, whose short stems collide with English ("tomb" for "tomba")."""
        return not self.verb and len(skeleton(self.root)) >= 3


@dataclass(frozen=True)
class Lexicon:
    roots: tuple[Root, ...]
    allowlist: frozenset[str]
    cue_words: frozenset[str]
    cue_pattern: re.Pattern[str] | None

    def is_cue(self, token: str) -> bool:
        return token in self.cue_words or bool(self.cue_pattern and self.cue_pattern.match(token))


def _affix(affixes: tuple[str, ...], before: bool) -> str:
    """Regex for an optional (when "" is listed) or required prefix/suffix group."""
    named = sorted((a for a in affixes if a), key=len, reverse=True)
    if not named:
        return ""
    alternatives = "|".join(map(re.escape, named))
    group = f"(?:(?:{alternatives}){JUNK})" if before else f"(?:{JUNK}(?:{alternatives}))"
    return group + ("?" if "" in affixes else "")


def build_pattern(
    root: str,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...] = ("",),
    verb: bool = False,
    tail: str = "",
) -> re.Pattern[str]:
    """Prefix + root + suffix (+ tail); letters may repeat and carry junk between them."""
    body = JUNK.join(f"{re.escape(ch)}+" for ch in root)
    prefix = VERB_PREFIX if verb else _affix(prefixes, before=True)
    suffix = _affix(suffixes, before=False)
    return re.compile(rf"(?<![a-z]){prefix}{body}{suffix}{tail}(?![a-z])")


def _parse_root(entry: Any, index: int, lang: str, defaults: dict[str, Any]) -> Root:
    if not isinstance(entry, dict):
        raise LexiconError(f"roots[{index}] must be a mapping")
    try:
        root = str(entry["root"]).strip().lower()
        severity = str(entry["severity"])
    except KeyError as e:
        raise LexiconError(f"roots[{index}] is missing {e}") from e
    if not re.fullmatch(r"[a-z']+", root):
        raise LexiconError(f"roots[{index}]: root {root!r} must be lowercase letters")
    if severity not in SEVERITIES:
        raise LexiconError(f"roots[{index}]: severity must be one of {sorted(SEVERITIES)}")
    verb = bool(entry.get("verb", False))
    if verb:
        if entry.get("prefixes"):
            raise LexiconError(f"roots[{index}]: a verb takes any prefix; drop 'prefixes'")
        if not root.endswith("a") or len(root) < 3:
            raise LexiconError(f"roots[{index}]: verb {root!r} must end in 'a' (tomba)")
    prefixes = tuple(str(p).lower() for p in entry.get("prefixes") or [""])
    default_suffixes = VERB_ENDINGS if verb else [""]
    suffixes = tuple(str(s).lower() for s in entry.get("suffixes") or default_suffixes)
    stem = root[:-1] if verb else root
    return Root(
        root=root,
        prefixes=prefixes,
        severity=severity,
        context_dependent=bool(entry.get("context_dependent", False)),
        notes=str(entry.get("notes") or ""),
        pattern=build_pattern(stem, prefixes, suffixes, verb, TU_TAIL if lang == "sw" else ""),
        suffixes=suffixes,
        lang=lang,
        # Typo matching scores prefix + root + suffix; verbs have open-ended prefixes.
        fuzzy=not verb and bool(entry.get("fuzzy", defaults.get("fuzzy", True))),
        verb=verb,
    )


def parse_lexicon(raw: dict[str, Any]) -> Lexicon:
    entries = raw.get("roots")
    if not isinstance(entries, list) or not entries:
        raise LexiconError("lexicon needs a non-empty 'roots' list")
    lang = str(raw.get("language") or "sw")
    defaults = raw.get("defaults") or {}
    roots = tuple(_parse_root(e, i, lang, defaults) for i, e in enumerate(entries))
    cues = raw.get("second_person_cues") or {}
    pattern = cues.get("pattern")
    return Lexicon(
        roots=roots,
        allowlist=frozenset(str(w).lower() for w in raw.get("allowlist") or []),
        cue_words=frozenset(str(w).lower() for w in cues.get("words") or []),
        cue_pattern=re.compile(pattern) if pattern else None,
    )


def load_lexicon(path: Path | None = None) -> Lexicon:
    if path is not None:
        return parse_lexicon(yaml.safe_load(path.read_text(encoding="utf-8")))
    return _default_lexicon()


def merge(*lexicons: Lexicon) -> Lexicon:
    patterns = [lex.cue_pattern.pattern for lex in lexicons if lex.cue_pattern]
    return Lexicon(
        roots=tuple(r for lex in lexicons for r in lex.roots),
        allowlist=frozenset().union(*(lex.allowlist for lex in lexicons)),
        cue_words=frozenset().union(*(lex.cue_words for lex in lexicons)),
        cue_pattern=re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None,
    )


DEFAULT_FILES = ("sw_roots.yaml", "en_roots.yaml")


@lru_cache
def _default_lexicon() -> Lexicon:
    data = files("collector.data")
    return merge(
        *(
            parse_lexicon(yaml.safe_load(data.joinpath(name).read_text(encoding="utf-8")))
            for name in DEFAULT_FILES
        )
    )
