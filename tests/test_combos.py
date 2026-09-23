"""A key combination has to say what it does, or nothing can judge it.

`press cmd+s` contains no word any list has, and a classifier reading that label alone has only the combo
to go on. So both guards were blind to it at once, and an `edit` verdict released a save.

The fix is not a table of well-known shortcuts — that is knowledge about the outside world, and it is
keyboard-layout and app dependent. The app itself publishes the key equivalent of every one of its menu
items, and the menu provider already reads them. So a combo is named by the app's own menu item, in the
app's own words: `press cmd+s (File ▸ Save…)` on one Mac, `(Ablage ▸ Sichern…)` on another.

This also retires a claim I made and could not back: `release_only_for.edit` allowlisted `channel:keys`
and `channel:pointer` as though the channel were a *fact* about what the action touches. A combo can be
anything and a coordinate can be anything; neither carries a fact.
"""

from typing import Any

import pytest

from macwork.config import Config
from macwork.engine import Engine
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, canonical_combo, menu, named_combo
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

MENU = [
    Affordance("m1", "menu", "press", "menu File ▸ Save… (⌘S)", {"combo": "cmd+s", "path": "File ▸ Save…"}, context="File"),
    Affordance("m2", "menu", "press", "menu Edit ▸ Undo (⌘Z)", {"combo": "cmd+z", "path": "Edit ▸ Undo"}, context="Edit"),
]


def obs_with_menu():
    return Observation(app={"pid": 1, "name": "X"}, window="w", affordances=list(MENU))


# --------------------------------------------------------------------------- naming

def test_a_combo_is_named_by_the_app_s_own_menu_item():
    assert named_combo(obs_with_menu(), "cmd+s") == "File ▸ Save…"


def test_case_and_spacing_do_not_matter():
    assert named_combo(obs_with_menu(), "CMD+S") == named_combo(obs_with_menu(), "cmd+s")


def test_a_combo_no_menu_item_has_is_left_alone():
    """No guessing: if the app does not publish it, nothing here knows what it does."""
    assert named_combo(obs_with_menu(), "cmd+shift+7") is None


def test_nothing_is_named_when_the_menu_was_not_read():
    assert named_combo(Observation(app=None, window=None, affordances=[]), "cmd+s") is None


# --------------------------------------------------------------------------- one key, however it is written

@pytest.mark.parametrize("said, key", [
    ("cmd + shift + g", "cmd+shift+g"), ("command+n", "cmd+n"), ("⌘O", "cmd+o"), ("⇧⌘G", "cmd+shift+g"),
    ("shift+cmd+g", "cmd+shift+g"), ("⌥⌘N", "cmd+alt+n"), ("ctrl+option+command+fn+k", "cmd+ctrl+alt+fn+k"),
    ("Return", "return"), ("the return key", "return"), ("press the escape key", "escape"), ("enter", "return"),
    ("⌫", "delete"), ("⌘⌫", "cmd+delete"), ("⌘↘", "cmd+end"), ("F20", "f20"),
    ("fn+delete", "fn+delete"),                     # fn is never dropped: fn+delete is another key than delete
    ("cmd++", None), ("cmd+", None), ("cmd+shift", None), ("f21", None),      # nothing the helper could split
    ("a", None),                                    # a character alone is typing, not a key
    ("menu item 「General」", None), ("rm -rf /", None), ("Command-N", None),
])
def test_key_strings_are_read_the_way_the_keyboard_reads_them(said, key):
    assert canonical_combo(said) == key


def test_a_key_written_in_another_order_names_the_same_menu_item(tmp_path):
    """The menu shows ⇧⌘P and stores cmd+shift+p; a planner writes shift+cmd+p. It is one key."""
    obs = Observation(app=None, window=None, affordances=[])
    menu(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "TextEdit"}), obs)
    assert named_combo(obs, "shift+cmd+p") == named_combo(obs, "⇧⌘P") == "File ▸ Export ▸ PDF"
    assert named_combo(obs, "cmd+alt+p") is None


def test_a_menu_item_knows_its_place_and_keeps_its_label(tmp_path):
    obs = Observation(app=None, window=None, affordances=[])
    menu(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "TextEdit"}), obs)
    pdf = next(a for a in obs.affordances if a.target.get("title") == "PDF")
    assert pdf.target["menu_path"] == ["File", "Export", "PDF"] and pdf.target["path"] == "File ▸ Export ▸ PDF"
    assert pdf.label == "menu File ▸ Export ▸ PDF (⇧⌘P)"


# --------------------------------------------------------------------------- what it buys

def test_a_planner_suggested_save_now_reaches_the_word_list(tmp_path):
    """The point of naming it: `press cmd+s` matched no pattern, `Save` does."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    bare = Affordance("t0", "keys", "key", "press cmd+s", {"combo": "cmd+s"})
    named = Affordance("t0", "keys", "key", "press cmd+s (File ▸ Save…)", {"combo": "cmd+s"})
    assert eng._floor_hits(bare) == []
    assert eng._floor_hits(named) == ["write"]


def test_the_suggestion_carries_the_name(tmp_path):
    from macwork.model import Task
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="save it", id="t1")
    task.tries = [{"keys": "cmd+s"}]
    offered = eng._suggested(task, obs_with_menu())
    assert offered and "Save" in offered[0].label, offered[0].label if offered else "nothing offered"


def test_an_unknown_combo_is_still_offered(tmp_path):
    """Naming is extra information, never a filter — an app may implement a key with no menu item."""
    from macwork.model import Task
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="x", id="t2")
    task.tries = [{"keys": "cmd+shift+7"}]
    assert len(eng._suggested(task, obs_with_menu())) == 1


# --------------------------------------------------------------------------- the claim I could not back

def test_edit_does_not_release_a_bare_coordinate():
    """`release_only_for` is for facts about an action. A click at x,y is not one: the text OCR read
    beside it may be wrong, and the thing under it may be Send."""
    allowed = (Config.load().policy["confirm"]["release_only_for"] or {}).get("edit") or []
    assert "channel:pointer" not in allowed
