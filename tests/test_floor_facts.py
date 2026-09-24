"""What the Mac declares about an action, and what the safety floor makes of it.

A key equivalent that prints nothing (Delete, Escape, the arrows, paging) was written into a menu item's label
as the raw control character AppKit reports for it: 109 menu items in 11 apps of this Mac's audit, ten of them
bound to a delete key. It is shown as the menu shows it, never by an English name, which would be a floor word.

A tooltip that names a control counted as the control's own words. The full-screen button is named by its
tooltip, which on this Mac says the button can also "execute" a zoom, in Chinese: a floor word, which raised
the bar the button had to clear from 0.7 to 0.9.

The floor cached its verdict per app, label and place: `app|label|context`. A key's label was its name alone,
so the first verdict on the Return key in an app served every window of it, every sheet and every field after
it; in this Mac's audit one was formed at 0.58 while Calculator had no readable window and was then used,
unchanged, with the window up. Nothing the Mac declares about where a key goes reached the verdict: not the
window's kind, not the sheet in front, not the button a sheet names for Return, not where the keyboard is.

No threshold, release list or bar changes here, and every split of the verdict cache only makes the floor ask
more. What the classifier is told stays as it was unless policy confirm.declared_facts is turned on.
"""

import json

from macwork.engine import Engine
from macwork.observe import observe
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

# The app's own windows as its focused-window snapshot reads them.
STANDARD = {"nodes": [
    {"ref": "w.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Untitled", "depth": 0, "frame": [100, 100, 600, 400]},
    {"ref": "w.1", "role": "AXButton", "rdesc": "button", "title": "Format", "actions": ["AXPress"], "parent": "w.0", "depth": 1},
], "ms": 1, "truncated": False}
# The same window with a sheet in front of it that names the buttons Return and Escape press (helper 0.2.0)
SHEET = {"nodes": [
    {"ref": "s.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Untitled", "depth": 0, "frame": [100, 100, 600, 400]},
    {"ref": "s.1", "role": "AXButton", "rdesc": "button", "title": "Format", "actions": ["AXPress"], "parent": "s.0", "depth": 1},
    {"ref": "s.2", "role": "AXSheet", "rdesc": "sheet", "parent": "s.0", "depth": 1, "default_button": "s.4", "cancel_button": "s.5"},
    {"ref": "s.3", "role": "AXStaticText", "value": "Delete the selected items?", "parent": "s.2", "depth": 2},
    {"ref": "s.4", "role": "AXButton", "rdesc": "button", "title": "Delete", "actions": ["AXPress"], "parent": "s.2", "depth": 2},
    {"ref": "s.5", "role": "AXButton", "rdesc": "button", "title": "Cancel", "actions": ["AXPress"], "parent": "s.2", "depth": 2},
], "ms": 1, "truncated": False}
# ...and as a helper older than 0.2.0 reads it: the sheet names no button
SHEET_UNNAMED = {**SHEET, "nodes": [{k: v for k, v in n.items() if k not in ("default_button", "cancel_button")} for n in SHEET["nodes"]]}
# the walk was cut (max_nodes, budget_ms) before it reached the window's last children, where a sheet would be
READ_IN_PART = {**STANDARD, "truncated": True}
# the app did not answer: the helper says so on every look after the first timeout
NOT_ANSWERING = {"nodes": [], "ms": 0, "truncated": False, "not_answering": True}
# the look on which an app first times out: an empty tree and nothing else
EMPTY = {"nodes": [], "ms": 1, "truncated": False}
# the app's own window as the window server lists it, which needs no answer from the app
ON_SCREEN = {"pid": 42, "id": 501, "owner": "TextEdit", "title": "Untitled", "layer": 0, "ordinary": True,
             "frame": [100, 100, 600, 400], "alpha": 1, "on_screen": True}


class Mac(FakeHelper):
    """TextEdit (pid 42) in front. `window` is what its focused-window snapshot answers, set by a test before
    each look; `screen` what the window server lists; `focus` what `ax.focused` answers, when a test sets it."""

    def __init__(self, window=STANDARD, screen=(), focus=None, menubar=None):
        super().__init__()
        self.window, self.screen, self.focus, self.menubar = window, list(screen), focus, menubar

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "focused_window":
            self.calls.append((method, p))
            return self.window
        if method == "ax.snapshot" and p.get("scope") == "menubar" and self.menubar is not None:
            self.calls.append((method, p))
            return self.menubar
        if method == "ax.focused" and self.focus is not None:
            self.calls.append((method, p))
            return self.focus
        return super().call(method, timeout, **p)


