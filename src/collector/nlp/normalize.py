"""Swahili-aware normalizer for obfuscated comments.

Each step is a pure `str -> str` function; `normalize` runs them in order:

1. fold        - NFKC, lowercase, strip zero-width chars and diacritics, unify apostrophes
2. homoglyphs  - Cyrillic/Greek/Latin lookalikes -> Latin
3. separators  - join spaced-out letters ("m j i n g a", "m.j.i.n.g.a")
4. leetspeak   - @->a 4->a 3->e 1->i !->i 0->o $->s 5->s 7->t, only next to letters
5. collapse    - runs of 3+ identical letters -> 1 ("mjiiinga"); doubles are kept because
                 Swahili has real ones ("mzee", "saa"); repeated punctuation/emoji -> 1
6. shorthand   - texting shorthand from sw_shorthand.yaml ("cjui" -> "sijui", "ww" -> "wewe")

Separators run before leetspeak (the plan lists them the other way round): in
"m j 1 n g a" the "1" only has letter neighbours once the letters are joined.

@mentions and URLs are protected from steps 3-6. The apostrophe in "ng'" is preserved.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml

# --- 1. fold ---------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'"})
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­᠎͏"), None)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.translate(_APOSTROPHES)).casefold()
    text = text.translate(_INVISIBLE)
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


# --- 2. homoglyphs -----------------------------------------------------------------

_HOMOGLYPHS = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "з": "3",
        "і": "i",
        "ј": "j",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "ѕ": "s",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "һ": "h",
        "ӏ": "l",
        "ь": "b",
        "п": "n",
        "г": "r",
        # Greek
        "α": "a",
        "β": "b",
        "γ": "y",
        "ε": "e",
        "η": "n",
        "ι": "i",
        "κ": "k",
        "μ": "u",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "σ": "o",
        "ς": "s",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ω": "w",
        # Latin lookalikes that NFD does not decompose
        "ı": "i",
        "ł": "l",
        "ø": "o",
        "đ": "d",
        "ɡ": "g",
        "ħ": "h",
    }
)


def homoglyphs(text: str) -> str:
    return text.translate(_HOMOGLYPHS)


# --- token helpers -------------------------------------------------------------------

_LEET = {"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "0": "o", "$": "s", "5": "s", "7": "t"}
_URL = re.compile(r"://|^www\.|^[\w.+-]+@[\w-]+\.[a-z]{2,}")
_MENTION = re.compile(r"^@\w")


def is_protected(token: str) -> bool:
    return bool(_MENTION.match(token) or _URL.search(token))


def _map_tokens(text: str, fn: Callable[[str], str]) -> str:
    return " ".join(t if is_protected(t) else fn(t) for t in text.split())


def _has_letter(token: str) -> bool:
    return any(ch.isalpha() for ch in token)


# --- 3. separators -------------------------------------------------------------------

_SEP_CHARS = "._\\-*~|+/•·"
_UNIT = r"(?:[^\W_]|[@$!])"
_INNER_SEP = re.compile(rf"(?<={_UNIT})[{_SEP_CHARS}]+(?={_UNIT})")
_SEP_ONLY = re.compile(rf"^[{_SEP_CHARS}]+$")


def _is_unit(token: str) -> bool:
    return len(token) == 1 and (token.isalnum() or token in "@$!")


def separators(text: str) -> str:
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        # A spaced-out run: 3+ single characters, optionally with separator-only tokens
        # between them ("m j i n g a", "m . j . i").
        units: list[str] = []
        j = i
        while j < len(tokens):
            if _is_unit(tokens[j]):
                units.append(tokens[j])
                j += 1
            elif (
                units
                and _SEP_ONLY.match(tokens[j])
                and j + 1 < len(tokens)
                and _is_unit(tokens[j + 1])
            ):
                j += 1
            else:
                break
        if len(units) >= 3:
            out.append("".join(units))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(t if is_protected(t) else _INNER_SEP.sub("", t) for t in out)


# --- 4. leetspeak --------------------------------------------------------------------


def _leet_token(token: str) -> str:
    if not _has_letter(token):
        return token  # plain numbers / punctuation stay as they are
    out = []
    for k, ch in enumerate(token):
        repl = _LEET.get(ch)
        if repl is not None:
            left = token[k - 1] if k > 0 else ""
            right = token[k + 1] if k + 1 < len(token) else ""
            if ch == "!":
                # "!" is often real punctuation ("acha!"): only inside a word.
                convert = left.isalpha() and right.isalpha()
            else:
                convert = any(c.isalpha() or c in _LEET for c in (left, right) if c)
            if convert:
                out.append(repl)
                continue
        out.append(ch)
    return "".join(out)


def leetspeak(text: str) -> str:
    return _map_tokens(text, _leet_token)


# --- 5. collapse ---------------------------------------------------------------------

_LETTER_RUN = re.compile(r"([^\W\d_])\1{2,}")
_SYMBOL_RUN = re.compile(r"([^\w\s])\1+")


def collapse(text: str) -> str:
    return _map_tokens(text, lambda t: _SYMBOL_RUN.sub(r"\1", _LETTER_RUN.sub(r"\1", t)))


# --- 6. shorthand --------------------------------------------------------------------


@dataclass(frozen=True)
class Shorthand:
    tokens: dict[str, str]
    patterns: tuple[tuple[re.Pattern[str], str], ...]


@lru_cache
def load_shorthand() -> Shorthand:
    raw: dict[str, Any] = yaml.safe_load(
        files("collector.data").joinpath("sw_shorthand.yaml").read_text(encoding="utf-8")
    )
    return Shorthand(
        tokens={str(k): str(v) for k, v in (raw.get("tokens") or {}).items()},
        patterns=tuple((re.compile(p), r) for p, r in raw.get("patterns") or []),
    )


_EDGES = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _shorthand_token(token: str, table: Shorthand) -> str:
    m = _EDGES.match(token)
    if not m or not m.group(2):
        return token
    lead, core, trail = m.groups()
    if core in table.tokens:
        core = table.tokens[core]
    elif _has_letter(core):
        for pattern, repl in table.patterns:
            core = pattern.sub(repl, core)
    return f"{lead}{core}{trail}"


def shorthand(text: str, table: Shorthand | None = None) -> str:
    table = table or load_shorthand()
    return _map_tokens(text, lambda t: _shorthand_token(t, table))


# --- pipeline ------------------------------------------------------------------------

STEPS: tuple[Callable[[str], str], ...] = (
    fold,
    homoglyphs,
    separators,
    leetspeak,
    collapse,
    shorthand,
)


def normalize(text: str) -> str:
    for step in STEPS:
        text = step(text)
    return " ".join(text.split())


def normalize_plain(text: str) -> str:
    """Every step except Swahili shorthand: the form English words are matched in."""
    for step in STEPS[:-1]:
        text = step(text)
    return " ".join(text.split())
