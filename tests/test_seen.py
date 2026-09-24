"""What the engine could not see, said out loud; and what an action could not know, checked before it.

* the screen text is one pool with a cap, and three providers cut it to the cap as they wrote, silently:
  a prompt read after a long window lost its words, and the decider was told nothing was missing;
* windows from other processes were counted one per process, so a second dialog from the same one was
  invisible;
* typing was offered while a password field had the keyboard, where the helper refuses every keystroke;
* an empty Accessibility tree read as "no window", also when the helper simply had no permission to look;
* a click on a point read from the screen landed wherever that point was now, and the clipboard channel
  reported success whether or not the pasteboard had changed;
* a window that changes by itself — a clock — "reacted" to every action;
* what a step drew in a window the tree describes — a panel the tree never hears of — was read and dropped, or
  not read at all, and the step was said to have changed the picture and no text.
"""

import base64

from macwork.act import CHANNELS, Outcome
from macwork.contract import kept
from macwork.engine import Engine
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, get_provider
from tests.test_canvas import Helper as CanvasHelper, box, ctx as canvas_ctx, obs_with
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_overlays import APP, Helper as WindowsHelper, look, node, win


def test_what_the_cap_cut_is_said(tmp_path):
    prompt = win(8, "System Settings", [150, 150, 200, 120])
    h = WindowsHelper([prompt, APP], {8: [node("Allow"), node("Deny")]})
    obs = look(h, **{})
    assert "screen_text_cut" not in obs.notes, "room enough: nothing to say"
    from macwork.config import Config
    c = Config.load(overrides={"config": {"observe": {"window": {"screen_text_chars": 30}}}})
    ctx = Ctx(cfg=c, helper=h, app={"pid": 1, "name": "Editor"}, goal="", inputs={}, task="t", gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    obs.screen_text = "x" * 25
    get_provider("overlays")(ctx, obs)
    assert len(obs.screen_text) == 30 and obs.notes["screen_text_cut"] > 0
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert "characters" in eng._evidence(obs)["screen_text_cut_short_by"]


def test_two_dialogs_from_one_process_are_two():
    a = {**win(8, "System Settings", [150, 150, 200, 120]), "id": 501}
    b = {**win(8, "System Settings", [400, 150, 200, 120]), "id": 502}
    obs = look(WindowsHelper([a, b, APP], {8: [node("Allow")]}))
    assert len(obs.notes["covered_by"]) == 2
    obs = look(WindowsHelper([a, dict(a), APP], {8: [node("Allow")]}))
    assert len(obs.notes["covered_by"]) == 1, "the same window listed twice is one window"


def test_typing_is_not_offered_into_a_password_field(tmp_path):
    h = FakeHelper()
    h.secure_input = True
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "focus", "typing"]}}), helper=h, decider=ScriptedDecider([]))
    seen = eng.observe(inputs={"text": "hello"})
    assert not any(a["channel"] == "keys" and a["verb"] == "type" for a in seen["affordances"])
    assert "password" in seen["notes"]["typing_withheld"]


def test_no_permission_is_not_no_window(tmp_path):
    class Blind(FakeHelper):
        ax_trusted = False

        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot":
                return {"nodes": [], "ms": 1}
            return super().call(method, timeout, **p)

    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window"]}}), helper=Blind(), decider=ScriptedDecider([]))
    seen = eng.observe()
    assert seen["notes"]["ax_trusted"] is False
    obs = Observation(app=None, window=None, affordances=[])
    obs.notes["ax_trusted"] = False
    assert "doctor" in eng._evidence(obs)["accessibility"]


