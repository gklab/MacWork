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
    ctx = Ctx(cfg=cfg, helper=helper, app={"pid": 1, "name": "编辑器"}, goal="", inputs={}, task="t",
              gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    get_provider("overlays")(ctx, obs)
    return obs


APP = win(1, "编辑器", [100, 100, 400, 400], regular=True)
BANNER = win(9, "Notification Center", [1100, 0, 380, 90])          # a corner: overlaps nothing
PROMPT = win(8, "系统设置", [150, 150, 200, 120])                    # over the app


def test_a_prompt_over_the_app_is_still_reported():
    h = Helper([PROMPT, APP], {8: [node("允许")]})
    obs = look(h)
    assert obs.notes["covered_by"][0]["where"] == "in front of the app"
    assert any("允许" in a.label for a in obs.affordances)


def test_a_banner_that_overlaps_nothing_is_no_longer_invisible():
    h = Helper([BANNER, APP], {9: [node("关闭")]})
    obs = look(h)
    assert obs.notes["covered_by"][0]["where"] == "elsewhere on screen"
    assert "会议" not in obs.screen_text and "Notification Center" in obs.screen_text


def test_what_it_says_reaches_the_decider_as_evidence():
    h = Helper([BANNER, APP], {9: [{"ref": "t", "pid": 9, "role": "AXStaticText", "value": "会议改到周四 15:40"}]})
    assert "会议改到周四 15:40" in look(h).screen_text


def test_its_buttons_are_options_like_any_other():
    h = Helper([BANNER, APP], {9: [node("查看"), node("关闭")]})
    assert {"查看", "关闭"} <= {a.label.split("「")[-1].rstrip("」") for a in look(h).affordances} or \
        len([a for a in look(h).affordances if a.channel == "window"]) == 2


def test_they_can_be_read_without_being_offered():
    h = Helper([BANNER, APP], {9: [node("关闭")]})
    obs = look(h, elsewhere_actions=False)
    assert obs.notes["covered_by"] and obs.affordances == []


def test_an_app_with_no_window_on_screen_no_longer_hides_everything_else():
    """That is when a prompt is *most* likely to be the thing wanting an answer."""
    h = Helper([BANNER], {9: [node("关闭")]})     # the app being worked in is minimised
    assert look(h).notes.get("covered_by")


def test_an_ordinary_app_s_window_is_not_one_of_these():
    h = Helper([win(7, "另一个 app", [0, 0, 300, 300], regular=True), APP], {7: [node("按钮")]})
    assert not look(h).notes.get("covered_by")


def test_the_app_s_own_windows_are_not_reported_as_someone_else_s():
    h = Helper([win(1, "编辑器", [0, 0, 50, 50]), APP], {1: [node("面板")]})
    assert not look(h).notes.get("covered_by")


def test_something_with_nothing_readable_in_it_is_left_out():
    h = Helper([win(5, "Dock", [0, 0, 1512, 982]), BANNER, APP], {5: [], 9: [node("关闭")]})
    obs = look(h)
    assert [o["from"] for o in obs.notes["covered_by"]] == ["Notification Center"]


def test_an_unreadable_one_does_not_use_up_the_budget():
    """Each candidate costs a snapshot; spending the allowance on empty layers would hide the real ones."""
    empties = [win(20 + i, f"层{i}", [0, 0, 10, 10]) for i in range(6)]
    h = Helper([*empties, BANNER, APP], {9: [node("关闭")]})
    assert [o["from"] for o in look(h, max_elsewhere=2).notes["covered_by"]] == ["Notification Center"]


def test_how_many_are_looked_at_is_bounded():
    many = [win(30 + i, f"提示{i}", [0, 0, 10, 10]) for i in range(10)]
    h = Helper([*many, APP], {30 + i: [node(f"按钮{i}")] for i in range(10)})
    assert len(look(h, max_elsewhere=3).notes["covered_by"]) == 3


def test_it_can_be_switched_off():
    h = Helper([BANNER, APP], {9: [node("关闭")]})
    assert not look(h, elsewhere=False).notes.get("covered_by")