class Classifier(ScriptedDecider):
    """A floor classifier that notes each action it is asked about and answers with `verdict(state)`: a
    dict of probabilities, the most likely first."""

    def __init__(self, verdict=lambda state: {"navigate": 1.0}):
        super().__init__([])
        self.verdict, self.asked, self.states = verdict, [], []

    def decide(self, state, questions):
        self.calls += 1
        out = {}
        for k in questions:
            probs = self.verdict(state)
            out[k] = {"type": "choice", "choice": next(iter(probs)), "probabilities": dict(probs)}
        self.states.append(state)
        self.asked += [a["action"] for a in state["actions"]] if "actions" in state else [state.get("action")]
        return out


def setup(tmp_path, mac, decider=None, providers=("window", "keys"), **over):
    config = {"observe": {"providers": list(providers), "ax": {"manual_accessibility": "never"}}}
    eng = Engine(cfg(tmp_path, config=config, **over), helper=mac, decider=decider or Classifier())
    ctx = eng._ctx("", {}, None, "t")
    ctx.gate = eng.gate
    return eng, ctx


# --------------------------------------------------------------------------- a key equivalent that prints nothing

def test_a_key_equivalent_that_prints_nothing_is_shown_as_its_glyph(tmp_path):
    """AppKit reports these keys as the characters it uses for them, and the label carried the raw character
    ('(⌘\\x08)'). Apple's glyph instead, as the menu shows it, and never the key's English name: 'delete' in a
    label is a floor word."""
    bar = {"nodes": [
        {"ref": "b.0", "role": "AXMenuBar", "depth": 0},
        {"ref": "b.1", "role": "AXMenuBarItem", "title": "File", "parent": "b.0"},
        {"ref": "b.2", "role": "AXMenu", "parent": "b.1"},
        {"ref": "b.3", "role": "AXMenuItem", "title": "Move to Trash", "cmd": {"char": "\x08", "mods": 0}, "parent": "b.2"},
        {"ref": "b.4", "role": "AXMenuItem", "title": "Delete Immediately", "cmd": {"char": "\x08", "mods": 2}, "parent": "b.2"},
        {"ref": "b.5", "role": "AXMenuBarItem", "title": "Go", "parent": "b.0"},
        {"ref": "b.6", "role": "AXMenu", "parent": "b.5"},
        {"ref": "b.7", "role": "AXMenuItem", "title": "Page Down", "cmd": {"char": "\uf72d", "mods": 8}, "parent": "b.6"},
        {"ref": "b.8", "role": "AXMenuItem", "title": "Single Selection", "cmd": {"char": "\x1b", "mods": 8}, "parent": "b.6"},
        {"ref": "b.9", "role": "AXMenuItem", "title": "Menu Key", "cmd": {"char": "\uf735", "mods": 8}, "parent": "b.6"},
        {"ref": "b.10", "role": "AXMenuItem", "title": "Back", "cmd": {"char": "\x7f", "mods": 0}, "parent": "b.6"},
    ], "ms": 1}
    eng, ctx = setup(tmp_path, Mac(menubar=bar), providers=("menu",))
    labels = {a.target["title"]: a for a in observe(ctx).affordances}

    assert labels["Move to Trash"].label == "menu File ▸ Move to Trash (⌘⌫)"
    assert labels["Delete Immediately"].label == "menu File ▸ Delete Immediately (⌥⌘⌫)"
    assert labels["Page Down"].label == "menu Go ▸ Page Down (⇟)"
    assert labels["Single Selection"].label == "menu Go ▸ Single Selection (⎋)"
    assert labels["Menu Key"].label == "menu Go ▸ Menu Key", "a key the Mac has no glyph for is left out, not written raw"
    for a in labels.values():
        assert all(ch.isprintable() for ch in a.label), repr(a.label)
        assert "delete)" not in a.label.lower() and "escape" not in a.label.lower()
        # the key a quiet wait presses again is still only a printable one: re-pressing ⌥⌘⌫ from the keyboard
        # after a Delete Immediately whose wait saw nothing would delete twice
        assert a.target["combo"] is None
    assert labels["Back"].label == "menu Go ▸ Back (⌘⌫)"
    assert eng._floor_hits(labels["Back"]) == [], "the key's name would have been a floor word the item does not say"


# --------------------------------------------------------------------------- a tooltip is not the control's own words