def test_a_point_read_from_the_screen_remembers_its_window_and_is_checked_against_it():
    o = obs_with(frame=[0, 0, 400, 400])
    get_provider("vision")(canvas_ctx(CanvasHelper([box("Start", 10, 10)]), vision={"mode": "always"}), o)
    click = next(a for a in o.affordances if a.channel == "pointer" and a.verb == "click")
    assert click.target["window_frame"] == [0, 0, 400, 400]

    class Moved(CanvasHelper):
        def __init__(self, frame):
            super().__init__()
            self.frame = frame

        def call(self, method, timeout=30.0, **p):
            if method == "screen.windows":
                return [{"pid": 1, "frame": self.frame}]
            return super().call(method, timeout, **p)

    moved = Moved([0, 120, 400, 400])
    out = CHANNELS["pointer"](canvas_ctx(moved), click, {})
    assert not out.ok and "moved" in out.error and not moved.did("input.click")
    still = Moved([1, 0, 400, 401])
    assert CHANNELS["pointer"](canvas_ctx(still), click, {}).ok and still.did("input.click")


def test_the_clipboard_says_whether_the_write_took(tmp_path):
    h = FakeHelper()
    c = Ctx(cfg=cfg(tmp_path), helper=h, app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    a = Affordance("c0", "clipboard", "write", "put text on the clipboard", {})
    assert CHANNELS["clipboard"](c, a, {"text": "hi"}).ok
    h.clipboard_stuck = True
    out = CHANNELS["clipboard"](c, a, {"text": "hi"})
    assert not out.ok and "did not change" in out.error


def test_a_window_that_moves_by_itself_is_no_witness(tmp_path):
    """Its events are raised for everything; a press there kept its promise only if something else moved."""
    from macwork.model import Step
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("start it", {}, None)
    obs = Observation(app={"pid": 42, "name": "Clock"}, window="World Clock", affordances=[])
    obs.screen_text = "12:00"

    def press_then_look(ambient):
        eng.cache["ambient"] = {"42|World Clock"} if ambient else set()
        task.steps.append(Step(len(task.steps), "button 「Start」", ok=True, events=["AXValueChanged"], promise="changed"))
        task.prev = {"sig": "sigA", "label": "button 「Start」", "handle": "button 「Start」", "ok": True, "events": ["AXValueChanged"],
                     "app": obs.app, "screen": "12:00", "window": "World Clock", "unseen": False, "glance": None}
        eng._learn_from_prev(task, obs.app, "sigA", obs)
        return task.steps[-1].kept

    assert press_then_look(ambient=True) is False, "an event on a window that moves by itself is not a change"
    assert press_then_look(ambient=False) is True


def test_a_picture_that_changed_where_no_text_did_is_read_by_sight_at_once(tmp_path):
    """A Qt app's About panel: "the picture changed (3% of it, at the bottom left); no text on screen did",
    and the engine had nothing to read it with. What the tree cannot say, the screen can."""
    from macwork.model import Step
    from tests.english_mac import EnglishMac

    class Painted(EnglishMac):
        def call(self, method, timeout=30.0, **p):
            if method == "screen.ocr":
                self.calls.append((method, p))
                return {"boxes": [{"text": "Version 10.9.12865", "frame": [20, 300, 120, 14]}], "frame": [0, 0, 400, 400], "ms": 5}
            if method == "screen.capture":
                return {"ok": True}
            return super().call(method, timeout, **p)

    h = Painted()
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "vision"]}}), helper=h, decider=ScriptedDecider([]))
    task = eng._new_task("which version is this?", {}, None)
    task.steps.append(Step(n=0, action="menu Help ▸ About", ok=True, events=[],
                           outcome="the picture in the window changed (3% of it, at the bottom left); no text on screen did",
                           change={"picture_only": True, "picture_share": 0.03, "picture_where": "at the bottom left"}))
    look = eng._look(task, eng._step_context(task))
    assert h.did("screen.ocr"), "the screen was read on this very look, not a step later"
    assert "10.9.12865" in look.obs.screen_text


