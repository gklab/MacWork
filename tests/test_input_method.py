"""An input method in the way of typing.

The helper types through an ASCII keyboard layout, so that an input method cannot compose the keys, and it gave
the person's own input source back as soon as the last key was posted. An app still a few keys behind then read
the last of them through the input method: 21 of 265 typing steps on 09-20..23 left the end of their text
composing. Reading the field back passed them, because the value holds the composition too, and the next look
took the input method's candidate panel for a prompt in front of the app: 48 looks in 27 tasks, 168 candidates
offered as options, 4 reports that it was left for the person to answer.

Helper protocol 0.2.0 keeps the layout until the keys have landed and says how (`landed`, `handback_ms`,
`switched`), says what an element is still composing (`marked`), and marks an input method's windows
(`input_method`). These are the engine's side of each. Nothing here needs a Mac, and no input method is named:
the one below is 'Example Input', composing the 'lo' of 'hello'.
"""

import json
from typing import Any

from macwork.act import Outcome, get_channel
from macwork.config import Config
from macwork.contract import kept
from macwork.engine import Engine
from macwork.helper import HelperError
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, get_provider
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

INTO_FIELD = Affordance("w3", "window", "type", "type into text field 「Recipient」", {"ref": "g2.3", "pid": 42})
AT_CURSOR = Affordance("y0", "keys", "type", "type the given text at the cursor", {})


# ----------------------------------------------------------------- the keyboard handed back once the keys are read

class HandsBack(FakeHelper):
    """Helper 0.2.0's reply to input.type: it had to select the ASCII layout, and the keys were read 14 ms after
    the last one. The menus' search field here takes no value, so a menu search types its words."""

    def call(self, method, timeout=30.0, **p):
        if method == "input.type":
            super().call(method, timeout, **p)
            return {"ok": True, "chars": len(p.get("text", "")), "switched": True, "landed": "read", "handback_ms": 14}
        if method == "ax.set" and p.get("ref") == "search":
            self.calls.append((method, p))
            raise HelperError("ax_error", "the field does not take a value")
        return super().call(method, timeout, **p)


