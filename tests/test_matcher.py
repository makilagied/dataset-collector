from pathlib import Path

import pytest
import yaml

from collector.nlp import lexicon as lx
from collector.nlp.matcher import keyboard_adjacent, match, match_text, max_edits, typo_like
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
        ("mjihga", "jinga", "fuzzy"),
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


# --- reviewed real comments -------------------------------------------------------------

REVIEWED = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "reviewed_comments.yaml").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("raw", REVIEWED["negative"])
def test_reviewed_false_positives_stay_fixed(raw: str) -> None:
    assert not hit(raw)


@pytest.mark.parametrize("raw", REVIEWED["positive"])
def test_reviewed_insults_still_found(raw: str) -> None:
    assert hit(raw)


@pytest.mark.parametrize(
    ("word", "target", "ok"),
    [
        ("malayq", "malaya", True),  # q is next to a
        ("mjigna", "mjinga", True),  # swapped neighbours
        ("mjiinga", "mjinga", True),  # doubled letter
        ("mabaya", "malaya", False),  # b is far from l: a different word
        ("ndani", "nyani", False),
        ("nani", "nyani", False),  # dropped consonant
        ("wajiunga", "wajinga", False),  # inserted letter
        ("mjinga", "mjinga", False),  # identical is a regex match, not a typo
    ],
)
def test_typo_like(word: str, target: str, ok: bool) -> None:
    assert typo_like(word, target, 1) is ok


def test_keyboard_adjacent() -> None:
    assert keyboard_adjacent("q", "a") and keyboard_adjacent("s", "w")
    assert keyboard_adjacent("z", "x") and keyboard_adjacent("b", "h")
    assert not keyboard_adjacent("b", "l") and not keyboard_adjacent("a", "a")


@pytest.mark.parametrize(
    ("text", "root"),
    [
        ("tomba", "tomba"),
        ("nitakutomba", "tomba"),
        ("Tombwa", "tomba"),
        ("t.o.m.b.a", "tomba"),
        ("kufira", "fira"),
        ("anafirwa", "fira"),
        ("kufila", "fila"),
        ("fila", "fila"),
        ("wewe nakugonga", "gonga"),
    ],
)
def test_verbs_take_any_prefix_and_verb_endings(text: str, root: str) -> None:
    result = match(normalize(text))
    assert result.is_hit and result.matched_roots == [root]


@pytest.mark.parametrize(
    "text",
    [
        "gonga mlango",
        "amegongwa na gari",
        "magari yamegongana",
        "the file",
        "fire!",
        "profile",
        "tomb raider",
        "sunna na nafila",
        "filamu",
    ],
)
def test_verbs_leave_innocent_words_alone(text: str) -> None:
    assert not hit(text)


def test_verb_root_must_be_dictionary_form() -> None:
    with pytest.raises(lx.LexiconError, match="must end in 'a'"):
        lx.parse_lexicon({"roots": [{"root": "tomb", "verb": True, "severity": "high"}]})
    with pytest.raises(lx.LexiconError, match="any prefix"):
        lx.parse_lexicon(
            {"roots": [{"root": "tomba", "verb": True, "prefixes": ["ku"], "severity": "high"}]}
        )


@pytest.mark.parametrize("text", ["MAMAEE", "mamae", "Mamaeeee", "m.a.m.a.e", "mamaee wewe"])
def test_mamae_is_a_hit(text: str) -> None:
    assert hit(text)


@pytest.mark.parametrize("text", ["mama", "mamake", "mama yako", "mamaye"])
def test_plain_mother_words_are_not_mamae(text: str) -> None:
    assert not hit(text)