def test_a_fingerprint_taken_a_moment_ago_is_the_baseline_an_action_moves_from(tmp_path):
    """A step took three, and the last two were of one screen, milliseconds apart."""
    import time as _time
    h = FakeHelper()
    eng = Engine(cfg(tmp_path, config={"engine": {"verify": {"fingerprint_reuse_ms": 200}}}), helper=h, decider=ScriptedDecider([]))
    c = eng._step_context(eng._new_task("x", {}, None))
    assert eng._recent_fingerprint(c) is None, "nothing taken yet"
    fp = eng._fingerprint(c)
    n = len(h.did("ax.fingerprint"))
    assert eng._recent_fingerprint(c) == fp and len(h.did("ax.fingerprint")) == n, "no second round trip"
    _time.sleep(0.25)
    assert eng._recent_fingerprint(c) is None, "too old to stand in for the screen now"


def test_the_glance_is_taken_while_the_tree_is_read(tmp_path):
    """A screen capture and Accessibility round trips share nothing but the app, and were taken one after
    the other on every look."""
    import threading

    class Glancing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "screen.glance":
                self.calls.append((method, p, threading.current_thread().name))
                return {"cells": [0] * 4, "frame": p.get("near")}
            return super().call(method, timeout, **p)

    h = Glancing()
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "sight"]}}), helper=h, decider=ScriptedDecider([]))
    seen = eng.observe()
    assert seen["notes"]["glance"]["cells"] == [0] * 4 and "_glance" not in seen["notes"]
    glances = [c for c in h.calls if c[0] == "screen.glance"]
    assert len(glances) == 1 and glances[0][2] == "glance", "on its own thread, begun with the look"
    eng.cache["window_frame_by_pid"] = {42: [10, 20, 300, 200]}       # what the window provider records once it has seen one
    eng.observe()
    assert [c for c in h.calls if c[0] == "screen.glance"][-1][1]["near"] == [10, 20, 300, 200], "the last window frame seen for this app"

    eng2 = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "sight"], "sight": {"overlap": False}}}), helper=Glancing(),
                  decider=ScriptedDecider([]))
    assert "glance" in eng2.observe()["notes"], "the serial path still works"


# ----------------------------------------------------------------- what a step drew where the tree says nothing
N = 8                  # the fake's glance: 8×8 cells of a window at [0, 0, 800, 600], 100×75 points each
PANEL_LINES = ["About this app", "Version 10.9.12865", "Copyright 2026"]