FULL_SCREEN = {"nodes": [
    {"ref": "f.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Untitled", "depth": 0},
    {"ref": "f.1", "role": "AXButton", "subrole": "AXFullScreenButton", "rdesc": "full screen button",
     "help": "This button can also execute a zoom of the window", "actions": ["AXPress"], "parent": "f.0", "depth": 1},
    {"ref": "f.2", "role": "AXButton", "rdesc": "button", "help": "Move to Trash", "actions": ["AXPress"], "parent": "f.0", "depth": 1},
    {"ref": "f.3", "role": "AXGroup", "rdesc": "group", "help": "Collected items", "actions": ["AXRemoveFromToolbar"],
     "action_desc": {"AXRemoveFromToolbar": "Remove from Toolbar"}, "parent": "f.0", "depth": 1},
    {"ref": "f.4", "role": "AXTable", "parent": "f.0", "depth": 1},
    {"ref": "f.5", "role": "AXRow", "rdesc": "row", "help": "Double-click to execute the saved search", "parent": "f.4", "depth": 2},
    {"ref": "f.6", "role": "AXStaticText", "value": "Weekly report", "parent": "f.5", "depth": 3},
], "ms": 1, "truncated": False}


def tooltip_named(tmp_path, decider, word):
    eng, ctx = setup(tmp_path, Mac(window=FULL_SCREEN), decider)
    obs = observe(ctx)
    return eng, ctx, obs, next(a for a in obs.affordances if word in a.label)


def test_a_tooltip_is_not_the_controls_own_words(tmp_path):
    """Measured on this Mac's audit: the full-screen button, named by its tooltip, had 29 of its 42 recorded
    verdicts gated at the 0.9 bar its tooltip's floor word chose, and would have had none gated at 0.7."""
    eng, ctx, obs, button = tooltip_named(tmp_path, Classifier(lambda state: {"navigate": 0.8, "other": 0.2}), "full screen")

    assert button.label == "full screen button 「This button can also execute a zoom of the window」", "still shown by it"
    assert eng._floor_hits(button) == []
    assert eng._floor("t", ctx, button, obs.window) == [], "navigate 0.8 clears the 0.7 bar of an action no word flagged"
    row = next(a for a in obs.affordances if a.verb == "select")
    assert row.label == "select row 「Double-click to execute the saved search」"
    assert eng._floor_hits(row) == [], "a row named by its tooltip is matched on the text it shows"


def test_a_tooltip_only_control_the_classifier_calls_delete_still_stops(tmp_path):
    """Guard: the classifier reads the label, tooltip and all, and its verdict is not a word hit."""
    eng, ctx, obs, trash = tooltip_named(tmp_path, Classifier(lambda state: {"delete": 0.9, "navigate": 0.1}), "Move to Trash")
    assert eng._floor("t", ctx, trash, obs.window) == ["delete"]


def test_the_apps_own_name_for_an_action_still_counts(tmp_path):
    """Guard: only the tooltip is left out of the words. The action's own name, the app's, is not."""
    eng, ctx, obs, group = tooltip_named(tmp_path, Classifier(), "Remove from Toolbar")
    assert group.label == "Remove from Toolbar group 「Collected items」"
    assert eng._floor_hits(group) == ["delete"]


# --------------------------------------------------------------------------- what a key's label says

def key(obs, name):
    return next(a for a in obs.affordances if a.label.startswith(f"press the {name} key"))


def test_return_and_escape_name_the_buttons_the_sheet_says_they_press(tmp_path):
    eng, ctx = setup(tmp_path, Mac(window=SHEET))
    obs = observe(ctx)
    ret, esc = key(obs, "return"), key(obs, "escape")

    assert obs.notes.get("window_kind") == "AXSheet"
    assert ret.label == "press the return key (default button 「Delete」)"
    assert esc.label == "press the escape key (cancel button 「Cancel」)"
    assert ret.facts["in"] == esc.facts["in"] == "AXSheet"
    assert eng._floor_hits(ret) == ["delete"], "the button it presses is a floor word, and the bar is the high one"
    assert key(obs, "tab").label == "press the tab key", "only what a key triggers is said, and only for that key"

    older = observe(setup(tmp_path, Mac(window=SHEET_UNNAMED))[1])
    assert key(older, "return").label == "press the return key", "a helper that names no button: as before"
    assert key(older, "return").facts["in"] == "AXSheet"

    # a sheet on a sheet: the keys go to the one in front, and a button its key cannot press is not named
    nested = {**SHEET, "nodes": SHEET["nodes"][:2] + [
        {"ref": "n.1", "role": "AXSheet", "parent": "s.0", "depth": 1, "default_button": "n.2"},
        {"ref": "n.2", "role": "AXButton", "rdesc": "button", "title": "Save", "actions": ["AXPress"], "parent": "n.1", "depth": 2},
        {**SHEET["nodes"][2], "parent": "n.1", "depth": 2}] + [{**n, "depth": 3} for n in SHEET["nodes"][3:]]}
    assert key(observe(setup(tmp_path, Mac(window=nested))[1]), "return").label == "press the return key (default button 「Delete」)"
    greyed = {**SHEET, "nodes": [{**n, "enabled": False} if n["ref"] == "s.4" else n for n in SHEET["nodes"]]}
    assert key(observe(setup(tmp_path, Mac(window=greyed))[1]), "return").label == "press the return key"


