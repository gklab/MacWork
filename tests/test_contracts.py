"""Every action promises something, and every promise is checked somewhere.

A step was "ok" when the action was carried out, and whether it did anything was a separate, partial
question: typed text was read back, a switched app was checked against the front app, and everything else
was "the screen reacted" if any Accessibility event had fired — including a clock's. Now a press promises
a change, an open promises a window, a read promises something back, and each is judged either the moment
the action returns or at the next look, from what changed. The memory of what did nothing is that verdict.
"""

from macwork.act import Outcome
from macwork.contract import DEFERRED, IMMEDIATE, kept, kept_by_change, promise
from macwork.engine import Engine
from macwork.model import Affordance, Step
from macwork.observe import Ctx
from tests.english_mac import APP, EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def test_every_action_promises_one_thing():
    assert promise(Affordance("w", "window", "type", "type into 「Search」", {}), {"text": "x"}) == "typed"
    assert promise(Affordance("a", "app", "activate", "switch to app Finder", {"pid": 7}), {}) == "switched"
    assert promise(Affordance("T", "file", "read", "read the text of a.txt", {"path": "/a"}, yields="file:/a:1"), {}) == "returned"
    assert promise(Affordance("r", "window", "read_all", "read all the text in this window", {}), {}) == "returned"
    assert promise(Affordance("p", "file", "open", "open the file a.txt", {"path": "/a"}), {}) == "opened"
    assert promise(Affordance("s", "window", "select", "select row 「x」", {}), {}) == "selected"
    assert promise(Affordance("m", "menu", "press", "menu File ▸ New", {}), {}) == "changed"
    assert promise(Affordance("k", "keys", "key", "press cmd+n", {}), {}) == "changed"
    assert promise(Affordance("o", "pointer", "click", "click 「OK」", {}), {}) == "changed"
    assert set(IMMEDIATE) | set(DEFERRED) == {"typed", "switched", "returned", "changed", "opened", "selected"}


def test_a_read_that_handed_nothing_back_broke_its_promise(tmp_path):
    c = Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    read = Affordance("T", "file", "read", "read the text of a.txt", {"path": "/a"}, yields="file:/a:1")
    assert kept(c, read, {}, Outcome(True, output={"file_read": {"text": "hello"}}), [])[0] is True
    held, why = kept(c, read, {}, Outcome(True, output={}), [])
    assert held is False and "nothing" in why


def test_a_press_is_judged_at_the_next_look_and_an_unseen_one_is_not():
    assert kept_by_change("changed", changed=True, seen=True) == (True, "the screen changed")
    assert kept_by_change("changed", changed=False, seen=True) == (False, "nothing on screen changed")
    assert kept_by_change("changed", changed=False, seen=False)[0] is None
    assert kept_by_change("typed", changed=False, seen=True) == (None, ""), "not a deferred promise"


def test_what_did_nothing_is_the_broken_promise(tmp_path):
    """A press whose screen stayed the same: kept is False, and that action is not offered from here again."""
    class Still(EnglishMac):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.wait":          # no event, no change: the fake's default answers one every time
                return {"events": [], "timed_out": True}
            return super().call(method, timeout, **p)

    eng = Engine(cfg(tmp_path), helper=Still(), decider=ScriptedDecider([{"pick": "Refresh"}, {"pick": "Refresh"}, {"pick": "done", "done": 0.95}]))
    res = eng.do("refresh it")
    steps = eng.tasks[res["task_id"]].steps
    assert steps[0].promise == "changed" and steps[0].kept is False and "nothing" in steps[0].kept_why
    assert eng.tasks[res["task_id"]].memory.no_effect_handles() == {steps[0].handle}


def test_a_press_that_changed_the_screen_kept_its_promise(tmp_path):
    helper = EnglishMac()
    d = ScriptedDecider([{"pick": "Refresh"}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path), helper=helper, decider=d)

    class Changing(EnglishMac):
        pass

    original = helper.call

    def call(method, timeout=30.0, **p):
        if method == "ax.perform":
            helper.text = "4 items"          # the press did something
        return original(method, timeout, **p)
    helper.call = call
    res = eng.do("refresh it")
    step = eng.tasks[res["task_id"]].steps[0]
    assert step.kept is True and step.kept_why == "the screen changed"
    assert not eng.tasks[res["task_id"]].memory.no_effect
