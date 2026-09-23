"""A cut ends where a word ends.

The engine cuts text to fit: a 'look into' summary cut each label at 40 characters. Cut inside a word, what is
left reads like a word of its own — 「… a Service of Saf」 was tagged a person, and 「Saf」 went out as a pseudonym
inside 「Safari」. So a cut steps back to where a word ends, but only so far: a word reaching back more than half
the room is cut where the room ends, rather than giving up more than half of what would fit.

Where a word ends is one rule, which the redactor is to share: at a space, punctuation or an underscore, where
letters with capitals meet letters without, and where letters meet digits; an accent written as a mark of its
own changes none of that. Text in other scripts is written here as escapes, so this file stays in English.
"""

import unicodedata

from macwork.model import clip, joined, joined_at

SERVICE = "a Service of Safari on this Mac"
LONG = "pneumonoultramicroscopicsilicovolcanoconiosis"
BROWSER = "\u6d4f\u89c8\u5668"          # three Chinese letters ("browser"): a script without capitals
OPENED = "\u6253\u5f00\u4e86"           # three more ("opened")
THAI = "\u0e17\u0e35\u0e48"             # a Thai letter with its vowel sign and tone mark, both marks of their own
ACUTE = "\u0301"                        # an acute accent written as a mark of its own, after its letter


def nfd(text: str) -> str:
    """Every accent written as a letter followed by a mark."""
    return unicodedata.normalize("NFD", text)


def words(text: str) -> list[str]:
    """The words of `text`, as `joined_at` divides it."""
    ends = [0] + [k for k in range(1, len(text)) if not joined_at(text, k)] + [len(text)]
    return [w for w in (text[a:b] for a, b in zip(ends, ends[1:])) if any(ch.isalnum() for ch in w)]


def test_a_cut_never_ends_inside_a_word():
    assert clip(SERVICE, 14) == "a Service of…"
    assert all(clip(SERVICE, n) == "a Service of…" for n in range(14, 20))      # never 「Saf」, 「Safar」
    assert clip(SERVICE, 20) == "a Service of Safari…"


def test_a_cut_steps_back_at_most_half_the_room():
    assert clip("abcdefgh " + "y" * 20, 20) == "abcdefgh…"                      # the word began 10 back: cut there
    assert clip("abcdefg " + "y" * 21, 20) == "abcdefg " + "y" * 11 + "…"      # 11 back, more than half of 20
    assert clip(f"Look up {LONG}", 40) == f"Look up {LONG[:31]}…"


def test_one_long_word_is_cut_hard():
    assert clip(LONG, 10) == LONG[:9] + "…" and len(clip(LONG, 10)) == 10


def test_what_fits_is_unchanged():
    assert clip(SERVICE, len(SERVICE)) == SERVICE and clip("", 5) == "" and clip("Open", 40) == "Open"


def test_an_accent_written_as_a_mark_of_its_own_stays_with_its_letter_and_the_word_still_ends():
    """Unicode can write é as e followed by a combining accent. The accent belongs to the e before it; the
    space after it still ends the word."""
    decomposed = "one two cafe" + ACUTE + " menu"
    assert clip(decomposed, 14) == "one two cafe" + ACUTE + "…"
    assert clip(decomposed, 13) == "one two…"                                   # never e without its accent


def test_a_word_ends_where_its_kind_of_letter_changes_whether_or_not_an_accent_is_a_mark_of_its_own():
    """A word ends where letters with capitals meet letters without, and where letters meet digits. Text
    arrives with é as one character or as e and a mark, and Thai writes its tone marks, and the vowel signs
    above and below a letter, as marks of their own: whether the word goes on after a mark is its letter's
    to say, not the mark's."""
    assert clip("Safari" + BROWSER + OPENED, 9) == "Safari…"                   # never Safari and a piece of the next
    assert clip("open Safari" + BROWSER + " now", 14) == "open Safari…"
    assert clip("abc123 def", 5) == "abc…"                                      # never 「abc1」
    assert clip("abc123456 xyz", 8) == "abc…"
    assert clip(nfd("caf\u00e9") + BROWSER + OPENED, 7) == nfd("caf\u00e9") + "…"   # not café and a Chinese letter
    assert clip(THAI + "Safari browser", 6) == THAI + "…"                           # not 「Sa」, a piece of Safari
    assert clip(nfd("Chlo\u00e92024.pdf"), 9) == nfd("Chlo\u00e9") + "…"            # not 「Chloé20」
    assert clip(nfd("Jos\u00e9") + BROWSER, 7) == nfd("Jos\u00e9") + "…"
    assert clip(nfd("one caf\u00e9s menu"), 10) == "one…"                           # cafés is one word: not 「café」


def test_the_words_of_a_text_are_the_same_however_its_accents_are_written():
    for text in ("Chlo\u00e92024.pdf", "Jos\u00e9" + BROWSER, "one caf\u00e9s menu", "\u00c9mile " + OPENED):
        assert [unicodedata.normalize("NFC", w) for w in words(nfd(text))] == words(text), text
    assert words(nfd("Chlo\u00e92024.pdf")) == [nfd("Chlo\u00e9"), "2024", "pdf"]
    assert words(nfd("Jos\u00e9") + BROWSER) == [nfd("Jos\u00e9"), BROWSER]
    assert words(THAI + "Safari") == [THAI, "Safari"]


def test_a_mark_taken_on_its_own_is_no_letter_of_any_kind():
    """Two characters alone cannot say what kind of letter a mark sits on. A mark taken for the last letter of
    a word went on into digits and Chinese, so a name ending in a mark read as glued to them where the same
    name written with é stands as a word; taken alone, a mark now carries no word on."""
    assert not joined(ACUTE, "2") and not joined(ACUTE, BROWSER[0]) and not joined(ACUTE, "s")
    assert joined("e", ACUTE) and not joined(ACUTE, " ") and joined(ACUTE, ACUTE)


def test_a_word_cut_hard_keeps_each_letter_with_its_accent():
    accented = ("e" + ACUTE) * 30                                               # one word, far longer than the room
    assert clip(accented, 10) == ("e" + ACUTE) * 4 + "…"                        # not a fifth e without its accent