class Painting(FakeHelper):
    """A window the tree describes (`buttons` in a toolbar, `rows` in a list that covers the rest, `unlabeled`
    buttons with no name, `texts` of its own, `extra` nodes of any kind) in which a step draws a panel the tree
    never hears of, at [300, 250]: `shown` says whether it is up. The glance sees it (and any cell `marks`
    sets), the screen can be read — the panel, and `elsewhere` a status word at the bottom — and the tree does
    not change (its fingerprint stays the same)."""

    def __init__(self, buttons=12, rows=0, unlabeled=0, texts=(), panel=PANEL_LINES, elsewhere=(("Ready", 20, 560),),
                 extra=()):
        super().__init__()
        self.buttons, self.rows, self.unlabeled, self.texts = buttons, rows, unlabeled, list(texts)
        self.panel, self.elsewhere, self.shown, self.title = list(panel), list(elsewhere), False, "Untitled"
        self.extra = [dict(n) for n in extra]
        self.corner = 100                           # the brightness of the bottom right cell, far from the panel
        self.marks: dict[int, int] = {}             # cell -> brightness, over whatever else is drawn there

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") != "menubar":
            nodes = [{"ref": "w.0", "role": "AXWindow", "title": self.title, "frame": [0, 0, 800, 600], "depth": 0}]
            nodes += [{"ref": f"t{i}", "role": "AXStaticText", "value": t, "parent": "w.0"} for i, t in enumerate(self.texts)]
            nodes += [{"ref": f"b{i}", "role": "AXButton", "rdesc": "button", "title": f"Tool {i}", "actions": ["AXPress"],
                       "frame": [10 + 40 * i, 10, 30, 20], "parent": "w.0"} for i in range(self.buttons)]
            nodes += [{"ref": f"r{j}", "role": "AXButton", "rdesc": "button", "title": f"Row {j}", "actions": ["AXPress"],
                       "frame": [0, 40 + 23 * j, 800, 23], "parent": "w.0"} for j in range(self.rows)]
            nodes += [{"ref": f"u{k}", "role": "AXButton", "actions": ["AXPress"], "frame": [700 + 30 * k, 10, 20, 20],
                       "parent": "w.0"} for k in range(self.unlabeled)]
            nodes += [dict(n, parent="w.0") for n in self.extra]
            return {"nodes": nodes, "ms": 2}
        if method == "screen.glance":
            cells = bytearray([100] * (N * N))
            if self.shown:                          # the panel covers cells 27, 28, 35 and 36
                for i in (27, 28, 35, 36):
                    cells[i] = 30
            cells[63] = self.corner
            for i, v in self.marks.items():
                cells[i] = v
            return {"grid": N, "cells": base64.b64encode(bytes(cells)).decode(), "frame": [0, 0, 800, 600]}
        if method == "screen.ocr":
            self.calls.append((method, p))
            boxes = [{"text": t, "frame": [700 + 30 * k + 2, 12, 16, 14]} for k, t in enumerate(("Zoom", "Share")[: self.unlabeled])]
            boxes += [{"text": t, "frame": [x, y, 120, 14]} for t, x, y in self.elsewhere]
            if self.shown:
                boxes += [{"text": t, "frame": [300, 250 + 16 * k, 160, 14]} for k, t in enumerate(self.panel)]
            return {"boxes": boxes, "frame": [0, 0, 800, 600], "ms": 5}
        return super().call(method, timeout, **p)


def painting(tmp_path, helper, **observe):
    return Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "sight", "vision"], **observe}}), helper=helper,
                  decider=ScriptedDecider([]))


def step(eng, task, look, action):
    """A step taken from `look`, as `_execute` counts it and `_perform` records it: the next look is what shows
    what it did."""
    from macwork.model import Step, exact_state
    eng.cache["actions_done"] = eng.cache.get("actions_done", 0) + 1
    task.steps.append(Step(len(task.steps), action, ok=True, before=exact_state(look.sig, look.obs.screen_text)))
    task.prev = {"sig": look.sig, "label": action, "handle": action, "ok": True, "events": [], "app": look.ctx.app,
                 "screen": look.obs.screen_text, "window": look.obs.window, "unseen": False, "glance": look.obs.notes.get("glance")}


def drawn(tmp_path, helper, **observe):
    """A look, a step that draws the panel, and the look after it."""
    eng = painting(tmp_path, helper, **observe)
    task = eng._new_task("which version is this?", {}, None)
    before = eng._look(task, eng._step_context(task))
    helper.shown = True
    step(eng, task, before, "menu App ▸ About")
    return eng, task, eng._look(task, eng._step_context(task))


def test_a_short_panel_in_a_described_window_is_read_where_the_picture_changed(tmp_path):
    """12 actionable nodes and a three-line panel: HEAD read the window, found four texts outside every node
    where six make a canvas, and dropped them. Reading by sight was decided for offering click targets."""
    h = Painting(buttons=12)
    eng, task, look = drawn(tmp_path, h)
    assert "Version 10.9.12865" in look.obs.screen_text, "what the step drew was read and dropped"
    change = task.steps[-1].change
    assert change["picture_only"] and change.get("picture_region") == [200, 150, 400, 300], change
    assert look.obs.notes["read_where_the_picture_changed"] == [200, 150, 400, 300]
    assert look.obs.notes["read_by_sight_lines"] == 3 and "Ready" not in look.obs.screen_text


