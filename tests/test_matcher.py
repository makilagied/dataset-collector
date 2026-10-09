from pathlib import Path

import pytest
import yaml

from collector.nlp import lexicon as lx
from collector.nlp.matcher import match, max_edits
from collector.nlp.normalize import normalize
from tests.conftest import REPORT

FIXTURE = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "matcher_fixture.yaml").read_text(encoding="utf-8")
)


def hit(raw: str) -> bool:
    return match(normalize(raw)).is_hit


# --- precision / recall ---------------------------------------------------------------


def test_fixture_size() -> None:
    assert len(FIXTURE["positive"]) >= 100
    assert len(FIXTURE["negative"]) >= 100


def test_precision_recall() -> None:
    pos, neg = FIXTURE["positive"], FIXTURE["negative"]
    false_neg = [t for t in pos if not hit(t)]
    false_pos = [t for t in neg if hit(t)]
    tp, fp = len(pos) - len(false_neg), len(false_pos)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / len(pos)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    REPORT.append(
        f"matcher fixture: {len(pos)} positive / {len(neg)} negative | "
        f"precision {precision:.3f}  recall {recall:.3f}  F1 {f1:.3f} | TP {tp} FP {fp} "
        f"FN {len(false_neg)}"
    )
    for t in false_neg:
        REPORT.append(f"  missed:         {t!r}")
    for t in false_pos:
        REPORT.append(f"  false positive: {t!r}")

    # Floors guard against regressions; the fixture is hand-made, not a real benchmark.
    assert precision >= 0.95, false_pos
    assert recall >= 0.95, false_neg


# --- methods --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "root", "method"),
    [
        ("mjinga", "jinga", "regex"),
        ("wajinga", "jinga", "regex"),
        ("mjiinga", "jinga", "regex"),
        ("mj#inga", "jinga", "regex"),
        ("mjnga", "jinga", "skeleton"),
        ("mpmbv", "pumbavu", "skeleton"),
        ("mujinga", "jinga", "fuzzy"),
        ("mpumbafu", "pumbavu", "fuzzy"),
    ],
)
def test_methods(text: str, root: str, method: str) -> None:
    result = match(text)
    assert result.matched_roots == [root]
    assert result.match_methods == [method]


def test_short_roots_need_exact_match() -> None:
    assert match("kama").matches == []  # one edit from "kuma"
    assert match("kuma").matched_roots == ["kuma"]


def test_bare_root_not_matched_when_prefix_required() -> None:
    assert match("shamba langu").matches == []
    assert match("mshamba").matched_roots == ["shamba"]


def test_allowlist_blocks_fuzzy_collision() -> None:
    assert match("wajenga").matches == []  # one edit from "wajinga"


def test_mentions_and_urls_skipped() -> None:
    assert match("@mjinga_official https://x.com/mjinga").matches == []


def test_context_dependent_needs_cue() -> None:
    plain = match("mbwa wangu")
    assert plain.matched_roots == ["mbwa"]
    assert not plain.is_hit
    assert plain.score == pytest.approx(0.35)
    cued = match("wewe ni mbwa")
    assert cued.is_hit
    assert cued.score == pytest.approx(0.7)
    assert match("unakula kama mbwa").is_hit  # 2nd-person verb form


def test_score_reflects_severity_and_method() -> None:
    assert match("malaya").score == pytest.approx(1.0)
    assert match("mjinga").score == pytest.approx(0.7)
    assert match("mjnga").score == pytest.approx(0.63)


def test_multiple_roots() -> None:
    result = match("mjinga na mpumbavu")
    assert result.matched_roots == ["jinga", "pumbavu"]
    assert [m.token_index for m in result.matches] == [0, 2]


def test_empty() -> None:
    result = match("")
    assert (result.matches, result.score, result.is_hit) == ([], 0.0, False)


@pytest.mark.parametrize(("length", "edits"), [(3, 0), (4, 0), (5, 1), (7, 1), (8, 2), (12, 2)])
def test_max_edits(length: int, edits: int) -> None:
    assert max_edits(length) == edits


# --- lexicon ---------------------------------------------------------------------------


def test_default_lexicon_loads() -> None:
    lex = lx.load_lexicon()
    assert len(lex.roots) >= 20
    jinga = next(r for r in lex.roots if r.root == "jinga")
    assert "mjinga" in jinga.forms
    assert jinga.skeleton_ok
    assert not next(r for r in lex.roots if r.root == "kuma").skeleton_ok
    assert lex.is_cue("wewe") and lex.is_cue("umekuwa") and not lex.is_cue("mama")


def test_load_from_path(tmp_path: Path) -> None:
    f = tmp_path / "lex.yaml"
    f.write_text("roots:\n  - {root: fala, severity: low}\n", encoding="utf-8")
    lex = lx.load_lexicon(f)
    assert lex.roots[0].prefixes == ("",)
    assert lex.cue_pattern is None
    assert match("fala", lex).matched_roots == ["fala"]


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({}, "non-empty"),
        ({"roots": ["x"]}, "mapping"),
        ({"roots": [{"root": "fala"}]}, "missing"),
        ({"roots": [{"root": "Fa1a", "severity": "low"}]}, "lowercase"),
        ({"roots": [{"root": "fala", "severity": "awful"}]}, "severity"),
    ],
)
def test_invalid_lexicon(raw: dict[str, object], error: str) -> None:
    with pytest.raises(lx.LexiconError, match=error):
        lx.parse_lexicon(raw)
