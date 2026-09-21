"""Windows the Accessibility tree cannot describe.

An app that draws its own content — an editor, a design tool, a terminal emulator, a game — publishes a
window and little or nothing inside it. What is there can still be read on-device, and what can be read can
be pointed at. Two things were missing: the pointer could only left-click a point, and there was no way to
see past the fold of a window that exposes no scroll area to perform `AXScrollDownByPage` on.

The part worth keeping straight is *when* to offer this, because the first attempt got it wrong twice.
"""

from typing import Any

from macwork.config import Config
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, get_provider


class Helper:
    mode = "fake"

    def __init__(self, boxes: list[dict[str, Any]] | None = None) -> None:
        self.boxes = boxes if boxes is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, timeout: float = 30.0, **p: Any) -> Any:
        self.calls.append((method, p))
        if method == "screen.ocr":
            return {"boxes": self.boxes, "frame": [0, 0, 400, 400], "ms": 10, "direction": "ltr"}
        if method == "ax.fingerprint":
            return {"fingerprint": "same-every-time"}     # what a canvas looks like: nothing in the tree moves
        if method == "apps.frontmost":
            return {"app": {"pid": 1, "name": "Canvas"}}     # pointing at a window means bringing it forward first
        if method in ("input.scroll", "input.click", "apps.activate"):
            return {"ok": True}
        raise AssertionError(method)

    def did(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]


def box(text: str, x: int, y: int) -> dict[str, Any]:
    return {"text": text, "frame": [x, y, 60, 14]}


def ctx(helper: Helper, cache: dict[str, Any] | None = None, **over: Any) -> Ctx:
    cfg = Config.load(overrides={"config": {"observe": over}} if over else None)
    return Ctx(cfg=cfg, helper=helper, app={"pid": 1, "name": "Canvas"}, goal="", inputs={}, task="t",
               gate=None, cache=cache if cache is not None else {})


def obs_with(frame: list[int] | None = None, actionable: int = 0) -> Observation:
    o = Observation(app={"pid": 1, "name": "Canvas"}, window="w", affordances=[])
    o.notes["window_actionable"] = actionable
    o.notes["open_windows"] = ["w"]
    if frame:
        o.notes["window_frame"] = frame
    return o


# --------------------------------------------------------------------------- the wheel

def test_the_wheel_is_offered_wherever_there_is_a_window():
    """Not gated on judging the window a canvas: both ways of judging that were wrong when measured — by
    geometry one text area swallowed all 63 things read from a terminal, and by text 62 of those 63 looked
    missing because `screen_text` is a capped summary."""
    o = obs_with(frame=[0, 0, 400, 400], actionable=477)     # a well-described window, tree and all
    get_provider("wheel")(ctx(Helper()), o)
    assert sorted(a.target["dy"] for a in o.affordances) == [-5, 5]


def test_it_costs_two_options_not_one_per_anything():
    o = obs_with(frame=[0, 0, 400, 400])
    get_provider("wheel")(ctx(Helper()), o)
    assert len(o.affordances) == 2


def test_a_window_that_is_not_there_gets_none():
    o = obs_with(frame=None)
    get_provider("wheel")(ctx(Helper()), o)
    assert o.affordances == []


def test_it_can_be_switched_off():
    o = obs_with(frame=[0, 0, 400, 400])
    get_provider("wheel")(ctx(Helper(), wheel={"enabled": False}), o)
    assert o.affordances == []


def test_it_aims_at_the_window_rather_than_wherever_the_pointer_was_left():
    from macwork.act import get_channel
    helper = Helper()
    a = Affordance("w", "pointer", "scroll", "scroll down", {"window_frame": [100, 50, 200, 100], "dy": -5})
    get_channel("pointer")(ctx(helper), a, {})
    assert helper.did("input.scroll") == [{"x": 200.0, "y": 100.0, "dy": -5}]


# --------------------------------------------------------------------------- reading a window with no tree