def test_a_key_in_a_text_field_says_where_the_keyboard_is(tmp_path):
    """By the field's role only: its name is the app's or a web page's text, and a verdict per field name
    would be asked again for every field."""
    field = {"focused": {"role": "AXTextField", "subrole": "AXSearchField", "rdesc": "search text field", "title": "Name", "ref": "f1"},
             "pid": 42, "app": "TextEdit", "bundle_id": "com.apple.TextEdit", "secure_input": False}
    eng, ctx = setup(tmp_path, Mac(focus=field), providers=("window", "keys", "focus"))
    obs = observe(ctx)
    delete = key(obs, "delete")

    assert delete.label == "press the delete key (keyboard in search text field)"
    assert delete.facts["keyboard_on"] == "AXTextField/AXSearchField"
    assert not any("Name" in a.label for a in obs.affordances if a.channel == "keys")

    elsewhere = observe(setup(tmp_path, Mac(focus={**field, "pid": 7, "app": "Finder"}), providers=("window", "keys", "focus"))[1])
    assert key(elsewhere, "delete").label == "press the delete key", "the keyboard is in another app"
    assert "keyboard_on" not in key(elsewhere, "delete").facts


# --------------------------------------------------------------------------- one verdict per place a key goes

def test_a_key_judged_in_one_kind_of_window_is_judged_again_in_another(tmp_path):
    """The sheet here names no button, so the label is the same in both: only where the key goes differs."""
    mac, d = Mac(), Classifier()
    eng, ctx = setup(tmp_path, mac, d)
    for window in (STANDARD, SHEET_UNNAMED, STANDARD):
        mac.window = window
        obs = observe(ctx)
        eng._floor("t", ctx, key(obs, "return"), obs.window)

    assert d.asked == ["press the return key", "press the return key"], \
        "asked once in the window and once with the sheet up; the window's verdict serves the window again"


def test_a_verdict_formed_while_the_window_could_not_be_read_is_not_used_once_it_reads(tmp_path):
    """7ffdee1b's Return was judged at 0.58 while Calculator had no readable window, and that verdict stopped
    the task on the look where the window was up. Blind looks carry the same information as each other and
    share a verdict; a look that reads the window does not use theirs."""
    unsure = {"edit": 0.33, "execute": 0.26, "navigate": 0.25, "send": 0.16}
    d = Classifier(lambda state: unsure if state.get("window") is None else {"navigate": 0.95, "other": 0.05})
    mac = Mac()
    eng, ctx = setup(tmp_path, mac, d)
    seen, keys = [], []
    # the first blind look is known blind by the helper's word alone (nothing of the app on screen yet), the
    # second by the window server's alone
    for window, screen in ((NOT_ANSWERING, []), (STANDARD, [ON_SCREEN]), (EMPTY, [ON_SCREEN]), (STANDARD, [ON_SCREEN])):
        mac.window, mac.screen = window, screen
        obs = observe(ctx)
        keys.append(key(obs, "return"))
        seen.append((eng._floor("t", ctx, keys[-1], obs.window), len(d.asked)))

    assert seen[0] == (["unclassified"], 1), seen[0]
    assert seen[1] == ([], 2), "the blind verdict was used once the window read"
    assert seen[2] == (["unclassified"], 2), "an empty tree with the app's window on screen is a blind look"
    assert seen[3] == ([], 2), "the readable verdict holds"
    blind = "a window that could not be read"
    assert [a.facts["in"] for a in keys] == [blind, "AXWindow/AXStandardWindow", blind, "AXWindow/AXStandardWindow"]


