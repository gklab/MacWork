"""Progress is nearer to the step's evidence, not "the screen changed".

A real run pressed New, Create New, Create New, Create New: every click opened something, the decider called
each one an intended visible effect, and the plan sat at its first sub-goal for nine steps until the clock
ran out. Where the planner said what the step should leave on screen, the progress question is asked against
that; a task with no plan keeps the generic question. Every ending that is not done carries a cause.
"""

from macwork.engine import Engine
from macwork.invariants import check_ending
from macwork.model import Task
from tests.english_mac import EnglishMac
from tests.test_engine import ScriptedDecider, cfg
from tests.test_flex import FakeBackend


def test_the_progress_question_is_asked_against_the_evidence_when_there_is_some(tmp_path):
    d = ScriptedDecider([{"pick": "New", "step_done": 0.1, "step_evidence": 0.1},
                         {"pick": "Refresh", "step_done": 0.95, "step_evidence": 0.9},
                         {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always"}}), helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([{"steps": [{"goal": "make a document", "evidence": "a window titled Untitled with a text area"}], "inputs": {}}])
    eng.do("make a new document")
    asked = [q["progress"]["instructions"] for s, q in d.seen if "progress" in q]
    assert asked and "Untitled" in asked[0] and "nearer" in asked[0], asked     # once the plan is walked, the generic one again


def test_a_task_without_a_plan_keeps_the_generic_question(tmp_path):
    d = ScriptedDecider([{"pick": "New"}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=d)
    eng.do("make a new document")
    asked = [q["progress"]["instructions"] for s, q in d.seen if "progress" in q]
    assert asked and all("intended visible effect" in x for x in asked)


def test_the_planner_is_told_which_sub_goal_the_task_is_stuck_on(tmp_path):
    # each pick a different option: one that did nothing on a screen is rightly not offered there again
    d = ScriptedDecider([{"pick": k, "progress": 0.0, "step_evidence": 0.1}
                         for k in ("New", "press the tab key", "press the space key", "press the up key", "press the down key",
                                   "press the left key", "press the right key", "press the home key", "press the end key")])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always"}}), helper=EnglishMac(), decider=d)
    back = FakeBackend([{"steps": [{"goal": "open a blank sheet", "evidence": "an empty grid"}, {"goal": "type 42", "evidence": "42 in A1"}], "inputs": {}},
                        {"steps": [{"goal": "open a blank sheet another way", "evidence": "an empty grid"}], "inputs": {}}])
    eng._planner = back
    res = eng.do("put 42 in a sheet")
    replans = [p for p in back.prompts[1:]]
    assert replans and "still at sub-goal 1" in replans[0] and "open a blank sheet" in replans[0], replans


def test_every_ending_short_of_done_carries_a_cause():
    task = Task(goal="x")
    task.reason = "no route to the goal was found"
    check_ending(task, "failed")
    assert task.outputs["invariants_broken"] == ["ended failed with no cause"]
    task2 = Task(goal="x")
    task2.reason, task2.cause = "no route", "no_route"
    check_ending(task2, "failed")
    assert "invariants_broken" not in task2.outputs
