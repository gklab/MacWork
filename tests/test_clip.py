"""A cut ends where a word ends.

The engine cuts text to fit: a 'look into' summary cut each label at 40 characters. Cut inside a word, what is
left reads like a word of its own — 「… a Service of Saf」 was tagged a person, and 「Saf」 went out as a pseudonym
inside 「Safari」. So a cut steps back to where a word ends, but only so far: a word longer than half the room
is cut where the room ends, rather than giving up more than half of what would fit.
"""

from macwork.model import clip

SERVICE = "a Service of Safari on this Mac"
LONG = "pneumonoultramicroscopicsilicovolcanoconiosis"


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
    decomposed = "one two café menu"
    assert clip(decomposed, 14) == "one two café…"
    assert clip(decomposed, 13) == "one two…"                                   # never e without its accent
