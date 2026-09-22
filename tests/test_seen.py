"""What the engine could not see, said out loud; and what an action could not know, checked before it.

* the screen text is one pool with a cap, and three providers cut it to the cap as they wrote, silently:
  a prompt read after a long window lost its words, and the decider was told nothing was missing;
* windows from other processes were counted one per process, so a second dialog from the same one was
  invisible;
* typing was offered while a password field had the keyboard, where the helper refuses every keystroke;
* an empty Accessibility tree read as "no window", also when the helper simply had no permission to look;
* a click on a point read from the screen landed wherever that point was now, and the clipboard channel
  reported success whether or not the pasteboard had changed;
* a window that changes by itself — a clock — "reacted" to every action.
"""

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
    c = Ctx(cfg=cfg(tmp_path), helper=FakeHelper(), app={"pid": 42, "name": "Clock"}, goal="", inputs={}, task="t", gate=None,
            cache={"ambient": {"42|World Clock"}})
    a = Affordance("w1", "window", "press", "button 「Start」", {})
    ok, why = kept(c, a, {}, Outcome(True), ["AXValueChanged"])
    assert ok is None and "by itself" in why
    c.cache["ambient"] = set()
    assert kept(c, a, {}, Outcome(True), ["AXValueChanged"])[0] is True