def test_a_panel_drawn_over_a_described_list_is_read_too(tmp_path):
    """A list the tree describes covers the window, and the panel is drawn over its rows: HEAD did not read
    the screen at all, since the tree names everything and no control lacks a label."""
    h = Painting(buttons=12, rows=24)
    eng, task, look = drawn(tmp_path, h)
    assert "Version 10.9.12865" in look.obs.screen_text
    assert len(h.did("screen.ocr")) == 1


def test_what_the_step_drew_is_not_cut_by_the_screen_text_cap(tmp_path):
    """Added after a long window, the cap cut the one thing on screen known to be new."""
    h = Painting(buttons=12, texts=[f"Paragraph {i}: " + "words of the document " * 3 for i in range(8)])
    eng, task, look = drawn(tmp_path, h, window={"screen_text_chars": 300})
    assert look.obs.notes["screen_text_cut"] > 0, "the tree's own text fills the screen text"
    assert look.obs.screen_text.splitlines()[:3] == PANEL_LINES, "what the step drew is not first"
    # six lines make the window's own reading add them too, after the tree's text, where the cap cuts them:
    # they are in the screen text once, first, and counted once
    six = Painting(buttons=12, texts=h.texts, panel=[f"Line {i}" for i in range(6)])
    eng, task, look = drawn(tmp_path, six, window={"screen_text_chars": 300})
    assert look.obs.screen_text.splitlines()[:6] == [f"Line {i}" for i in range(6)]
    assert look.obs.notes["read_by_sight_lines"] == 6, "lines the cap cut were counted as read into the screen text"


def test_only_what_is_new_where_the_picture_changed_is_added(tmp_path):
    """Text read elsewhere in the window is not what the step drew, and a line the tree holds is the tree's,
    even where the cap cut it from the screen text."""
    long = [f"Paragraph {i}: " + "words of the document " * 3 for i in range(8)]
    h = Painting(buttons=12, texts=long + ["Copyright 2026"], elsewhere=[("Ready", 20, 560), ("Status: idle", 650, 560)])
    eng, task, look = drawn(tmp_path, h, window={"screen_text_chars": 300})
    text = look.obs.screen_text
    assert text.splitlines()[:2] == ["About this app", "Version 10.9.12865"]
    assert look.obs.notes["screen_text_cut"] > 0 and "Copyright 2026" not in text, \
        "a line the tree holds, past the cap, was read again off the screen"
    assert "Status: idle" not in text, "text outside where the picture changed was added"
    assert look.obs.notes["read_by_sight_lines"] == 2
    # and what goes ahead of the tree's text is bounded: ten lines, in a window the tree describes
    many = Painting(buttons=12, rows=24, panel=[f"Line {i}" for i in range(12)])
    eng, task, look = drawn(tmp_path, many)
    assert look.obs.screen_text.splitlines()[:10] == [f"Line {i}" for i in range(10)]
    assert "Line 10" not in look.obs.screen_text and look.obs.notes["read_by_sight_lines"] == 10


def test_a_window_read_on_every_look_is_read_once_after_the_picture_changed(tmp_path):
    """A canvas (six texts outside every node) is read on every look. After a step that changed its picture
    and none of its text, the loop read it a second time on the same look, and every text in it was offered
    and said twice; what the step changed is not said again either."""
    cells = [(f"Cell {i}", 300, 250 + 16 * i) for i in range(6)]      # where the picture changes
    h = Painting(buttons=12, panel=[], elsewhere=[("Ready", 20, 560)] + cells)
    eng, task, look = drawn(tmp_path, h)
    assert task.steps[-1].change["picture_only"]
    lines = look.obs.screen_text.splitlines()
    assert sorted(lines) == sorted(["Ready"] + [c[0] for c in cells]), lines
    clicks = [a.label for a in look.obs.affordances if a.label.startswith("click the text")]
    assert len(clicks) == len(set(clicks)) == 7, clicks
    assert len(h.did("screen.ocr")) == 2, "one read on each look"


