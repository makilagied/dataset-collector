import pytest

from collector.nlp import lexicon as lx
from collector.nlp.matcher import match
from collector.nlp.normalize import normalize, normalize_plain
from collector.storage_filter import is_hit


def hit(raw: str) -> bool:
    return match(normalize(raw), text_plain=normalize_plain(raw)).is_hit


@pytest.mark.parametrize(
    "raw",
    [
        "fuck you",
        "f*ck you",
        "FUUUCK u",
        "shut the f.u.c.k up",
        "fck off",
        "motherfucker",
        "you are a b!tch",
        "stupid idiots",
        "a s s h o l e",
        "dumbass",
        "you dog",
        "u shit",
        "shit happens",  # listed as abusive on its own in the "Abusive Words" sheet
        "you ugly",
        "you nigga",
        "kys",
        "mjinga wewe, fuck",
    ],
)
def test_english_abuse_is_found(raw: str) -> None:
    assert hit(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "class assess pass batch idiom",  # look-alikes; no typo matching in English
        "Dickens cocktail cockpit",
        "my dog is cute",  # animal words need a 2nd-person cue
        "nice goat skin",
        "ugly dress",
        "my nigga",
        "sexy",
        "hongera sana kaka",
        "nyama ya mbuzi",  # "ya" is Swahili "of", not English "you"
    ],
)
def test_innocent_text_is_not_a_hit(raw: str) -> None:
    assert not hit(raw)


def test_shorthand_does_not_hide_english() -> None:
    # Swahili shorthand rewrites "c" ("fuck" -> "fusik"); English roots never see that.
    assert normalize("fuck") != "fuck"
    assert match(normalize("fuck")).matches == []
    assert hit("fuck")


def test_suffix_forms_and_language() -> None:
    lex = lx.load_lexicon()
    fuck = next(r for r in lex.roots if r.root == "fuck")
    assert fuck.lang == "en" and not fuck.fuzzy
    assert {"fucking", "motherfucker", "fuck"} <= set(fuck.forms)
    jinga = next(r for r in lex.roots if r.root == "jinga")
    assert jinga.lang == "sw" and jinga.fuzzy
    assert lex.is_cue("you") and lex.is_cue("wewe") and not lex.is_cue("ya")


def test_build_pattern_suffixes() -> None:
    p = lx.build_pattern("idiot", ("",), ("", "s", "ic"))
    assert all(p.search(w) for w in ("idiot", "idiots", "idiotic", "idiiiots"))
    assert not p.search("idiotsx")
    required = lx.build_pattern("cock", ("",), ("sucker",))
    assert required.search("cocksucker") and not required.search("cock")


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("fuck you", True),
        ("wewe ni mjinga", True),
        ("you idiot", True),
        ("hongera sana", False),
        ("my dog", False),
        ("my nigga", False),  # scores 0.5 but is not aimed at anyone
        ("mbwa wangu", False),
    ],
)
def test_abusive_only_filter(raw: str, kept: bool) -> None:
    assert is_hit(raw) is kept