def test_text_no_element_covers_becomes_a_target():
    o = obs_with(frame=[0, 0, 400, 400])
    get_provider("vision")(ctx(Helper([box("Start", 10, 10), box("Settings", 10, 30)]), vision={"mode": "always"}), o)
    assert [a.verb for a in o.affordances if a.channel == "pointer"].count("click") == 2


def test_text_the_tree_already_reaches_is_not_offered_twice():
    o = obs_with(frame=[0, 0, 400, 400])
    o.affordances.append(Affordance("t", "window", "press", "Start", {"frame": [8, 8, 64, 18]}))
    get_provider("vision")(ctx(Helper([box("Start", 10, 10), box("Settings", 10, 300)]), vision={"mode": "always"}), o)
    clicks = [a.label for a in o.affordances if a.channel == "pointer" and a.verb == "click"]
    assert len(clicks) == 1 and "Settings" in clicks[0]


def test_the_secondary_button_is_one_option_aimed_by_name():
    """Per-spot would double a canvas window's options against the budget that is already the scarce thing."""
    o = obs_with(frame=[0, 0, 400, 400])
    get_provider("vision")(ctx(Helper([box(f"Items{i}", 10, i * 20) for i in range(12)]), vision={"mode": "always"}), o)
    named = [a for a in o.affordances if a.verb == "click_named"]
    assert len(named) == 1 and "what" in named[0].slots


def test_aiming_at_something_that_is_no_longer_there_fails_rather_than_clicking_anyway():
    from macwork.act import get_channel
    helper = Helper()
    a = Affordance("p", "pointer", "click_named", "context menu", {"spots": {"Start": [0, 0, 10, 10]}, "button": "right"})
    out = get_channel("pointer")(ctx(helper), a, {"what": "something that is not there"})
    assert not out.ok and not helper.did("input.click")


# --------------------------------------------------------------------------- the cache a canvas invalidates

def test_a_canvas_is_read_again_after_the_engine_acts():
    """Scrolling a canvas changes everything on screen and not one Accessibility node, so keying the read on
    the tree's fingerprint would have handed back the text from before the scroll."""
    helper, cache = Helper([box("I", 10, 10 + i * 20) for i in range(8)]), {}
    c = ctx(helper, cache, vision={"mode": "always"})
    get_provider("vision")(c, obs_with(frame=[0, 0, 400, 400]))
    get_provider("vision")(c, obs_with(frame=[0, 0, 400, 400]))
    assert len(helper.did("screen.ocr")) == 1, "the same screen was read twice"
    cache["actions_done"] = 1                                   # the engine scrolled
    get_provider("vision")(c, obs_with(frame=[0, 0, 400, 400]))
    assert len(helper.did("screen.ocr")) == 2, "the screen was not read again after acting on it"


def test_a_window_the_tree_does_describe_keeps_its_cache_across_actions():
    helper, cache = Helper([box("I", 10, 10)]), {}
    c = ctx(helper, cache, vision={"mode": "always"})
    o = obs_with(frame=[0, 0, 400, 400], actionable=400)
    o.affordances.append(Affordance("t", "window", "press", "I", {"frame": [0, 0, 400, 400]}))
    get_provider("vision")(c, o)
    cache["actions_done"] = 1
    get_provider("vision")(c, obs_with(frame=[0, 0, 400, 400], actionable=400))
    assert len(helper.did("screen.ocr")) == 1


# --------------------------------------------------------------------------- what a Shortcut hands back