def test_a_read_panel_is_not_reported_gone_while_it_is_still_there(tmp_path):
    """Read on one look and missing from the next, a panel still on screen was reported gone by the step after
    it. Its text stays while that part of the window looks the same, in that window, and is not read again
    for a change elsewhere in it (in a window the tree describes, which is not read on every look)."""
    h = Painting(buttons=12, rows=24)
    eng, task, read = drawn(tmp_path, h)
    assert "Version 10.9.12865" in read.obs.screen_text
    step(eng, task, read, "press the Tool 3 button")                         # it changes nothing
    same = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" in same.obs.screen_text, "the panel is on screen and its text was not"
    assert "gone" not in task.steps[-1].outcome and "Version" not in task.steps[-1].outcome, task.steps[-1].outcome
    h.title = "Other"                                                      # another window in front: not its text
    step(eng, task, same, "press the Tool 4 button")
    other = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" not in other.obs.screen_text and eng.scope(task.id).read_there is not None
    h.title = "Untitled"                                                   # …and back
    step(eng, task, other, "press the Tool 5 button")
    back = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" in back.obs.screen_text
    h.corner, h.texts = 220, ["Saved"]                    # the picture changes, but not there, and the tree says more
    reads = len(h.did("screen.ocr"))
    step(eng, task, back, "press the Tool 6 button")
    lit = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" in lit.obs.screen_text, "a change elsewhere in the window took the panel's text"
    assert task.steps[-1].outcome == "appeared: Saved", task.steps[-1].outcome
    assert len(h.did("screen.ocr")) == reads, "a change elsewhere in the window had the panel read again"
    h.shown = False                                                        # a step that closes it
    step(eng, task, lit, "press the escape key")
    closed = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" not in closed.obs.screen_text and "Version 10.9.12865" in task.steps[-1].outcome
    assert eng.scope(task.id).read_there is None


def test_a_panel_that_stays_is_read_again_where_it_changed(tmp_path):
    """A step that changed a line of a panel that stays up changes the picture where the panel was read. Its
    lines were dropped then, all of them: the step was reported to have made the whole panel go, and as a change
    with text gone is not one of the picture only, nothing read what it did either."""
    h = Painting(buttons=12, panel=PANEL_LINES + ["Check for updates"])
    eng, task, read = drawn(tmp_path, h)
    assert "Check for updates" in read.obs.screen_text
    h.panel = PANEL_LINES + ["You are up to date"]          # the line the button was on, and the cell it is in
    h.marks = {35: 60}
    step(eng, task, read, "click the text 「Check for updates」")
    after = eng._look(task, eng._step_context(task))
    assert task.steps[-1].outcome == "「Check for updates」 became 「You are up to date」", task.steps[-1].outcome
    assert after.obs.screen_text.splitlines()[:4] == PANEL_LINES + ["You are up to date"]
    assert eng.scope(task.id).read_there["lines"] == PANEL_LINES + ["You are up to date"]


def test_a_panel_that_closed_while_the_engine_waited_is_not_read_back(tmp_path):
    """No action is taken while the engine waits, and a read keyed on the actions taken handed back the panel
    from before the wait on the looks after it, though the picture showed it closed."""
    h = Painting(buttons=12)
    eng, task, read = drawn(tmp_path, h)
    assert "Version 10.9.12865" in read.obs.screen_text
    reads = len(h.did("screen.ocr"))
    h.shown = False                                         # it closes by itself; no step is taken
    again = eng._look(task, eng._step_context(task))
    assert "Version 10.9.12865" not in again.obs.screen_text, "a panel no longer on screen was read back"
    assert eng.scope(task.id).read_there is None and len(h.did("screen.ocr")) == reads + 1
    still = eng._look(task, eng._step_context(task))       # …and while it looks the same, it is not read again
    assert len(h.did("screen.ocr")) == reads + 1 and "Version 10.9.12865" not in still.obs.screen_text


