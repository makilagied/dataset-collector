"""Pluggable abuse scorer for the `model` sampling stratum.

A real scorer (e.g. a fine-tuned AfroXLMR) implements `Scorer` and is registered in
SCORERS. Until then the stub flags nothing, and the sampler moves the model quota to the
random stratum.
"""

from collections.abc import Callable, Sequence
from typing import Protocol


class Scorer(Protocol):
    name: str

    def score(self, texts: Sequence[str]) -> list[float]:
        """Probability-like abuse score in [0, 1] per text."""
        ...


class StubScorer:
    name = "stub"

    def score(self, texts: Sequence[str]) -> list[float]:
        return [0.0] * len(texts)


SCORERS: dict[str, Callable[[], Scorer]] = {"stub": StubScorer}
