"""What the Mac declares about an action, and what the safety floor makes of it.

A key equivalent that prints nothing (Delete, Escape, the arrows, paging) was written into a menu item's label
as the raw control character AppKit reports for it: 109 menu items in 11 apps of this Mac's audit, ten of them
bound to a delete key. It is shown as the menu shows it, never by an English name, which would be a floor word.

A tooltip that names a control counted as the control's own words. The full-screen button is named by its
tooltip, which on this Mac says the button can also "execute" a zoom, in Chinese: a floor word, which raised
the bar the button had to clear from 0.7 to 0.9.

No threshold, release list or bar changes here.
"""

from macwork.engine import Engine
from macwork.observe import observe
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

# The app's own windows as its focused-window snapshot reads them.
STANDARD = {"nodes": [
    {"ref": "w.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Untitled", "depth": 0, "frame": [100, 100, 600, 400]},
    {"ref": "w.1", "role": "AXButton", "rdesc": "button", "title": "Format", "actions": ["AXPress"], "parent": "w.0", "depth": 1},
], "ms": 1, "truncated": False}


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
