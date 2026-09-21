"""An action that could not be carried out is not an action that does nothing.

Both used to be one fact — "never offered again from this screen in this task". For an action that ran and
changed nothing that is right: it is a fact about the action. For one that *failed* — the app was busy, the
element had gone, a wait timed out — it is a fact about the moment, and an open world has plenty of those: a
button that works once the page has loaded was gone for the rest of the task.

Withheld while the screen is exactly as it was when it failed; offered again as soon as anything on it has
changed. Nothing here is a timer or a count — `engine.max_repeats` was already the limit and still is.
"""

from macwork.engine import Engine
from macwork.helper import HelperError
from tests.english_mac import EnglishMac
from tests.test_engine import ScriptedDecider, cfg


class Busy(EnglishMac):
    """Its button cannot be pressed until the app is ready; `text` is what the window says."""

    def __init__(self, fails=1):
        super().__init__()
        self.fails, self.text = fails, "loading…"   # what the window says

    def call(self, method, timeout=30.0, **p):
        if method == "ax.perform" and self.fails > 0:
            self.calls.append((method, p))
            self.fails -= 1
            raise HelperError("cannot_complete", "the app did not answer")
        if method == "ax.wait" and self.quiet:
            self.calls.append((method, p))
            return {"events": [], "timed_out": True}
        return super().call(method, timeout, **p)

    quiet = False


class Harmless(ScriptedDecider):
    what = {"navigate": 1.0}           # these tests are not about the floor


def offered(decider, n):
    return list(decider.seen[n][1]["action"]["criteria"].values())


def run(tmp_path, helper, script):
    decider = Harmless(script)
    res = Engine(cfg(tmp_path, config={"observe": {"providers": ["window"]}}), helper=helper, decider=decider).do("refresh the list", inputs={"text": "x"})
    return decider, res


def test_it_is_withheld_while_nothing_has_changed(tmp_path):
    helper = Busy()
    decider, _ = run(tmp_path, helper, [{"pick": "Refresh"}, {"pick": "done", "done": 0.95}])
    assert any("Refresh" in o for o in offered(decider, 0))
    assert not any("Refresh" in o for o in offered(decider, 1)), "asking again on the very same screen gets the same failure"


def test_it_comes_back_when_the_screen_has_changed(tmp_path):
    helper = Busy()

    def loaded(state, questions):
        helper.text = "ready"          # the page finished loading while something else was done
        return {"pick": "Search"}

    decider, res = run(tmp_path, helper, [{"pick": "Refresh"}, loaded, {"pick": "Refresh"}, {"pick": "done", "done": 0.95}])
    assert any("Refresh" in o for o in offered(decider, 2)), f"still gone after the screen changed: {res['steps']}"
    assert "Refresh" in res["steps"][-1] and "(failed)" not in res["steps"][-1]


def test_an_action_that_ran_and_did_nothing_stays_gone_as_before(tmp_path):
    helper = Busy(fails=0)
    helper.quiet = True                # it is pressed, and nothing whatever happens

    def changes(state, questions):
        helper.text = "something else changed"
        return {"pick": "Search"}

    decider, _ = run(tmp_path, helper, [{"pick": "Refresh"}, changes, {"pick": "done", "done": 0.95}, {"pick": "done", "done": 0.95}])
    assert not any("Refresh" in o for o in offered(decider, 1))
    assert not any("Refresh" in o for o in offered(decider, 2)), "that is a fact about the action, not about the moment"


def test_it_does_not_come_back_for_ever(tmp_path):
    helper = Busy(fails=99)
    n = {"i": 0}

    def again(state, questions):
        n["i"] += 1
        helper.text = f"tick {n['i']}"     # a screen that never stops changing, and a button that never works
        return {"pick": "Refresh"} if any("Refresh" in o for o in questions["action"]["criteria"].values()) else {"pick": "done", "done": 0.95}

    decider, res = run(tmp_path, helper, [again] * 12)
    tries = [s for s in res["steps"] if "Refresh" in s]
    assert 1 < len(tries) <= 4, f"engine.max_repeats is the limit: {len(tries)} tries"
