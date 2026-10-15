import pytest

from collector.nlp.highlight import hit_words, segments


def marked(raw: str) -> str:
    return "".join(f"[{piece}]" if hit else piece for piece, hit in segments(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "@kachindi_briged_kiwango kaa kwakutulia wewe malayq",
            "@kachindi_briged_kiwango kaa kwakutulia wewe [malayq]",
        ),
        ("Wewe ni M.J.I.N.G.A kabisa!!", "Wewe ni [M.J.I.N.G.A] kabisa!!"),
        ("huyu ni m j i n g a sana", "huyu ni [m j i n g a] sana"),  # spaced out
        ("wewe ni m@l@y@", "wewe ni [m@l@y@]"),  # leet kept, it is part of the word
        ("wewe ni mbwa, mbwa wangu", "wewe ni [mbwa], [mbwa] wangu"),  # each its own mark
        ("Mjinga wewe!!!", "[Mjinga] wewe!!!"),  # trailing punctuation not marked
        ("kahaba😂😂", "[kahaba]😂😂"),
        ("f*ck you idiot", "[f*ck] you [idiot]"),
        ("@wazirimbwana504 shoga wewe", "@wazirimbwana504 [shoga] wewe"),  # never in a mention
        ("nyie wajinga na wapumbavu", "nyie [wajinga] na [wapumbavu]"),
        ("mumeo,mkundu wewe", "mumeo,[mkundu] wewe"),  # only the word, not its neighbour
        ("mjinga/mpumbavu", "[mjinga]/[mpumbavu]"),
        ("M.J.I.N.G.A, mjinga", "[M.J.I.N.G.A], [mjinga]"),
    ],
)
def test_marks_the_hit_words_as_written(raw: str, expected: str) -> None:
    assert marked(raw) == expected


@pytest.mark.parametrize("raw", ["habari za asubuhi", "my dog is cute", "we nani", ""])
def test_no_marks_without_a_hit(raw: str) -> None:
    assert segments(raw) == [(raw, False)]
    assert hit_words(raw) == []


def test_hit_words() -> None:
    assert hit_words("Wewe ni M.J.I.N.G.A, mjinga kweli") == ["M.J.I.N.G.A", "mjinga"]
