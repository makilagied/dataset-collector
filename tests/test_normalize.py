import pytest

from collector.nlp import normalize as n

# (raw, expected) — obfuscations seen in Swahili comments, plus things that must survive.
CASES: list[tuple[str, str]] = [
    # case / punctuation
    ("MJINGA", "mjinga"),
    ("Mjinga!!!", "mjinga!"),
    ("Mbwa wewe!!", "mbwa wewe!"),
    ("acha!", "acha!"),
    # separators inside words
    ("m.j.i.n.g.a", "mjinga"),
    ("m_j_i_n_g_a", "mjinga"),
    ("m-j-i-n-g-a", "mjinga"),
    ("m*j*i*n*g*a", "mjinga"),
    ("m~j~i~n~g~a", "mjinga"),
    ("m/p/u/m/b/a/v/u", "mpumbavu"),
    ("mji.nga", "mjinga"),
    ("m-pumbavu", "mpumbavu"),
    ("M-J-I-N-G-A wewe", "mjinga wewe"),
    # spaced-out letters
    ("m j i n g a", "mjinga"),
    ("M J I N G A wewe", "mjinga wewe"),
    ("u ni m j i n g a", "u ni mjinga"),
    ("m . j . i . n . g . a", "mjinga"),
    # leetspeak
    ("mj1nga", "mjinga"),
    ("mj!nga", "mjinga"),
    ("m@l@y@", "malaya"),
    ("m4l4y4", "malaya"),
    ("MAL@Y@", "malaya"),
    ("mpumb@vu", "mpumbavu"),
    ("f@l@", "fala"),
    ("mp0mb0v0", "mpombovo"),
    ("$hoga", "shoga"),
    ("m5enge", "msenge"),
    ("7ako", "tako"),
    ("mal3", "male"),
    # combined separators + leetspeak
    ("m j 1 n g @", "mjinga"),
    ("m.j.1.n.g.@", "mjinga"),
    ("M.@.L.@.Y.@", "malaya"),
    # repeated characters
    ("mjiiinga", "mjinga"),
    ("mjiiiiiingaaaaa", "mjinga"),
    ("mjinnnnga", "mjinga"),
    ("mzee", "mzee"),
    ("saa", "saa"),
    ("😂😂😂 mjinga", "😂 mjinga"),
    # unicode tricks
    ("мjіngа", "mjinga"),  # Cyrillic м, і, а
    ("mjιngα", "mjinga"),  # Greek iota, alpha
    ("ｍｊｉｎｇａ", "mjinga"),  # fullwidth
    ("𝐦𝐣𝐢𝐧𝐠𝐚", "mjinga"),  # mathematical bold
    ("ⓜⓙⓘⓝⓖⓐ", "mjinga"),  # circled
    ("mj​inga", "mjinga"),  # zero-width space
    ("mj­inga", "mjinga"),  # soft hyphen
    ("m‍j‌i⁠nga", "mjinga"),  # joiners / word joiner
    ("mjíngá", "mjinga"),  # diacritics
    ("MJÏNGÂ", "mjinga"),
    # texting shorthand
    ("cjui", "sijui"),
    ("cwezi", "siwezi"),
    ("cc", "sisi"),
    ("mcenge", "msenge"),
    ("xaxa", "sasa"),
    ("ww ni mjinga", "wewe ni mjinga"),
    ("unataka nn?", "unataka nini?"),
    ("km mbwa", "kama mbwa"),
    ("m2 mbaya", "mtu mbaya"),
    ("wa2 wote", "watu wote"),
    ("Ww, c mjinga", "wewe, si mjinga"),
    # must survive unchanged
    ("chakula", "chakula"),
    ("ng'ombe", "ng'ombe"),
    ("ng’ombe", "ng'ombe"),  # curly apostrophe unified
    ("mwaka 2026", "mwaka 2026"),
    ("100%", "100%"),
    ("@juma_k mjinga", "@juma_k mjinga"),
    ("@m4l4y4.tz", "@m4l4y4.tz"),
    ("https://example.com/a.b-c", "https://example.com/a.b-c"),
    ("www.mjinga.co.tz", "www.mjinga.co.tz"),
    ("habari  za\nasubuhi", "habari za asubuhi"),
    ("", ""),
    ("   ", ""),
]


def test_at_least_40_cases() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize(("raw", "expected"), CASES, ids=[c[0] or "<empty>" for c in CASES])
def test_normalize(raw: str, expected: str) -> None:
    assert n.normalize(raw) == expected


def test_idempotent() -> None:
    for raw, _ in CASES:
        once = n.normalize(raw)
        assert n.normalize(once) == once, raw


# --- individual steps ---------------------------------------------------------------


def test_fold() -> None:
    assert n.fold("ＭＪＩＮＧÁ​") == "mjinga"


def test_homoglyphs_leave_latin_alone() -> None:
    assert n.homoglyphs("habari yako") == "habari yako"
    assert n.homoglyphs("ѕокоtа") == "sokota"


def test_separators_need_three_units() -> None:
    assert n.separators("a b") == "a b"  # too short to be a spaced-out word
    assert n.separators("a b c") == "abc"


def test_separators_keep_apostrophe_and_comma() -> None:
    assert n.separators("ng'ombe sawa,sawa") == "ng'ombe sawa,sawa"


def test_leet_ignores_numbers_and_trailing_bang() -> None:
    assert n.leetspeak("2026 100 acha!") == "2026 100 acha!"
    assert n.leetspeak("!wewe") == "!wewe"


def test_collapse_keeps_doubles() -> None:
    assert n.collapse("mzee saa kuu") == "mzee saa kuu"
    assert n.collapse("sawaaaa ...") == "sawa ."


def test_shorthand_custom_table() -> None:
    import re

    table = n.Shorthand(tokens={"bt": "lakini"}, patterns=((re.compile("q"), "k"),))
    assert n.shorthand("bt qaribu", table) == "lakini karibu"


def test_shorthand_yaml_loads() -> None:
    table = n.load_shorthand()
    assert table.tokens["ww"] == "wewe"
    assert table.patterns


@pytest.mark.parametrize(
    "token", ["@juma", "http://x.y/z", "https://a.b", "www.site.com", "me@mail.com"]
)
def test_protected(token: str) -> None:
    assert n.is_protected(token)


@pytest.mark.parametrize("token", ["m@l@y@", "@", "mjinga", "sawa.sawa"])
def test_not_protected(token: str) -> None:
    assert not n.is_protected(token)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mjinga/mpumbavu", "mjinga mpumbavu"),  # two whole words stay apart
        ("wajinga.wewe", "wajinga wewe"),
        ("M.J.I.N.G.A", "mjinga"),  # a disguised word is joined
        ("mji-nga", "mjinga"),
        ("mala-ya", "malaya"),
    ],
)
def test_separators_join_disguised_words_but_not_whole_words(raw: str, expected: str) -> None:
    assert n.normalize(raw) == expected