def test_a_window_read_only_in_part_is_not_a_standard_window(tmp_path):
    """A sheet is among the window's last children and the walk reaches it last: a cut walk loses it first."""
    mac, d = Mac(), Classifier()
    eng, ctx = setup(tmp_path, mac, d)
    kinds = []
    for window in (STANDARD, READ_IN_PART):
        mac.window = window
        obs = observe(ctx)
        ret = key(obs, "return")
        eng._floor("t", ctx, ret, obs.window)
        kinds.append((obs.notes.get("window_kind"), getattr(ret, "facts", {}).get("in")))

    assert len(d.asked) == 2, "the standard window's verdict served a window that may have had a sheet up"
    assert kinds == [("AXWindow/AXStandardWindow",) * 2, ("a window read only in part",) * 2]
    cut = {**STANDARD, "nodes": [{**STANDARD["nodes"][0], "more_children": 12}] + STANDARD["nodes"][1:]}
    mac.window = cut
    assert observe(ctx).notes.get("window_kind") == "a window read only in part", "its own last children were cut"


def test_an_app_with_no_window_open_is_not_asked_about_its_keys_again(tmp_path):
    """Guard: splitting verdicts by where a key goes must not make an app that has no window ask again on
    every look. Nothing of it is on screen, and it answers."""
    mac, d = Mac(window=EMPTY), Classifier()
    eng, ctx = setup(tmp_path, mac, d)
    for _ in range(2):
        obs = observe(ctx)
        for name in ("return", "escape"):
            eng._floor("t", ctx, key(obs, name), obs.window)
    assert d.asked == ["press the return key", "press the escape key"]


# --------------------------------------------------------------------------- what the classifier is told