# Words from abusive comments reviewed 2026-10-17.
@pytest.mark.parametrize(
    "text",
    [
        "namnyonya",
        "nikunyonyee",
        "kunyonywa",
        "anakulamba",
        "anawakula",
        "wanajibaka",
        "najibaka",
        "bakwa",
        "tako",
        "titi",
        "punyeto",
        "nyeto",
        "nakojoa",
        "kudadeki",
        "ananyege",
        "Mfileeee",
        "Kisimiii",
        "Visimi",
        "mdinya",
        "Unanidinyaaa",
        "nililiwa",
        "mbunye",
        "umbwaaa",
        "mxiewww",
        "Mxiuuuuuu",
        "modofokoooo",
        "mbupuz",
        "qmmk",
        "qmmke",
        "mishahawa",
        "kakuinamisha",
        "tmbwa",
        "unanikaza",
        "kumchapa",
        "Mbususu",
        "nikilombe",
        "kulombwa",
        "nikizibue",
        "nyash",
        "wanakulana",
        "unani harmonaiz",
        "chuchuuu wewe",
        "wewe kiharage",
        "umala….utakua",
        "umalaya",
        "jitombea",
        "kutomba2",
        "mjinga2",
        "kamalaya",
    ],
)
def test_reviewed_abusive_words(text: str) -> None:
    assert hit(text)


@pytest.mark.parametrize(
    "text",
    [
        "kibaka amekamatwa",
        "tabaka la kati",
        "kuna mabaka usoni",
        "kaza buti kaka",
        "kuzibua sinki",
        "kula chakula",
        "kiharage na wali",
        "Chuchu Hans amependeza",
        "shahada ya kwanza",
        "kisima cha maji",
        "harmonize ametoa wimbo",
        "Kilamba",
    ],
)
def test_reviewed_words_spare_innocent_uses(text: str) -> None:
    assert not hit(text)


def hit_text(raw: str) -> bool:
    return match_text(raw).is_hit


# Words and spellings from the "Abusive Words" spreadsheet (2026-10-19).
@pytest.mark.parametrize(
    "text",
    [
        "amelarwa",
        "anafirika",
        "kafirwe",
        "kajifire",
        "kakifirishe",
        "firaneni",
        "anatomwa",
        "kajitombeshe",
        "katombane",
        "Tombaneni",
        "kitombi",
        "mtombo",
        "katiwe",
        "Unatiwa",
        "Basha",
        "Bwabwa",
        "choko",
        "Khanithi",
        "kumanyoko",
        "kumambichi",
        "mbolo",
        "mboo",
        "uboo",
        "mbwajike",
        "mfiro",
        "nyoko",
        "Pumbafu",
        "Shwaini",
        "Shenzi",
        "Kinembe",
        "Changudoa",
        "Dada poa",
        "Mwendawazimu",
        "arsehole",
        "prick",
        "bollocks",
        "shit",
        "bullshit",
        "pussies",
        "piss off",
        "punani",
        "Minge",
        "cock sucker",
        "you ass",
        "you cock",
    ],
)
def test_sheet_words(text: str) -> None:
    assert hit_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "anaf**rana",
        "anat#mbana",
        "kaf**rike",
        "Kish##nzi",
        "kis*nge",
        "Shwa*ni",
        "mb*o",
        "uch*",
        "kum@@nyoko",
        "kajif@re",
        "f@ck",
        "c@nt",
        "d%ck",
        "p%ssy",
        "pr**ck",
        "ars**hole",
        "bullsh**t",
        "boll@cks",
        "pun*ni",
        "M*nge",
    ],
)
def test_hidden_letters(text: str) -> None:
    assert hit_text(text)
    assert match_text(text).hits[0].method in ("masked", "regex", "skeleton")


@pytest.mark.parametrize(
    "text",
    [
        "Mwanamke atiwa mbaroni",
        "kutiwa mbaroni",
        "nyoka mkubwa",
        "nimechoka sana",
        "fire on the pitch",
        "he scored 2 balls",
        "I am a sucker for love",
        "door knob",
        "damn that goal",
        "Clara",
        "a*b test",
        "10*20",
        "haha*",
        "uchumi",
        "boo",
    ],
)
def test_sheet_words_spare_innocent_uses(text: str) -> None:
    assert not hit_text(text)


def test_quoted_affixes_required() -> None:
    raw = {"roots": [{"root": "piss", "severity": "low", "suffixes": ["", False]}]}
    with pytest.raises(lx.LexiconError, match="quoted"):
        lx.parse_lexicon(raw)
