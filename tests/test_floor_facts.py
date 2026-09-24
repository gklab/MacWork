"""What the Mac declares about an action, and what the safety floor makes of it.

A key equivalent that prints nothing (Delete, Escape, the arrows, paging) was written into a menu item's label
as the raw control character AppKit reports for it: 109 menu items in 11 apps of this Mac's audit, ten of them
bound to a delete key. It is shown as the menu shows it, never by an English name, which would be a floor word.

A tooltip that names a control counted as the control's own words. The full-screen button is named by its
tooltip, which on this Mac says the button can also "execute" a zoom, in Chinese: a floor word, which raised
the bar the button had to clear from 0.7 to 0.9. Its subrole names it too, and where something else names a
control its tooltip is left out of the words; a control whose only name is its tooltip is called by it, and its
words count.

The floor cached its verdict per app, label and place: `app|label|context`. A key's label was its name alone,
so the first verdict on the Return key in an app served every window of it, every sheet and every field after
it; in this Mac's audit one was formed at 0.58 while Calculator had no readable window and was then used,
unchanged, with the window up. Nothing the Mac declares about where a key goes reached the verdict: not the
window's kind, not the sheet in front, not the button a sheet names for Return, not where the keyboard is.

No threshold or release list changes here. Which bar applies changes for one kind of control only: one named by
a tooltip that something else also names, whose tooltip's words no longer choose the 0.9 bar. Every split of the
verdict cache makes the floor ask more, and a question it gated stays gated under facts the classifier is not
told, so a split never asks a gate into a release. What the classifier reads changes whatever policy
confirm.declared_facts says: the label it reads is the decider's, and a plain key's label now names the button
Return or Escape presses and the kind of field the keyboard is in, and a key equivalent that prints nothing is
shown by its glyph. The switch covers `the_mac_declares` alone. A routine finds a key by its label, and compares
it without what the engine says there about where the key goes.
"""

import json

from macwork.engine import Engine
from macwork.observe import observe
from macwork.skills import Skills
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