def test_a_shortcut_s_result_is_not_dropped_on_the_floor(monkeypatch, tmp_path):
    """`shortcuts run` writes what the shortcut *returns* to --output-path, not to stdout. Without asking
    for one, a shortcut that looks something up ran, succeeded and told the task nothing.

    This is also as close as anything gets to invoking an App Intent: no process may perform another app's
    intent, and a shortcut the person made is the route the system does offer.
    """
    import subprocess as sp

    from macwork.act import get_channel
    from macwork.model import Affordance

    seen: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = cmd[cmd.index("--output-path") + 1]
        open(out, "w", encoding="utf-8").write("Today 22°C，Cloudy")
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(sp, "run", fake_run)

    a = Affordance("s0", "shortcut", "run", "run shortcut 「Weather today」", {"name": "Weather today"})
    out = get_channel("shortcut")(ctx(Helper()), a, {"input": "Beijing"})
    assert out.ok and out.output["shortcut_result"]["text"] == "Today 22°C，Cloudy"
    assert "--input-path" in seen["cmd"] and "--output-path" in seen["cmd"]


def test_a_shortcut_that_returns_nothing_reports_nothing(monkeypatch):
    import subprocess as sp

    from macwork.act import get_channel
    from macwork.model import Affordance

    monkeypatch.setattr(sp, "run", lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout="", stderr=""))
    out = get_channel("shortcut")(ctx(Helper()), Affordance("s0", "shortcut", "run", "run it", {"name": "x"}), {})
    assert out.ok and out.output == {}


def test_a_window_mostly_undescribed_is_read_without_being_asked(tmp_path):
    """Numbers reports 29 actionable nodes in its chrome and one scroll area — 989x1103 of a 1260x1201
    window, 72% of it — that the tree describes not at all: no cell, no number, no header. Reading it was
    offered as one option among 843. How much of a window the tree leaves undescribed can be asked for
    before anything is read, and where most of it is undescribed there is no other way to see what is there.
    """
    from macwork.observe import PROVIDERS, Ctx
    from macwork.model import Observation
    from tests.test_engine import FakeHelper, cfg

    read = []

    class Canvas(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "screen.ocr":
                read.append(p)
                return {"frame": [0, 0, 1260, 1201],
                        "boxes": [{"text": "137", "frame": [700, 300, 40, 20], "conf": 0.9},
                                  {"text": "Total", "frame": [700, 400, 40, 20], "conf": 0.9}]}
            return super().call(method, timeout, **p)

    obs = Observation(app={"pid": 42, "name": "Numbers"}, window="sales", affordances=[])
    obs.notes["window_frame"] = [0, 0, 1260, 1201]
    obs.notes["window_actionable"] = 29                      # a well-stocked chrome
    obs.notes["unlabeled"] = [{"frame": [0, 98, 989, 1103], "rdesc": "scroll area", "context": ""}]
    ctx = Ctx(cfg(tmp_path), Canvas(), app={"pid": 42, "name": "Numbers"}, running=[])
    PROVIDERS["vision"](ctx, obs)

    assert read, "the window was left unread, so nothing in it can be acted on"
    assert any(a.channel == "pointer" and "137" in a.label for a in obs.affordances), \
        [a.label for a in obs.affordances]


def test_a_window_the_tree_does_describe_is_not_read(tmp_path):
    from macwork.observe import PROVIDERS, Ctx
    from macwork.model import Observation
    from tests.test_engine import FakeHelper, cfg

    read = []

    class Watching(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "screen.ocr":
                read.append(p)
                return {"frame": [0, 0, 1260, 1201], "boxes": []}
            return super().call(method, timeout, **p)

    obs = Observation(app={"pid": 42, "name": "TextEdit"}, window="untitled", affordances=[])
    obs.notes["window_frame"] = [0, 0, 1260, 1201]
    obs.notes["window_actionable"] = 29
    obs.notes["unlabeled"] = [{"frame": [0, 0, 120, 40], "rdesc": "group", "context": ""}]   # a corner of it
    PROVIDERS["vision"](Ctx(cfg(tmp_path), Watching(), app={"pid": 42, "name": "TextEdit"}, running=[]), obs)

    assert not read, "a window the tree describes was read from the screen anyway"
    assert any(a.channel == "vision" for a in obs.affordances), "…and reading it was not even offered"
