"""Windows belonging to no app the task is working in.

One kind covers the app — a permission prompt, a modal alert — and that was handled. The other is simply
somewhere else on the screen, and a notification banner is always that: it sits in a corner and overlaps
nothing, so one arriving mid-task was invisible to the engine and to the decider both. Measured against a
real banner: 8 Accessibility nodes carrying its full text, and nothing in the observation mentioned it.

No process is named anywhere here. What separates these from an ordinary app's window is what the system
says about them — no Dock presence, and something readable inside.
"""

from typing import Any

from macwork.config import Config
from macwork.model import Observation
from macwork.observe import Ctx, get_provider


class Helper:
    mode = "fake"

    def __init__(self, windows: list[dict[str, Any]], trees: dict[int, list[dict[str, Any]]] | None = None) -> None:
        self.windows, self.trees = windows, trees or {}
        self.snapshots: list[int] = []

    def call(self, method: str, timeout: float = 30.0, **p: Any) -> Any:
        if method == "screen.windows":
            return self.windows
        if method == "ax.snapshot":
            self.snapshots.append(p["pid"])
            return {"nodes": self.trees.get(p["pid"], [])}
        raise AssertionError(method)


def win(pid: int, owner: str, frame: list[int], regular: bool = False) -> dict[str, Any]:
    return {"pid": pid, "owner": owner, "frame": frame, "regular": regular, "alpha": 1}


def node(title: str) -> dict[str, Any]:
    return {"ref": f"r{title}", "pid": 0, "role": "AXButton", "title": title, "actions": ["AXPress"],
            "frame": [0, 0, 10, 10]}