class ByAction(Classifier):
    """A floor classifier whose answer to each question turns on that question as it reads it, with the action
    it is about written in: `verdict(question)`, alone or in a batch."""

    def decide(self, state, questions):
        self.calls += 1
        out = {}
        for k, q in questions.items():
            probs = self.verdict(str(q.get("instructions", "")))
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
    verdicts gated at the 0.9 bar its tooltip's floor word chose, and would have had none gated at 0.7. Its
    subrole names it as well, and a row shows its own text."""
    eng, ctx, obs, button = tooltip_named(tmp_path, Classifier(lambda state: {"navigate": 0.8, "other": 0.2}), "full screen")

    assert button.label == "full screen button 「This button can also execute a zoom of the window」", "still shown by it"
    assert eng._floor_hits(button) == []
    assert eng._floor("t", ctx, button, obs.window) == [], "navigate 0.8 clears the 0.7 bar of an action no word flagged"
    row = next(a for a in obs.affordances if a.verb == "select")
    assert row.label == "select row 「Double-click to execute the saved search」"
    assert eng._floor_hits(row) == [], "a row named by its tooltip is matched on the text it shows"


def test_a_control_named_only_by_its_tooltip_is_matched_on_it(tmp_path, monkeypatch):
    """An icon button with no title, description or value is called by its tooltip, and that is the only word for
    what it does. Left out of the words, 'Move to Trash' there had the 0.7 bar of an action nothing flagged to
    clear, and `_risky` let it into what exploring an app presses, where no floor verdict is asked for."""
    eng, ctx, obs, trash = tooltip_named(tmp_path, Classifier(lambda state: {"navigate": 0.8, "delete": 0.1, "send": 0.1}),
                                         "Move to Trash")
    assert trash.label == "button 「Move to Trash」" and "floor_text" not in trash.target
    assert eng._floor_hits(trash) == ["delete"]
    assert eng._risky(trash, ctx), "not yet classified, it is judged by its words"
    assert eng._floor("t", ctx, trash, obs.window) == ["delete"], "navigate 0.8 does not clear the 0.9 bar of a flagged action"
    shows_nothing = {**FULL_SCREEN, "nodes": FULL_SCREEN["nodes"][:5] + [
        {"ref": "f.7", "role": "AXRow", "rdesc": "row", "help": "Erase this disk", "parent": "f.4", "depth": 2}]}
    row = next(a for a in observe(setup(tmp_path, Mac(window=shows_nothing))[1]).affordances if a.verb == "select")
    assert row.label == "select row 「Erase this disk」" and eng._floor_hits(row) == ["delete"], "a row that shows no text"

    import macwork.learn
    monkeypatch.setattr(macwork.learn.time, "sleep", lambda s: None)
    icons = {"nodes": [
        {"ref": "i.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Library", "depth": 0, "frame": [0, 0, 800, 600]},
        {"ref": "i.1", "role": "AXButton", "rdesc": "button", "help": "Delete", "actions": ["AXPress"], "parent": "i.0", "depth": 1},
        {"ref": "i.2", "role": "AXButton", "rdesc": "button", "title": "Info", "actions": ["AXPress"], "parent": "i.0", "depth": 1},
    ], "ms": 1, "truncated": False}
    mac = Mac(window=icons, menubar={"nodes": [], "ms": 1})
    eng = Engine(cfg(tmp_path, config={"learn": {"max_actions": 5, "budget_s": 20}}), helper=mac,
                 decider=ScriptedDecider([]))                  # judges everything it is asked about safe to explore
    eng.learn("TextEdit")
    pressed = [p.get("ref") for m, p in mac.calls if m == "ax.perform"]
    assert "i.2" in pressed and "i.1" not in pressed, pressed


def classify_in_a_batch(eng, ctx, affs, window):
    """What a look does with its actions: one request of their own, through the gate, answers kept."""
    questions, state, mapping = eng._floor_questions(ctx, affs, window)
    eng._floor_answers(ctx.gate.decide(eng.redactor("t"), state, questions, task="t"), mapping)


def test_a_tooltip_only_control_the_classifier_calls_delete_still_stops(tmp_path):
    """Guard: the classifier reads the label, tooltip and all, alone and in a batch, and its verdict is not a
    word hit. It answers from what it is asked: delete only where the question holds the tooltip."""
    def reads(question):
        return {"delete": 0.9, "navigate": 0.1} if "Move to Trash" in question else {"navigate": 0.95, "other": 0.05}

    for batch in (False, True):
        d = ByAction(reads)
        eng, ctx, obs, trash = tooltip_named(tmp_path, d, "Move to Trash")
        if batch:
            classify_in_a_batch(eng, ctx, [trash], obs.window)
        assert eng._floor("t", ctx, trash, obs.window) == ["delete"], "in a batch" if batch else "alone"
        assert d.asked == ["button 「Move to Trash」"], d.asked


def test_the_classifier_reads_a_tooltip_the_words_leave_out(tmp_path):
    """Guard: where something else names a control — the description its subrole gives, the text a row shows —
    its tooltip is left out of the floor's words, and the classifier still reads it, alone and in a batch. For
    such a control that is the one thing that can stop it, so the fake here calls it destructive only where the
    question it is asked holds the tooltip."""
    def reads(question):
        return {"execute": 0.9, "navigate": 0.1} if "execute" in question else {"navigate": 0.95, "other": 0.05}

    for batch in (False, True):
        d = ByAction(reads)
        eng, ctx = setup(tmp_path, Mac(window=FULL_SCREEN), d)
        obs = observe(ctx)
        named = [next(a for a in obs.affordances if "full screen" in a.label), next(a for a in obs.affordances if a.verb == "select")]
        assert [eng._floor_hits(a) for a in named] == [[], []], "the tooltip's 'execute' is not the controls' own word"
        if batch:
            classify_in_a_batch(eng, ctx, named, obs.window)
        for a in named:
            assert eng._floor("t", ctx, a, obs.window) == ["execute"], ("in a batch" if batch else "alone", a.label)
        assert d.asked == [a.label for a in named], d.asked


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
    """By the field's role and subrole only: its name is the app's or a web page's text, and a verdict per field
    name would be asked again for every field."""
    field = {"focused": {"role": "AXTextField", "subrole": "AXSearchField", "rdesc": "search text field", "title": "Name", "ref": "f1"},
             "pid": 42, "app": "TextEdit", "bundle_id": "com.apple.TextEdit", "secure_input": False}
    eng, ctx = setup(tmp_path, Mac(focus=field), providers=("window", "keys", "focus"))
    obs = observe(ctx)
    delete = key(obs, "delete")

    assert delete.label == "press the delete key (keyboard in search field)"
    assert delete.facts["keyboard_on"] == "AXTextField/AXSearchField"
    assert not any("Name" in a.label for a in obs.affordances if a.channel == "keys")
    area = observe(setup(tmp_path, Mac(focus={**field, "focused": {"role": "AXTextArea", "rdesc": "text", "ref": "f2"}}),
                         providers=("window", "keys", "focus"))[1])
    assert key(area, "delete").label == "press the delete key (keyboard in text area)", "a role with no subrole"

    elsewhere = observe(setup(tmp_path, Mac(focus={**field, "pid": 7, "app": "Finder"}), providers=("window", "keys", "focus"))[1])
    assert key(elsewhere, "delete").label == "press the delete key", "the keyboard is in another app"
    assert "keyboard_on" not in key(elsewhere, "delete").facts


# a web page's search field whose author set aria-roledescription, which WebKit and Chromium hand to Accessibility
# as the field's AXRoleDescription
PAGE_SAYS = "search box: return only filters this list"
CHECKOUT = {"nodes": [
    {"ref": "p.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Checkout", "depth": 0, "frame": [0, 0, 800, 600]},
    {"ref": "p.1", "role": "AXWebArea", "rdesc": "HTML content", "parent": "p.0", "depth": 1},
    {"ref": "p.2", "role": "AXButton", "rdesc": "button", "title": "Place order", "actions": ["AXPress"], "parent": "p.1", "depth": 2},
], "ms": 1, "truncated": False}
IN_THE_PAGE = {"focused": {"role": "AXTextField", "rdesc": PAGE_SAYS, "title": "q", "ref": "p.3"},
               "pid": 42, "app": "TextEdit", "bundle_id": "com.apple.TextEdit", "secure_input": False}


def test_a_page_does_not_write_what_a_key_says(tmp_path):
    """A key is the keyboard's, and its label is the engine's words and the Mac's constants. Where the keyboard
    is was said by the focused field's role description, which a page writes: its words reached the label the
    decider chooses from and the floor classifier reads, alone and in a batch, for Return in a web form, which
    submits it."""
    d = Classifier()
    eng, ctx = setup(tmp_path, Mac(window=CHECKOUT, focus=IN_THE_PAGE), d, providers=("window", "keys", "focus"))
    obs = observe(ctx)
    keys = [a for a in obs.affordances if a.channel == "keys"]
    ret = key(obs, "return")

    assert ret.label == "press the return key (keyboard in text field)"
    assert not any(PAGE_SAYS in a.label or PAGE_SAYS in eng._floor_key(ctx, a) for a in keys)
    _, state, _ = eng._floor_questions(ctx, keys, obs.window)
    eng._floor("t", ctx, ret, obs.window)
    assert d.states[-1]["action"] == ret.label
    assert PAGE_SAYS not in json.dumps(state, ensure_ascii=False) and PAGE_SAYS not in json.dumps(d.states, ensure_ascii=False)


def test_a_routine_finds_a_key_wherever_the_keyboard_is(tmp_path):
    """A key has no identity but its label, and its label says the button it presses and where the keyboard is:
    a routine step 'press the return key' was not found with the keyboard in a field, and the replay stopped
    there. It is the same key, judged by the floor on the label it has here."""
    field = {"focused": {"role": "AXTextField", "subrole": "AXSearchField", "ref": "f1"}, "pid": 42, "app": "TextEdit",
             "bundle_id": "com.apple.TextEdit", "secure_input": False}
    in_field = observe(setup(tmp_path, Mac(focus=field), providers=("window", "keys", "focus"))[1])
    in_sheet = observe(setup(tmp_path, Mac(window=SHEET))[1])
    step = {"channel": "keys", "verb": "key", "label": "press the return key", "key": "", "context": "", "inputs": []}

    assert key(in_field, "return").label == "press the return key (keyboard in search field)"
    assert Skills.find(step, in_field) is key(in_field, "return")
    assert Skills.find({**step, "label": "press the return key (default button 「Delete」)"}, in_field) is key(in_field, "return")
    assert Skills.find(step, in_sheet) is key(in_sheet, "return")
    assert Skills.find({**step, "label": "press the escape key"}, in_field) is key(in_field, "escape"), "another key"


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


def focused_on(role):
    return {"focused": {"role": role, "ref": "x"}, "pid": 42, "app": "TextEdit", "bundle_id": "com.apple.TextEdit",
            "secure_input": False}


def test_a_key_the_floor_gated_is_not_asked_through_by_a_fact_the_classifier_is_not_told(tmp_path):
    """The keyboard moved from a button to a table: where Return goes is keyed on that, and the classifier is not
    told it, so it was asked the very question it had answered, and its second answer released what its first
    had gated. A verdict near the bar was sampled until it passed. Gated, the question stays gated under every
    fact it is not told, the ones it was released under before included; told of another window, it is another
    question."""
    unsure, sure, other = {"navigate": 0.6, "other": 0.4}, {"navigate": 0.95, "other": 0.05}, {"other": 0.55, "navigate": 0.45}
    for answers, roles, want in (([unsure, sure], ("AXButton", "AXTable"), [["unclassified"]] * 2),
                                 ([other, sure], ("AXButton", "AXTable"), [["other"]] * 2),
                                 ([sure, unsure], ("AXButton", "AXTable", "AXButton"), [[], ["unclassified"], ["unclassified"]])):
        said = iter(answers + [sure])
        d = Classifier(lambda state: next(said))
        mac = Mac()
        eng, ctx = setup(tmp_path, mac, d, providers=("window", "keys", "focus"))
        seen = []
        for role in roles:
            mac.focus = focused_on(role)
            obs = observe(ctx)
            seen.append(eng._floor("t", ctx, key(obs, "return"), obs.window))
        assert d.asked == ["press the return key"] * 2 and d.states[0] == d.states[1], "asked again, the same question"
        assert seen == want, "an answer to the same question released what the floor had gated"

    # the last run goes on in a window of another title, with a sheet up: told of that window, a question of its own
    mac.window = {**SHEET_UNNAMED, "nodes": [{**SHEET_UNNAMED["nodes"][0], "title": "Notes"}] + SHEET_UNNAMED["nodes"][1:]}
    obs = observe(ctx)
    assert eng._floor("t", ctx, key(obs, "return"), obs.window) == [] and len(d.asked) == 3, "told of another window"


def test_each_thing_the_mac_declares_keeps_a_verdict_apart(tmp_path):
    """Every fact the Mac declares about an action is part of its verdict key, and only where a key goes and where
    the keyboard is were shown to split it: a control of another kind or with another identifier under the same
    name, and a key in a window that shows a file, are each asked about again."""
    top = {"ref": "v.0", "role": "AXWindow", "subrole": "AXStandardWindow", "title": "Notes", "depth": 0}
    share = {"ref": "v.1", "role": "AXButton", "rdesc": "button", "title": "Share", "ident": "toolbarShare",
             "actions": ["AXPress"], "parent": "v.0", "depth": 1}

    def window(button=None, **declared):
        return {"nodes": [{**top, **declared}, {**share, **(button or {})}], "ms": 1, "truncated": False}

    mac, d = Mac(), Classifier()
    eng, ctx = setup(tmp_path, mac, d)
    for tree in (window(), window({"ident": "toolbarShareMenu"}), window({"subrole": "AXToolbarButton"}),
                 window(document="file:///Users/me/Notes.rtf")):
        mac.window = tree
        obs = observe(ctx)
        for a in (next(a for a in obs.affordances if a.label == "button 「Share」"), key(obs, "return")):
            eng._floor("t", ctx, a, obs.window)

    assert d.asked == ["button 「Share」", "press the return key", "button 「Share」", "button 「Share」", "press the return key"]


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
    {"ref": "d.9", "role": "AXTextField", "subrole": "AXSearchField", "rdesc": "search text field", "title": "Search this site",
     "parent": "d.5", "depth": 2},
], "ms": 1, "truncated": False}
# the keyboard is in the page's search field
IN_PAGE_FIELD = {"focused": {"role": "AXTextField", "subrole": "AXSearchField", "rdesc": "search text field", "ref": "d.9"},
                 "pid": 42, "app": "TextEdit", "bundle_id": "com.apple.TextEdit", "secure_input": False}
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
    eng, ctx = setup(tmp_path, Mac(window=DECLARING, menubar=EDIT_MENU, focus=IN_PAGE_FIELD), d,
                     providers=("menu", "window", "keys", "focus"), **on)
    obs = observe(ctx)
    _, state, _ = eng._floor_questions(ctx, obs.affordances, obs.window)
    told = declared(state)
    ret = key(obs, "return")

    assert told["menu Edit ▸ Copy (⌘C)"] == {"identifier": "copy:"}
    assert told["button 「Share」"] == {"control": "AXButton", "in": "AXWindow/AXStandardWindow"}
    assert told["button 「Erase」"] == {"control": "AXButton", "in": "AXSheet"}
    assert told["select row 「Alice Smith」"] == {"control": "AXRow", "in": "AXWindow/AXStandardWindow"}, \
        "a row's identifier is built from the data it shows, and never leaves the Mac"
    assert told["button 「Subscribe」"] is None, "a web page writes its own roles and identifiers"
    facts = {a.label: a.facts for a in obs.affordances}
    assert facts["button 「Share」"]["identifier"] == "toolbarShare", "kept apart by it, though never sent"
    assert "identifier" not in facts["select row 「Alice Smith」"] and "identifier" not in facts["button 「Subscribe」"]
    assert ret.facts["keyboard_on"] == "AXTextField/AXSearchField", "kept apart by where the keyboard is"
    assert told[ret.label] == {"in": "AXSheet"}, "where the keyboard is may be a page's element: never sent"
    assert not any(k.startswith("_") for d in told.values() if d for k in d)

    eng._floor("t", ctx, ret, obs.window)
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