DECLARING = {"nodes": [
    {"ref": "d.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Library", "depth": 0, "frame": [0, 0, 800, 600]},
    {"ref": "d.1", "role": "AXToolbar", "parent": "d.0", "depth": 1},
    {"ref": "d.2", "role": "AXButton", "rdesc": "button", "title": "Share", "ident": "toolbarShare", "actions": ["AXPress"], "parent": "d.1", "depth": 2},
    {"ref": "d.3", "role": "AXTable", "parent": "d.0", "depth": 1},
    {"ref": "d.4", "role": "AXRow", "rdesc": "row", "title": "Alice Smith", "ident": "person-alice-smith", "parent": "d.3", "depth": 2},
    {"ref": "d.5", "role": "AXWebArea", "rdesc": "web area", "parent": "d.0", "depth": 1},
    {"ref": "d.6", "role": "AXButton", "rdesc": "button", "title": "Subscribe", "ident": "subscribe", "actions": ["AXPress"], "parent": "d.5", "depth": 2},
    {"ref": "d.7", "role": "AXSheet", "rdesc": "sheet", "parent": "d.0", "depth": 1},
    {"ref": "d.8", "role": "AXButton", "rdesc": "button", "title": "Erase", "actions": ["AXPress"], "parent": "d.7", "depth": 2},
], "ms": 1, "truncated": False}
EDIT_MENU = {"nodes": [
    {"ref": "e.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "e.1", "role": "AXMenuBarItem", "title": "Edit", "parent": "e.0"},
    {"ref": "e.2", "role": "AXMenu", "parent": "e.1"},
    {"ref": "e.3", "role": "AXMenuItem", "title": "Copy", "ident": "copy:", "cmd": {"char": "C", "mods": 0}, "parent": "e.2"},
], "ms": 1}


def test_the_classifier_is_told_what_the_mac_declares_only_when_asked(tmp_path):
    def declared(state):
        return {a["action"]: a.get("the_mac_declares") for a in state["actions"]}

    on = {"policy": {"confirm": {"declared_facts": True}}}
    d = Classifier()
    eng, ctx = setup(tmp_path, Mac(window=DECLARING, menubar=EDIT_MENU), d, providers=("menu", "window", "keys"), **on)
    obs = observe(ctx)
    _, state, _ = eng._floor_questions(ctx, obs.affordances, obs.window)
    told = declared(state)

    assert told["menu Edit ▸ Copy (⌘C)"] == {"identifier": "copy:"}
    assert told["button 「Share」"] == {"control": "AXButton", "in": "AXWindow/AXStandardWindow"}
    assert told["button 「Erase」"] == {"control": "AXButton", "in": "AXSheet"}
    assert told["select row 「Alice Smith」"] == {"control": "AXRow", "in": "AXWindow/AXStandardWindow"}, \
        "a row's identifier is built from the data it shows, and never leaves the Mac"
    assert told["button 「Subscribe」"] is None, "a web page writes its own roles and identifiers"
    facts = {a.label: a.facts for a in obs.affordances}
    assert facts["button 「Share」"]["identifier"] == "toolbarShare", "kept apart by it, though never sent"
    assert "identifier" not in facts["select row 「Alice Smith」"] and "identifier" not in facts["button 「Subscribe」"]
    assert told["press the return key"] == {"in": "AXSheet"}, "where the keyboard is may be a page's element"
    assert not any(k.startswith("_") for d in told.values() if d for k in d)

    eng._floor("t", ctx, key(obs, "return"), obs.window)
    assert d.states[-1]["the_mac_declares"] == {"in": "AXSheet"}, "a single classification is told the same"

    eng, ctx = setup(tmp_path, Mac(window=DECLARING, menubar=EDIT_MENU), providers=("menu", "window", "keys"))
    obs = observe(ctx)
    _, state, _ = eng._floor_questions(ctx, obs.affordances, obs.window)
    assert not any("the_mac_declares" in a for a in state["actions"]), "off by default"
    assert "the_mac_declares" not in json.dumps(state)
    in_sheet = key(obs, "return")
    ctx.helper.window = STANDARD
    in_window = key(observe(ctx), "return")
    assert in_sheet.label == in_window.label and eng._floor_key(ctx, in_sheet) != eng._floor_key(ctx, in_window), \
        "what is not told is still what keeps the two verdicts apart"


# --------------------------------------------------------------------------- every way a key reaches the floor

def test_a_key_the_planner_suggests_is_judged_where_this_looks_keys_are(tmp_path):
    eng, ctx = setup(tmp_path, Mac(window=SHEET))
    task = eng._new_task("empty the list", {}, None)
    task.tries = [{"keys": "cmd+return"}]
    look = eng._look(task, eng._ctx(task.goal, {}, None, task.id))
    suggested = next(a for a in look.affs if a.target.get("try") == 0)     # the planner's first try, by its marker
    assert suggested.facts.get("in") == "AXSheet", suggested.facts


def test_text_typed_is_judged_in_the_window_it_goes_into(tmp_path):
    """The typed text makes a new action to judge, and it is judged where the field is: in the sheet."""
    tree = {"nodes": [
        {"ref": "t.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Untitled", "depth": 0},
        {"ref": "t.1", "role": "AXSheet", "rdesc": "sheet", "parent": "t.0", "depth": 1},
        {"ref": "t.2", "role": "AXTextField", "rdesc": "text field", "title": "Name", "parent": "t.1", "depth": 2},
    ], "ms": 1, "truncated": False}

    class Typing(ScriptedDecider):
        def __init__(self):
            super().__init__([{"pick": "Name"}, {"pick": "done"}])
            self.floor_states = []

        def decide(self, state, questions):
            if "what" in questions:
                self.floor_states.append(state)
            return super().decide(state, questions)

    d = Typing()
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "keys"], "ax": {"manual_accessibility": "never"}}},
                     policy={"confirm": {"declared_facts": True}}), helper=Mac(window=tree), decider=d)
    eng.do("name it", inputs={"text": "Quarterly"})
    typed = [s for s in d.floor_states if "Quarterly" in str(s.get("action"))]
    assert typed and typed[0].get("the_mac_declares") == {"control": "AXTextField", "in": "AXSheet"}, d.floor_states


def test_a_way_back_is_chosen_among_keys_that_say_what_they_press(tmp_path):
    """Exploring an app backs out with a key the decider picks, vetted by the floor. Return on a sheet whose
    default button deletes is not offered as a way back that changes nothing."""
    class Back:
        calls, cost_usd, last_ms = 0, 0.0, 1.0
        options: dict = {}

        def decide(self, state, questions):
            Back.options = questions["back"]["criteria"]
            return {"back": {"type": "choice", "choice": "none"}}

    eng, ctx = setup(tmp_path, Mac(window=SHEET), Back(), providers=("window",))
    obs = observe(ctx)
    eng._way_back(ctx, obs, "Untitled", "menu File ▸ Clear…")
    offered = list(Back.options.values())
    assert "press the escape key (cancel button 「Cancel」)" in offered, offered
    assert not any(o.startswith("press the return key") for o in offered), "its default button deletes"
