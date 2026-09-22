"""Input that lasts, for what is steered rather than operated — and what the engine may conclude from it.

Two halves. The first is plumbing: someone declares controls (the caller, or the person per app), each
becomes an option that states what will physically happen and for how long, and the helper carries out the
whole hold in one call. Nothing here knows any game.

The second half is the one that matters. Everything the loop has learned to conclude — "that did nothing",
"that has had its turns from this screen", "that choice was taken back" — rests on the screen showing what
an action did. A view that draws itself shows nothing, so every one of those rules would fire on the first
step: walking forward would be withdrawn for having changed nothing. A step whose effect cannot be seen says
so, and nothing concludes from it.
"""

import pathlib

from macwork.engine import Engine
from macwork.model import Observation
from macwork.observe import PROVIDERS, Ctx, parse_controls
from tests.english_mac import APP, EnglishMac
from tests.test_engine import ScriptedDecider, cfg

CONTROLS = {"move forward": {"keys": ["w"], "ms": [300, 1200]},
            "sprint": {"keys": ["shift", "w"], "ms": 1000},
            "look left": {"move": {"dx": -200}, "ms": 150},
            "attack": {"buttons": ["left"], "ms": 100}}


class Steered(EnglishMac):
    """A Mac whose front window draws itself: holds are carried out, and nothing on screen ever changes."""

    ended = "completed"

    def call(self, method, timeout=30.0, **p):
        if method == "input.hold":
            self.calls.append((method, p))
            return {"ok": True, "held_ms": p["ms"] if self.ended == "completed" else 40, "ended": self.ended}
        if method == "ax.wait":
            self.calls.append((method, p))
            return {"events": [], "timed_out": True}
        return super().call(method, timeout, **p)


class Harmless(ScriptedDecider):
    what = {"navigate": 1.0}           # these tests are not about the floor


def offered(inputs=None, **config):
    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["controls"](Ctx(cfg(pathlib.Path("/nowhere"), config=config), Steered(), app=APP, inputs=inputs or {}, running=[]), obs)
    return obs


# ----------------------------------------------------------------- what is declared becomes what is offered
def test_each_length_is_its_own_option_and_says_what_will_happen():
    labels = [a.label for a in offered({"controls": CONTROLS}).affordances]
    assert labels == ["move forward — hold w for 0.3 s", "move forward — hold w for 1.2 s",
                      "sprint — hold shift + w for 1 s", "look left — move the pointer by (-200, 0) over 0.15 s",
                      "attack — hold the left mouse button for 0.1 s"]


def test_nothing_is_offered_that_nobody_declared():
    assert offered().affordances == []


def test_the_person_can_declare_them_per_app_and_the_caller_has_the_last_word():
    mine = {"observe": {"controls": {"apps": {APP["bundle_id"]: {"jump": {"keys": ["space"], "ms": 80}, "move forward": {"keys": ["up"], "ms": 500}}}}}}
    assert [a.label for a in offered(**mine).affordances] == ["jump — hold space for 0.08 s", "move forward — hold up for 0.5 s"]
    both = [a.label for a in offered({"controls": {"move forward": {"keys": ["w"], "ms": 300}}}, **mine).affordances]
    assert both == ["jump — hold space for 0.08 s", "move forward — hold w for 0.3 s"]
    other_app = {"observe": {"controls": {"apps": {"com.example.other": {"jump": {"keys": ["space"], "ms": 80}}}}}}
    assert offered(**other_app).affordances == []


def test_a_length_over_the_ceiling_is_shortened_before_the_label_is_written():
    good, _ = parse_controls({"wait it out": {"keys": ["w"], "ms": [4000, 60000, 90000]}}, max_ms=5000)
    assert [c["ms"] for c in good] == [4000, 5000], "and two lengths that become the same one are one option"


def test_what_cannot_be_understood_is_reported_not_guessed():
    good, bad = parse_controls({"no length": {"keys": ["w"]}, "nothing to do": {"ms": 100}, "backwards": {"keys": ["s"], "ms": -5},
                                "not a mapping": "w", "fine": {"keys": ["d"], "ms": 200}}, max_ms=5000)
    assert [c["name"] for c in good] == ["fine"] and len(bad) == 4
    assert offered({"controls": {"no length": {"keys": ["w"]}}}).notes["controls_rejected"]