def test_what_a_field_holds_and_what_a_control_is_called_are_not_what_the_step_drew(tmp_path):
    """Typing into a document and pressing a checkbox change only the picture: the text a field holds and the
    name of a control are not in the screen text. Read off the screen where the picture changed, they were
    put in front of it as what the step drew, though the tree gives them as the field's contents and the
    control's name."""
    typed = "Dear Sam, the total is 391"
    extra = [{"ref": "f", "role": "AXTextArea", "rdesc": "text entry area", "value": typed, "editable": True,
              "frame": [300, 240, 400, 60]},
             {"ref": "c", "role": "AXCheckBox", "rdesc": "checkbox", "title": "Bold", "actions": ["AXPress"],
              "frame": [300, 282, 60, 16]}]
    h = Painting(buttons=12, extra=extra, panel=[typed, "Bold", "1 spelling suggestion"])
    eng, task, look = drawn(tmp_path, h)
    assert task.steps[-1].change["picture_only"]
    assert look.obs.screen_text.splitlines()[:1] == ["1 spelling suggestion"], look.obs.screen_text
    assert typed not in look.obs.screen_text and "Bold" not in look.obs.screen_text.splitlines()
    assert look.obs.notes["read_by_sight_lines"] == 1 and eng.scope(task.id).read_there["lines"] == ["1 spelling suggestion"]


def test_a_step_without_a_region_is_read_as_before(tmp_path):
    """A step recorded before where the picture changed was measured has no region: its window is read the way
    it was — the loop's reading names its unlabeled controls — and nothing is read as what the step drew. Not
    the whole window instead: in a window the tree describes that is text HEAD never put in front of the
    decider."""
    from macwork.model import Step
    h = Painting(buttons=12, rows=24, unlabeled=2)
    h.shown = True
    eng = painting(tmp_path, h)
    task = eng._new_task("which version is this?", {}, None)
    task.steps.append(Step(n=0, action="menu Help ▸ About", ok=True, events=[],
                           outcome="the picture in the window changed (6% of it, in the middle); no text on screen did",
                           change={"picture_only": True, "picture_share": 0.06, "picture_where": "in the middle"}))
    look = eng._look(task, eng._step_context(task))
    assert any("「Zoom」" in a.label for a in look.obs.affordances), "the window was not read as it was"
    assert "Version 10.9.12865" not in look.obs.screen_text and "read_where_the_picture_changed" not in look.obs.notes
    assert getattr(eng.scope(task.id), "read_there", None) is None


def test_the_controls_without_a_label_are_named_by_the_same_read(tmp_path):
    """The loop's own read of the window after such a step names its unlabeled controls, on that look and
    the ones after it; one read serves that and what the step drew, and it is a read taken after the step."""
    h = Painting(buttons=12, rows=24, unlabeled=2)
    eng = painting(tmp_path, h)
    task = eng._new_task("which version is this?", {}, None)
    first = eng._look(task, eng._step_context(task))
    assert any(a.channel == "vision" for a in first.obs.affordances) and not h.did("screen.ocr")
    h.shown = True
    step(eng, task, first, "menu App ▸ About")
    second = eng._look(task, eng._step_context(task))
    named = [a.label for a in second.obs.affordances if "(no label)" in a.label]
    assert any("「Zoom」" in x for x in named) and any("「Share」" in x for x in named), named
    assert "Version 10.9.12865" in second.obs.screen_text and len(h.did("screen.ocr")) == 1
    step(eng, task, second, "press the Tool 3 button")
    third = eng._look(task, eng._step_context(task))
    assert any("「Zoom」" in a.label for a in third.obs.affordances), "the controls were named on that look only"
    assert len(h.did("screen.ocr")) == 1, "the tree has not changed, and the read after the step was made again"
