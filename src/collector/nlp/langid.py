"""Language identification.

Uses lingua (optional `nlp` extra), restricted to the languages expected in East African
Instagram comments, which is both faster and more accurate on short text than detecting
among all languages. Without lingua, falls back to a function-word heuristic so the
pipeline still runs; the backend name is part of the pipeline version, so switching
backends triggers reprocessing.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from collector.nlp.normalize import fold, is_protected

MIN_LETTERS = 3
_LETTERS = re.compile(r"[^\W\d_]")


@dataclass(frozen=True)
class LangResult:
    lang: str | None  # ISO 639-1, e.g. "sw", "en"; None when undecidable
    conf: float


class Detector(Protocol):
    name: str

    def detect(self, text: str) -> LangResult: ...


def prepare(text: str) -> str:
    """Fold and drop @mentions / URLs, which carry no language signal."""
    return " ".join(t for t in fold(text).split() if not is_protected(t))


class HeuristicDetector:
    """Counts Swahili vs English function words. Coarse, but dependency-free."""

    name = "heuristic"
    SW = frozenset(
        "na ya wa ni kwa za la cha vya hii huyu hao sana wewe mimi sisi nyinyi yako wako "
        "kama lakini hapa sasa tu pia bado nini gani habari asante karibu mungu kweli "
        "hakuna kuna safi poa mambo hongera pole dada kaka mama baba leo kesho".split()
    )
    EN = frozenset(
        "the and is are you your to of in it this that for my so not be have what with "
        "i me we they he she was were nice good love very".split()
    )

    def detect(self, text: str) -> LangResult:
        words = re.findall(r"[a-z']+", prepare(text))
        sw = sum(w in self.SW for w in words)
        en = sum(w in self.EN for w in words)
        if sw == en:
            return LangResult(None, 0.0)
        return LangResult("sw" if sw > en else "en", round(max(sw, en) / (sw + en), 3))


class LinguaDetector:
    name = "lingua"

    def __init__(self) -> None:
        from lingua import Language, LanguageDetectorBuilder

        self._detector = LanguageDetectorBuilder.from_languages(
            Language.SWAHILI, Language.ENGLISH, Language.FRENCH
        ).build()

    def detect(self, text: str) -> LangResult:
        cleaned = prepare(text)
        if len(_LETTERS.findall(cleaned)) < MIN_LETTERS:
            return LangResult(None, 0.0)
        values = self._detector.compute_language_confidence_values(cleaned)
        if not values:
            return LangResult(None, 0.0)
        top = values[0]
        return LangResult(top.language.iso_code_639_1.name.lower(), round(top.value, 3))


@lru_cache
def get_detector() -> Detector:
    try:
        return LinguaDetector()
    except ImportError:
        return HeuristicDetector()