# ----------------------------------------------------------------- carrying one out
def run(tmp_path, script, helper=None, **config):
    helper, decider = helper or Steered(), Harmless(script)
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "controls"]}, **config}), helper=helper, decider=decider)
    return helper, decider, eng.do("walk to the door", inputs={"controls": CONTROLS})


def test_the_whole_hold_is_one_call_with_its_length(tmp_path):
    helper, _, res = run(tmp_path, [{"pick": "sprint"}, {"pick": "done", "done": 0.95}])
    assert helper.did("input.hold") == [{"ms": 1000, "keys": ["shift", "w"], "buttons": [], "move": {}, "yield_to_user": True}]
    assert res["status"] == "done" and res["outputs"]["held"] == {"control": "sprint", "ms": 1000, "ended": "completed"}
    order = [m for m, _ in helper.calls if m in ("input.hold", "ax.wait", "ax.snapshot")]
    after = order[order.index("input.hold") + 1]
    assert after == "ax.snapshot", "the hold was the wait: it looks again at once, with no UI event to wait for"


def test_an_exclusive_run_is_not_ended_by_the_person(tmp_path):
    helper, _, _ = run(tmp_path, [{"pick": "sprint"}, {"pick": "done", "done": 0.95}], engine={"mode": "exclusive"})
    assert helper.did("input.hold")[0]["yield_to_user"] is False


def test_the_person_reaching_for_the_mac_is_a_failed_step_that_says_why(tmp_path):
    helper = Steered()
    helper.ended = "user"
    _, decider, res = run(tmp_path, [{"pick": "sprint"}, {"pick": "done", "done": 0.95}], helper=helper)
    assert res["steps"][0].endswith("(failed)")
    assert "reached for the Mac: released after 40 ms" in decider.seen[1][0]["history"][-1]


def test_the_controls_are_options_not_a_blob_in_the_state(tmp_path):
    _, decider, _ = run(tmp_path, [{"pick": "done", "done": 0.95}])
    assert "controls" not in decider.seen[0][0]["inputs"]


def test_a_helper_installed_before_this_existed_says_what_to_do(tmp_path):
    from macwork.helper import HelperError

    class Older(Steered):
        def call(self, method, timeout=30.0, **p):
            if method == "input.hold":
                raise HelperError("no_method", "unknown method input.hold")
            return super().call(method, timeout, **p)

    _, decider, res = run(tmp_path, [{"pick": "sprint"}, {"pick": "done", "done": 0.95}], helper=Older())
    assert "macwork helper install" in decider.seen[1][0]["history"][-1]


# ----------------------------------------------------------------- what may be concluded
def test_walking_forward_again_and_again_is_not_being_stuck(tmp_path):
    """Six holds from a screen that never changes. `no_effect` would have withdrawn it after the first,
    a stretch that got nowhere (`engine.max_no_progress`), and the retraction memory somewhere in between."""
    script = [{"pick": "move forward — hold w for 1.2 s"}] * 6 + [{"pick": "done", "done": 0.95}]
    helper, decider, res = run(tmp_path, script)
    assert len(helper.did("input.hold")) == 6 and res["status"] == "done"
    assert all(any("move forward" in o for o in q["action"]["criteria"].values()) for _, q in decider.seen)
    assert "tried_here_without_effect" not in decider.seen[-1][0]


def test_the_history_does_not_claim_it_did_nothing(tmp_path):
    _, decider, _ = run(tmp_path, [{"pick": "attack"}, {"pick": "done", "done": 0.95}])
    line = decider.seen[1][0]["history"][-1]
    assert "does not show in anything the engine can read" in line and "no ui change" not in line


def test_an_ordinary_button_that_does_nothing_is_still_withdrawn(tmp_path):
    """The exemption is for what cannot be seen, not for everything on a screen that holds such a thing."""
    _, decider, _ = run(tmp_path, [{"pick": "Refresh"}, {"pick": "done", "done": 0.95}])
    assert not any("Refresh" in o for o in decider.seen[1][1]["action"]["criteria"].values())
    assert any("move forward" in o for o in decider.seen[1][1]["action"]["criteria"].values())


# ----------------------------------------------------------------- step level
def test_a_caller_driving_step_by_step_can_hold_too(tmp_path):
    helper = Steered()
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["controls"]}}), helper=helper, decider=Harmless([]))
    seen = eng.observe(None, "", {"controls": CONTROLS})
    attack = next(a for a in seen["affordances"] if a["label"].startswith("attack"))
    assert eng.act(attack["id"])["ok"] and helper.did("input.hold")[0]["buttons"] == ["left"]