class Ignores(HandsBack):
    """The keys were read, and the field still shows its old text, however it is written to."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.get":
            self.calls.append((method, p))
            return {"value": "/old/path"}
        return super().call(method, timeout, **p)


def test_typing_asks_the_helper_to_hold_the_layout_until_the_keys_are_read(tmp_path):
    ctx = Ctx(cfg(tmp_path, config={"input": {"handback_ms": 1234, "handback_quiet_ms": 321}}), HandsBack(),
              app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    ways = [INTO_FIELD,
            Affordance("w4", "window", "type_submit", "type into text field 「Search」 and press Return", {"ref": "g2.3", "pid": 42}),
            AT_CURSOR,
            Affordance("m0", "menusearch", "search", "search the menus", {"pid": 42, "menu_ref": "help", "field_ref": "search"})]
    outs = [get_channel(a.channel)(ctx, a, {"text": "hello"}) for a in ways]
    sent = ctx.helper.did("input.type")
    assert len(sent) == len(ways)
    assert all(p.get("handback_ms") == 1234 and p.get("quiet_ms") == 321 for p in sent), sent
    assert [o.typed for o in outs[:3]] == [{"ok": True, "chars": 5, "switched": True, "landed": "read", "handback_ms": 14}] * 3
    # a value set behind the app's back that did not take is typed after all, and the same way
    ctx = Ctx(cfg(tmp_path, config={"input": {"handback_ms": 1234, "handback_quiet_ms": 321, "set_value_first": True}}),
              Ignores(), app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    out = get_channel("window")(ctx, INTO_FIELD, {"text": "hello"})
    assert [(p.get("handback_ms"), p.get("quiet_ms")) for p in ctx.helper.did("input.type")] == [(1234, 321)]
    assert out.typed == outs[0].typed


def test_each_typing_step_is_recorded_by_how_it_was_handed_back_never_by_its_text(tmp_path):
    text = "someone.unusual@example.com"
    eng = Engine(cfg(tmp_path), helper=HandsBack(), decider=ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}]))
    res = eng.do("fill in the recipient", {"text": text}, app="TextEdit")
    assert res["status"] == "done"
    recs = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [r for r in recs if r["kind"] == "step"]
    typed = steps[0]
    assert [typed.get(k) for k in ("typed_landed", "typed_handback_ms", "typed_switched", "typed_kept")] == ["read", 14, True, True]
    assert "typed_landed" not in steps[-1], "the looks after the last step typed nothing"
    assert "typed" not in {r["kind"] for r in recs}, "no record of its own: the step's record says it"
    # the floor's record of what the action was carries its text, as it did before; the record of how the keys
    # were handed back never does
    assert not any(text in json.dumps(r, ensure_ascii=False) for r in steps)
    assert not any(isinstance(v, str) and len(v) > 12 for v in typed.values() if v != typed["task"])

    eng = Engine(cfg(tmp_path / "ignored"), helper=Ignores(), decider=ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}]))
    eng.do("fill in the recipient", {"text": text}, app="TextEdit")
    failed = [json.loads(line) for line in (tmp_path / "ignored" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
              if json.loads(line)["kind"] == "step"][0]
    assert failed["ok"] is False and (failed.get("typed_landed"), failed.get("typed_kept")) == ("read", False), \
        "a step that broke its promise still says how its keys were handed back"


# ----------------------------------------------------------------- letters still being composed are not typed

class Composing(FakeHelper):
    """The field and the element with the keyboard focus both hold "hello", of which an input method is still
    composing `marked` — said, as helper 0.2.0 says it, only when the read asks with marked=True. `ABSENT`: the
    element publishes no marked range, or the helper is older, and nothing is said."""

    ABSENT = object()

    def __init__(self, marked: Any = "lo") -> None:
        super().__init__()
        self.marked = marked

    def call(self, method, timeout=30.0, **p):
        if method == "ax.get":
            self.calls.append((method, p))
            got = {"value": "hello"}
            if p.get("marked") and self.marked is not self.ABSENT:
                got["marked"] = self.marked
            return got
        if method == "ax.focused":
            self.calls.append((method, p))
            node = {"role": "AXTextArea", "ref": "focus", "value": "hello"}
            if p.get("marked") and self.marked is not self.ABSENT:
                node["marked"] = self.marked
            return {"focused": node, "pid": 42, "app": "TextEdit", "secure_input": False}
        return super().call(method, timeout, **p)


def ctx_of(tmp_path, helper) -> Ctx:
    return Ctx(cfg(tmp_path), helper, app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})


def test_letters_still_being_composed_at_the_cursor_are_not_typed(tmp_path):
    held, why = kept(ctx_of(tmp_path, Composing()), AT_CURSOR, {"text": "hello"}, Outcome(True), [])
    assert held is False, why
    assert why == "「lo」 is still being composed by the input method: not yet in where the cursor is"


def test_letters_still_being_composed_in_a_field_are_not_typed(tmp_path):
    held, why = kept(ctx_of(tmp_path, Composing()), INTO_FIELD, {"text": "hello"}, Outcome(True), [])
    assert held is False, why
    assert why == "「lo」 is still being composed by the input method: not yet in the field"


def test_the_read_back_asks_what_is_being_composed(tmp_path):
    h = Composing(marked="")
    for a in (INTO_FIELD, AT_CURSOR):
        kept(ctx_of(tmp_path, h), a, {"text": "hello"}, Outcome(True), [])
    assert [p.get("marked") for p in h.did("ax.get")] == [True]
    assert [p.get("marked") for p in h.did("ax.focused")] == [True]


def test_committed_text_is_still_typed(tmp_path):
    """Nothing composing (""), a marked range that says nothing, or nothing said at all — the element publishes
    no range, or the helper is older: the text is read back as it was before. So is anything but text, which
    helper 0.2.0 never sends: its WebKit branch, which would have said True, was dropped unchecked."""
    for marked in (Composing.ABSENT, None, "", True):
        for a in (INTO_FIELD, AT_CURSOR):
            held, why = kept(ctx_of(tmp_path, Composing(marked)), a, {"text": "hello"}, Outcome(True), [])
            assert held is True and why.startswith("the text is in"), (marked, a.channel, why)


# ----------------------------------------------------------------- an input method's panel is no prompt

APP_WINDOW = {"id": 10, "pid": 42, "owner": "TextEdit", "layer": 0, "frame": [100, 100, 600, 400], "regular": True,
              "alpha": 1, "on_screen": True, "ordinary": True, "input_method": False}
# as helper 0.2.0 lists an input method's candidate panel: another process, above the normal window level, flagged
PANEL = {"id": 333, "pid": 77, "owner": "Example Input", "layer": 24, "frame": [300, 300, 340, 120], "regular": False,
         "alpha": 1, "on_screen": True, "ordinary": False, "input_method": True}
CANDIDATES = [{"ref": "c0", "pid": 77, "role": "AXStaticText", "value": "lo"},
              {"ref": "c1", "pid": 77, "role": "AXButton", "title": "1 lo", "actions": ["AXPress"], "frame": [310, 330, 40, 20]}]


class Windows:
    """The window server and the trees of other processes, for the overlays provider alone."""
    mode = "fake"

    def __init__(self, windows: list[dict[str, Any]], trees: dict[int, list[dict[str, Any]]]) -> None:
        self.windows, self.trees, self.snapshots = windows, trees, []

    def call(self, method: str, timeout: float = 30.0, **p: Any) -> Any:
        if method == "screen.windows":
            return self.windows
        if method == "ax.snapshot":
            self.snapshots.append(p["pid"])
            return {"nodes": self.trees.get(p["pid"], [])}
        if method == "screen.ocr":
            return {"boxes": [], "frame": p.get("near"), "ms": 1}
        raise AssertionError(method)


def overlays(helper: Windows) -> Observation:
    ctx = Ctx(cfg=Config.load(), helper=helper, app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t",
              gate=None, cache={})
    obs = Observation(app=ctx.app, window="Untitled", affordances=[])
    get_provider("overlays")(ctx, obs)
    return obs


def test_an_input_method_panel_over_the_app_is_not_a_prompt():
    h = Windows([PANEL, APP_WINDOW], {77: CANDIDATES})
    obs = overlays(h)
    assert not obs.notes.get("covered_by") and not obs.notes.get("interruptions")
    assert obs.affordances == [], "a candidate is not an option"
    assert "1 lo" not in obs.screen_text and "Example Input" not in obs.screen_text
    assert 77 not in h.snapshots, "nothing of it is read"


def test_an_input_method_window_at_the_ordinary_level_is_still_read():
    """Its settings window is a window like any other: a task may have opened it, or be about it."""
    settings = {**PANEL, "id": 334, "layer": 0, "ordinary": True, "frame": [200, 150, 500, 300]}
    h = Windows([settings, APP_WINDOW], {77: [{"ref": "s1", "pid": 77, "role": "AXCheckBox", "title": "Show candidates",
                                               "actions": ["AXPress"], "frame": [220, 200, 120, 20]}]})
    obs = overlays(h)
    assert [c["from"] for c in obs.notes["covered_by"]] == ["Example Input"]
    assert any("Show candidates" in a.label for a in obs.affordances) and 77 in h.snapshots


class ComposingOverTheApp(FakeHelper):
    """TextEdit's window, and over it the candidate panel of an input method still composing the last two letters
    of what was typed: the field reads "hello", and "lo" of it is marked."""

    def __init__(self) -> None:
        super().__init__()
        self.screen = [PANEL, APP_WINDOW]

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("pid") == PANEL["pid"]:
            self.calls.append((method, p))
            return {"nodes": CANDIDATES, "ms": 1}
        if method == "ax.get" and p.get("marked"):
            self.calls.append((method, p))
            return {"value": self.typed, "marked": "lo"}
        return super().call(method, timeout, **p)


def test_a_typing_step_that_leaves_its_end_composing_fails_and_leaves_nothing_for_you(tmp_path):
    d = ScriptedDecider([{"pick": "Recipient"}] + [{"pick": "done"}] * 4)
    c = cfg(tmp_path, config={"observe": {"providers": ["apps", "menu", "window", "keys", "overlays"]}})
    eng = Engine(c, helper=ComposingOverTheApp(), decider=d)
    res = eng.do("fill in the recipient", {"text": "hello"}, app="TextEdit")
    step = eng.tasks[res["task_id"]].steps[0]
    assert step.ok is False and "input method" in (step.error or ""), step.error
    assert "left_for_you" not in res["outputs"]
    states = [s for s, _ in d.seen + d.side]
    assert states and not any(isinstance(s, dict) and "covered_by" in s for s in states)
    assert not any("about" in q for _, q in d.side), "nobody was asked whether the goal is about it"
    assert PANEL["pid"] not in [p.get("pid") for p in eng.helper.did("ax.snapshot")]
