"""A sub-goal is done when the screen shows what the planner said it would, not when one number says so.

Consecutive confident looks used to walk a whole plan without a step being taken, and the planner had been
writing the expected result of each step all along ("Finder window showing docs contents") — as prose the
engine stringified and ignored. Each step now carries its evidence, and the decider is asked, in the same
request as everything else, whether the screen shows it.
"""

from macwork.engine import Engine
from macwork.planner import _steps
from tests.english_mac import EnglishMac
from tests.test_engine import ScriptedDecider, cfg
from tests.test_flex import FakeBackend


def test_steps_come_with_their_evidence_or_without_it():
    goals, seen = _steps([{"goal": "open the settings", "evidence": "a window titled Settings"}, "press New", {"sub_goal": "old shape", "result": "a document"}], 8)
    assert goals == ["open the settings", "press New", "old shape"] and seen == ["a window titled Settings", "", "a document"]
    assert _steps(None, 8) == ([], [])


def test_a_sub_goal_advances_only_when_its_evidence_is_on_screen(tmp_path):
    d = ScriptedDecider([{"pick": "New"},
                         {"pick": "Refresh", "step_done": 0.95, "step_evidence": 0.1},    # done, says the quick judgement; the screen does not show it
                         {"pick": "Refresh", "step_done": 0.95, "step_evidence": 0.9},    # now it does
                         {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always"}}), helper=EnglishMac(), decider=d)
    back = FakeBackend([{"steps": [{"goal": "make a document", "evidence": "a window titled Untitled with a text area"}], "inputs": {}}])
    eng._planner = back
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["plan"]["at"] == 1
    assert [s[:12] for s in res["steps"]] == ["menu File ▸ ", "button 「Refr"], "the plan did not move on the first confident look"
    asked = [q for s, q in d.seen if "step_evidence" in q]
    assert asked and "Untitled" in asked[0]["step_evidence"]["instructions"], "the evidence is in the question, filled in through the gate"
    shown = [s for s, q in d.seen if s.get("current_step_expects")]
    assert shown and "Untitled" in shown[0]["current_step_expects"], "the decider sees what the step should leave"


def test_a_plan_without_evidence_advances_as_before(tmp_path):
    d = ScriptedDecider([{"pick": "New"}, {"pick": "Refresh", "step_done": 0.95}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always"}}), helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([{"steps": ["make a document"], "inputs": {}}])
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["plan"]["at"] == 1 and len(res["steps"]) == 1
    assert not any("step_evidence" in q for s, q in d.seen)
