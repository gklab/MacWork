"""What gets in the way is kept apart from the work.

A prompt from another process used to be poured into the same pool as everything else: its buttons were
options toward the goal, and when the decider chose one the safety floor stopped the whole task. Told to
total a column in Numbers while another app's "allow access to the local network?" prompt sat on screen, the
engine spent its first step on that prompt's button and handed the task back untouched. A worker that needs
a person every time a window pops up is not a worker.

An interruption is one of three things — the task itself, something to set aside, or the owner's to answer —
and which one comes from judgements the engine already makes, not from a list of known dialogs.
"""

from typing import Any

from macwork.engine import Engine
from tests.test_engine import FakeHelper, ScriptedDecider, cfg, worded

PROMPT_PID = 8
APP_FRAME = [100, 100, 900, 700]
PROMPT_FRAME = [400, 300, 260, 235]


def button(ref: str, title: str) -> dict[str, Any]:
    return {"ref": ref, "role": "AXButton", "title": title, "actions": ["AXPress"], "frame": [420, 480, 80, 24]}


class Interrupted(FakeHelper):
    """The app being worked in, with a window from another process on top of it."""

    def __init__(self, says: str, buttons: list[str]) -> None:
        super().__init__()
        self.says, self.buttons, self.dismissed = says, buttons, False

    def call(self, method, timeout=30.0, **p):
        if method == "screen.windows":
            app = {"pid": 42, "owner": "TextEdit", "frame": APP_FRAME, "regular": True, "alpha": 1}
            prompt = {"pid": PROMPT_PID, "owner": "Some Other App", "frame": PROMPT_FRAME, "regular": False, "alpha": 1}
            return [app] if self.dismissed else [prompt, app]
        if method == "ax.snapshot" and p.get("pid") == PROMPT_PID:
            nodes = [{"ref": "p.0", "role": "AXStaticText", "value": self.says}]
            return {"nodes": nodes + [button(f"p.{i + 1}", b) for i, b in enumerate(self.buttons)]}
        if method == "ax.perform" and str(p.get("ref", "")).startswith("p."):
            self.calls.append((method, p))
            self.dismissed = True
            return {"ok": True}
        return super().call(method, timeout, **p)


class Floor(ScriptedDecider):
    """Classifies a prompt's controls the way the real floor would: allowing or refusing access is a grant,
    a close or a "later" only backs out."""

    def _classify(self, q):
        text = worded(q)
        if "Allow" in text:                       # "Allow" and "Don't Allow" alike: refusing is deciding too
            pick = "grant"
        elif "Later" in text or "Close" in text:
            pick = "back_out"
        else:
            return super()._classify(q)
        return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}


def engine(tmp_path, helper, decider):
    providers = {"observe": {"providers": ["menu", "window", "overlays", "keys"]}}
    return Engine(cfg(tmp_path, config=providers), helper=helper, decider=decider)


def offered(decider) -> list[str]:
    return [v for _, q in decider.seen if "action" in q for v in q["action"]["criteria"].values()]


def test_a_permission_prompt_from_another_app_does_not_stop_the_work(tmp_path):
    helper = Interrupted("Allow “Some Other App” to find devices on your local network?", ["Don't Allow", "Allow"])
    d = Floor([{"pick": "New Document"}, {"pick": "done", "done": 0.95}])
    res = engine(tmp_path, helper, d).do("make a new document")

    assert res["status"] == "done", f"a prompt that had nothing to do with the goal ended the task: {res['status']}"
    assert not any("Allow" in v for v in offered(d)), "its buttons were offered as ways of reaching the goal"
    assert not helper.dismissed, "it was answered — and answering it either way is the owner's decision"
    left = res["outputs"]["left_for_you"]
    assert left and left[0]["from"] == "Some Other App" and "local network" in left[0]["says"]


def test_something_that_can_simply_be_dismissed_is_dismissed_first(tmp_path):
    helper = Interrupted("A new version is available.", ["Later", "Install Now"])
    d = Floor([{"pick": "New Document"}, {"pick": "done", "done": 0.95}])
    res = engine(tmp_path, helper, d).do("make a new document")

    assert res["status"] == "done"
    assert helper.dismissed and "Later" in res["steps"][0], f"it was not set aside before the work began: {res['steps']}"
    assert not any("Install Now" in v for v in offered(d)), "the other button was offered toward the goal"
    assert "left_for_you" not in res["outputs"], "nothing was left: it was dealt with"


def test_when_the_goal_is_about_the_prompt_it_is_the_task_and_the_floor_still_asks(tmp_path):
    helper = Interrupted("Allow “Some Other App” to find devices on your local network?", ["Don't Allow", "Allow"])
    d = Floor([{"pick": "Allow"}])
    d.about = 1.0                                  # the goal names this prompt
    res = engine(tmp_path, helper, d).do("give Some Other App access to the local network")

    assert any("Allow" in v for v in offered(d)), "the goal was about the prompt and its controls were withheld"
    assert res["status"] == "need_confirm" and not helper.dismissed, "granting went ahead without the user"


def test_a_click_that_would_land_on_the_prompt_is_not_offered(tmp_path):
    from macwork.interrupt import _under
    from macwork.model import Affordance
    beneath = Affordance("o1", "pointer", "click", "click 「137」", {"x": 500, "y": 400})
    beside = Affordance("o2", "pointer", "click", "click 「249」", {"x": 150, "y": 150})
    press = Affordance("w1", "window", "press", "button New", {"frame": [450, 350, 40, 20]})
    assert _under(beneath, PROMPT_FRAME), "a click under the prompt would land on the prompt, not on the app"
    assert not _under(beside, PROMPT_FRAME)
    assert not _under(press, PROMPT_FRAME), "an Accessibility press reaches the app whatever floats above it"