def look(helper: Helper, **over: Any) -> Observation:
    cfg = Config.load(overrides={"config": {"observe": {"overlays": over}}} if over else None)
    ctx = Ctx(cfg=cfg, helper=helper, app={"pid": 1, "name": "Editor"}, goal="", inputs={}, task="t",
              gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    get_provider("overlays")(ctx, obs)
    return obs


APP = win(1, "Editor", [100, 100, 400, 400], regular=True)
BANNER = win(9, "Notification Center", [1100, 0, 380, 90])          # a corner: overlaps nothing
PROMPT = win(8, "System Settings", [150, 150, 200, 120])                    # over the app


def test_a_prompt_over_the_app_is_still_reported():
    h = Helper([PROMPT, APP], {8: [node("Allow")]})
    obs = look(h)
    assert obs.notes["covered_by"][0]["where"] == "in front of the app"
    assert any("Allow" in a.label for a in obs.affordances)


def test_a_banner_that_overlaps_nothing_is_no_longer_invisible():
    h = Helper([BANNER, APP], {9: [node("Close")]})
    obs = look(h)
    assert obs.notes["covered_by"][0]["where"] == "elsewhere on screen"
    assert "Meeting" not in obs.screen_text and "Notification Center" in obs.screen_text


def test_what_it_says_reaches_the_decider_as_evidence():
    h = Helper([BANNER, APP], {9: [{"ref": "t", "pid": 9, "role": "AXStaticText", "value": "meeting moved to Thursday 15:40"}]})
    assert "meeting moved to Thursday 15:40" in look(h).screen_text


def test_its_buttons_are_options_like_any_other():
    h = Helper([BANNER, APP], {9: [node("View"), node("Close")]})
    assert {"View", "Close"} <= {a.label.split("「")[-1].rstrip("」") for a in look(h).affordances} or \
        len([a for a in look(h).affordances if a.channel == "window"]) == 2


def test_they_can_be_read_without_being_offered():
    h = Helper([BANNER, APP], {9: [node("Close")]})
    obs = look(h, elsewhere_actions=False)
    assert obs.notes["covered_by"] and obs.affordances == []


def test_an_app_with_no_window_on_screen_no_longer_hides_everything_else():
    """That is when a prompt is *most* likely to be the thing wanting an answer."""
    h = Helper([BANNER], {9: [node("Close")]})     # the app being worked in is minimised
    assert look(h).notes.get("covered_by")


def test_an_ordinary_app_s_window_is_not_one_of_these():
    h = Helper([win(7, "Another app", [0, 0, 300, 300], regular=True), APP], {7: [node("button")]})
    assert not look(h).notes.get("covered_by")


def test_the_app_s_own_windows_are_not_reported_as_someone_else_s():
    h = Helper([win(1, "Editor", [0, 0, 50, 50]), APP], {1: [node("Panel")]})
    assert not look(h).notes.get("covered_by")


def test_something_with_nothing_readable_in_it_is_left_out():
    h = Helper([win(5, "Dock", [0, 0, 1512, 982]), BANNER, APP], {5: [], 9: [node("Close")]})
    obs = look(h)
    assert [o["from"] for o in obs.notes["covered_by"]] == ["Notification Center"]


def test_an_unreadable_one_does_not_use_up_the_budget():
    """Each candidate costs a snapshot; spending the allowance on empty layers would hide the real ones."""
    empties = [win(20 + i, f"layer {i}", [0, 0, 10, 10]) for i in range(6)]
    h = Helper([*empties, BANNER, APP], {9: [node("Close")]})
    assert [o["from"] for o in look(h, max_elsewhere=2).notes["covered_by"]] == ["Notification Center"]


def test_how_many_are_looked_at_is_bounded():
    many = [win(30 + i, f"Alert{i}", [0, 0, 10, 10]) for i in range(10)]
    h = Helper([*many, APP], {30 + i: [node(f"button{i}")] for i in range(10)})
    assert len(look(h, max_elsewhere=3).notes["covered_by"]) == 3


def test_it_can_be_switched_off():
    h = Helper([BANNER, APP], {9: [node("Close")]})
    assert not look(h, elsewhere=False).notes.get("covered_by")


def test_a_window_that_just_appeared_is_read_before_the_ones_that_were_always_there():
    """A real task pressed the menu bar clock three times and never read what came down: the panel was
    listed behind the windows that had been there all along, past the elsewhere budget."""
    cache: dict = {}

    def look_with(windows, trees):
        cfg = Config.load(overrides={"config": {"observe": {"overlays": {"max_elsewhere": 2}}}})
        ctx = Ctx(cfg=cfg, helper=Helper(windows, trees), app={"pid": 1, "name": "Editor"}, goal="", inputs={}, task="t",
                  gate=None, cache=cache)
        obs = Observation(app=ctx.app, window="w", affordances=[])
        get_provider("overlays")(ctx, obs)
        return [c["from"] for c in obs.notes.get("covered_by", [])]

    old1 = {**win(11, "Old One", [1000, 0, 200, 60]), "id": 1}
    old2 = {**win(12, "Old Two", [1000, 100, 200, 60]), "id": 2}
    assert look_with([old1, old2, APP], {11: [node("A")], 12: [node("B")]}) == ["Old One", "Old Two"]
    panel = {**win(13, "Control Center", [1200, 30, 300, 400]), "id": 3}      # dropped down by the last action, listed last
    assert look_with([old1, old2, panel, APP], {11: [node("A")], 12: [node("B")], 13: [node("Today")]})[0] == "Control Center"


def test_a_panel_that_says_more_than_fits_says_so_and_can_be_read_whole():
    """Notification Center, dropped down by the menu bar clock, held the date past a 400-character cut, and
    the decider was told the panel held only notifications."""
    long_nodes = [node(f"Item {i} with a longish title") for i in range(40)]
    panel = {**win(13, "Notification Center", [1200, 30, 300, 400]), "id": 3}
    cfg = Config.load(overrides={"config": {"observe": {"overlays": {"text_chars": 200}}}})
    ctx = Ctx(cfg=cfg, helper=Helper([panel, APP], {13: long_nodes}), app={"pid": 1, "name": "Editor"}, goal="", inputs={}, task="t",
              gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    get_provider("overlays")(ctx, obs)
    said = obs.notes["covered_by"][0]["text"]
    assert "more characters, not shown" in said and len(said) < 300
    whole = [a for a in obs.affordances if a.verb == "read_all" and a.target["pid"] == 13]
    assert whole and "Notification Center" in whole[0].label and whole[0].target["interruption"]
