"""English first — and able to run on a Mac in any language.

Two things, and they pull in the same direction. The engine may not *know* any language: whatever it keys a
decision on has to come from the Mac (a menu's own words, an element's identifier), never from a word
written into this repository — a word in the source is a word that is wrong on every Mac set to another
language. And what people read here — code, questions put to the decider, tests — is written in English.

Text in another script is allowed where that script is the subject, and each such place is named below with
its reason. The list is short and is meant to stay that way.
"""

import ast
import pathlib
import re

import pytest

OTHER_SCRIPT = re.compile(r"[぀-ヿ㐀-鿿가-힯Ѐ-ӿ؀-ۿ]")   # CJK, Korean, Cyrillic, Arabic

# Where the engine's own code may hold text in another script, and why.
CODE = {
    "macwork/privacycheck.py": "a synthetic corpus for measuring redaction in many languages: that is its job",
    "macwork/privacy.py": "one carrier sentence per script, so a bare name is tagged in the script it is written in",
    "macwork/facts.py": "a character class: each CJK character is a word",
    "macwork/evals.py": "the eval harness's \"don't save\" buttons, in every language it has been run in",
}
# Defaults that may: the floor's word lists are documented as two languages wide and as a hint, never a
# verdict; the address pattern recognises a Chinese postal address.
DEFAULTS = {"policy.yaml", "privacy.yaml"}

# Tests written before the rule. They simulate the Chinese-language Mac the project began on, and they stay
# as that case. The list may shrink. It does not grow: a new test is written in English (tests/english_mac.py
# is the fake Mac for it) unless another language is the very thing it tests — then name the language here.
LEGACY_TESTS = {
    "test_approval.py", "test_apps.py", "test_canvas.py", "test_capability.py", "test_combos.py", "test_engine.py",
    "test_evals.py", "test_facts.py", "test_flex.py", "test_focus.py", "test_identity.py", "test_lifecycle.py",
    "test_localdecider.py", "test_overlays.py", "test_paths.py", "test_privacy.py", "test_provenance.py",
    "test_queue.py", "test_release.py", "test_retraction.py", "test_revert.py", "test_store.py",
    "test_surfaces.py", "test_ungated.py", "test_watch.py",
}
LANGUAGE_IS_THE_SUBJECT = {
    "test_floor.py": "the safety floor in German, Japanese, French and Russian",
    "test_floor_threshold.py": "the same verdict on a German, a Japanese and an English Mac",
    "test_english_first.py": "this file names the scripts it looks for",
}


def literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """String literals that are code — docstrings and comments are prose about the world, and may quote it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = {id(n.body[0].value) for n in ast.walk(tree)
             if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
             and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose and OTHER_SCRIPT.search(n.value)]


@pytest.mark.parametrize("path", sorted(pathlib.Path("macwork").rglob("*.py")), ids=str)
def test_the_engine_holds_no_word_of_any_language(path):
    if str(path) in CODE:
        return
    found = literals(path)
    assert not found, (f"{path}:{found[0][0]} holds {found[0][1][:40]!r}. A word written here is wrong on every Mac set to "
                       f"another language: ask the Mac for it, or name the file and the reason in CODE")


def test_the_exceptions_are_still_exceptions():
    """An entry that no longer applies is a standing permission nobody is using."""
    for path in CODE:
        assert literals(pathlib.Path(path)), f"{path} no longer holds any such text: take it out of CODE"


@pytest.mark.parametrize("path", sorted(pathlib.Path("macwork/defaults").glob("*.yaml")), ids=str)
def test_what_ships_as_a_default_is_in_english(path):
    """Above all questions.yaml: it is what the decider is asked, on every Mac."""
    if path.name in DEFAULTS:
        return
    lines = [i + 1 for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
             if OTHER_SCRIPT.search(line.split(" #")[0])]
    assert not lines, f"{path.name}:{lines[0]} is not in English"


def test_a_new_test_is_written_in_english():
    allowed = LEGACY_TESTS | set(LANGUAGE_IS_THE_SUBJECT)
    new = sorted(p.name for p in pathlib.Path("tests").glob("*.py")
                 if p.name not in allowed and OTHER_SCRIPT.search(p.read_text(encoding="utf-8")))
    assert not new, (f"{new} hold text in another language. Write them against tests/english_mac.py — or, if the language "
                     f"is what is being tested, say so in LANGUAGE_IS_THE_SUBJECT")


def test_the_legacy_list_only_shrinks():
    gone = sorted(n for n in LEGACY_TESTS if not pathlib.Path("tests", n).exists()
                  or not OTHER_SCRIPT.search(pathlib.Path("tests", n).read_text(encoding="utf-8")))
    assert not gone, f"{gone} are in English now (or gone): take them off LEGACY_TESTS"
