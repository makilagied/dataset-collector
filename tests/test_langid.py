import importlib.util

import pytest

from collector.nlp import langid

HAS_LINGUA = importlib.util.find_spec("lingua") is not None


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("wewe ni mjinga sana", "sw"),
        ("habari za asubuhi dada", "sw"),
        ("this is so nice", "en"),
        ("@juma the video is nice https://x.com", "en"),
        ("mjinga", None),  # no function words either way
        ("", None),
    ],
)
def test_heuristic(text: str, lang: str | None) -> None:
    assert langid.HeuristicDetector().detect(text).lang == lang


def test_heuristic_confidence_is_share_of_votes() -> None:
    result = langid.HeuristicDetector().detect("wewe ni nice sana")
    assert result.lang == "sw"
    assert result.conf == pytest.approx(0.75)


def test_prepare_drops_mentions_and_urls() -> None:
    assert langid.prepare("@Juma Habari https://x.com/a") == "habari"


@pytest.mark.skipif(not HAS_LINGUA, reason="lingua not installed (pip install -e '.[nlp]')")
class TestLingua:
    @pytest.fixture(scope="class")
    def detector(self) -> langid.LinguaDetector:
        return langid.LinguaDetector()

    @pytest.mark.parametrize(
        ("text", "lang"),
        [
            ("wewe ni mjinga sana", "sw"),
            ("habari za asubuhi", "sw"),
            ("nice video bro", "en"),
            ("merci beaucoup", "fr"),
        ],
    )
    def test_detects(self, detector: langid.LinguaDetector, text: str, lang: str) -> None:
        result = detector.detect(text)
        assert result.lang == lang
        assert 0 < result.conf <= 1

    def test_too_short(self, detector: langid.LinguaDetector) -> None:
        assert detector.detect("😂 @juma ok").lang is None

    def test_default_backend(self) -> None:
        langid.get_detector.cache_clear()
        assert langid.get_detector().name == "lingua"


def test_falls_back_without_lingua(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_lingua(self: object) -> None:
        raise ImportError("no lingua")

    monkeypatch.setattr(langid.LinguaDetector, "__init__", no_lingua)
    langid.get_detector.cache_clear()
    try:
        assert langid.get_detector().name == "heuristic"
    finally:
        langid.get_detector.cache_clear()
