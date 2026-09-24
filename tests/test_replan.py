"""A replan is told the plan, and replaces what is left of it.

The replan was handed the goal, what had been done and the problem — never the plan, where it stood in it, or
what the screen was to show for the sub-goal it was at (aff49b2812ff's replan knew neither that 168 was expected
nor that it was at sub-goal 1, and came back with nothing after 19.8 s). Its answer then replaced the whole plan
and put the task back at sub-goal 1, and an answer with moves only did the same. Now the replan is told the plan,
each sub-goal done, now or next, and its answer is spliced in from where the plan stood when it was asked; an
answer that lands after the plan moved on brings its moves only. And a sub-goal the planner said nothing about the
screen for is ticked off once per step taken: consecutive confident looks walked a whole plan that way.
"""

import json

from macwork.consult import Asked
from macwork.engine import Engine
from macwork.model import Step
from macwork.observe import observe
from tests.english_mac import EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend

PLAN = ["open Calculator", "compute 17×23", "write it into a new document"]
EVIDENCE = ["a Calculator window", "391 on the display", ""]


def planned(tmp_path, replies, at=1):
    """A task a plan had got to sub-goal `at` of, one step taken, and a planner that answers `replies`."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = FakeBackend(list(replies))
    task = eng._new_task("compute 17×23 in Calculator and write the result into a new document", {}, None)
    task.plan, task.plan_evidence, task.plan_i = list(PLAN), list(EVIDENCE), at
    task.steps.append(Step(0, "open app Calculator", ok=True, events=["AXFocusedWindowChanged"], decision={"progress_after": 0.9}))
    return eng, task


def replan(eng, task):
    ctx = eng._step_context(task)
    return eng._consult(task, ctx, observe(ctx), "the actions on screen do not lead toward the goal")


def adoptions(tmp_path):
    """The audit's records of answers taken in (not the exchanges themselves)."""
    return [r for r in (json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines())
            if r.get("kind") == "plan" and "problem" in r]


def test_a_replan_is_told_the_plan_and_where_it_stands(tmp_path):
    eng, task = planned(tmp_path, [{"steps": [], "inputs": {}, "try": [{"keys": "cmd+n"}], "blocked": ""}])
    replan(eng, task)
    prompt = eng._planner.prompts[0]
    assert "Plan: " in prompt, "the replan was not told the plan"
    told = json.loads(prompt.split("Plan: ", 1)[1].split("\n", 1)[0])
    assert told == [{"sub_goal": "open Calculator", "status": "done"},
                    {"sub_goal": "compute 17×23", "status": "now", "expected_on_screen": "391 on the display"},
                    {"sub_goal": "write it into a new document", "status": "next"}], told
    assert prompt.index("Goal:") < prompt.index("Plan:") < prompt.index("Done so far:"), \
        "what is the same on every call comes first; the plan belongs with the goal"


def test_a_replan_replaces_only_what_is_left_of_the_plan(tmp_path):
    answer = {"steps": [{"goal": "clear the display", "evidence": "0 on the display"},
                        {"goal": "compute 17×23", "evidence": "391 on the display"},
                        {"goal": "write it into a new document", "evidence": "a document holding 391"}],
              "inputs": {}, "try": [{"keys": "escape"}], "blocked": ""}
    eng, task = planned(tmp_path, [answer])
    assert replan(eng, task)
    assert task.plan == ["open Calculator", "clear the display", "compute 17×23", "write it into a new document"], task.plan
    assert task.plan_i == 1 and task.plan_evidence[1] == "0 on the display" and task.plan_evidence[0] == "a Calculator window"
    assert task.memory.plan_moved_at == len(task.steps)
    got = adoptions(tmp_path)[-1]
    assert got["at"] == 1 and got["brief_chars"] and all(isinstance(n, int) for n in got["brief_chars"].values()), got


def test_moves_alone_leave_the_plan_where_it_is(tmp_path):
    """An answer with moves only set the task back to sub-goal 1 of a plan it had got further in. Its moves are
    taken in as the keyboard reads them, and the plan stays where it was."""
    eng, task = planned(tmp_path, [{"steps": [], "inputs": {}, "try": [{"keys": "Command + N"}], "blocked": ""}], at=2)
    assert replan(eng, task)
    assert task.plan == PLAN and task.plan_i == 2, (task.plan, task.plan_i)
    assert task.tries == [{"keys": "cmd+n"}]
    assert adoptions(tmp_path)[-1]["at"] is None


def test_an_answer_that_lands_after_the_plan_moved_adopts_only_its_moves(tmp_path):
    """Asked beside the loop at sub-goal 1, the answer lands once the task has reached sub-goal 2: set back, the
    plan would repeat what the task has done. Its moves are taken, less the ones the task took meanwhile — a key
    pressed alone is the key the task pressed."""
    eng, task = planned(tmp_path, [], at=1)
    asked = Asked(key=("x",), kind="rethink", problem="?", went_wrong=False, first=False, steps_at=len(task.steps), plan_i_at=1)
    task.steps += [Step(1, "press the return key (default button 「OK」)", ok=True, events=["x"], decision={"progress_after": 0.9}),
                   Step(2, "menu File ▸ New Document (⌘N)", ok=True, events=["x"], decision={"progress_after": 0.9})]
    task.plan_i = 2
    answer = {"steps": ["open Calculator again"], "evidence": [""], "inputs": {}, "blocked": "",
              "try": [{"keys": "return"}, {"action": "File ▸ New Document"}, {"keys": "cmd+s"}]}
    assert eng._adopt(task, None, answer, asked)
    assert task.plan == PLAN and task.plan_i == 2, (task.plan, task.plan_i)
    assert task.tries == [{"keys": "cmd+s"}], task.tries


def test_a_sub_goal_without_evidence_advances_once_per_step_taken(tmp_path):
    """18e94cd's evidence gate stopped consecutive confident looks from walking a plan without a step being taken;
    a sub-goal the planner gave no evidence for was not under it. Ticked off, the plan moves on only once a step has
    been taken since it last moved."""
    seen = []

    def look(pick, **said):
        def answer(state, questions):
            seen.append(state.get("current_step"))
            return {"pick": pick, **said}
        return answer

    d = ScriptedDecider([look("New"),
                         look("Refresh", step_done=0.95, step_evidence=0.9),     # sub-goal 1 shown done: on to 2
                         look("Refresh", step_done=0.95),                        # no step since: 2 is not ticked off
                         look("done", done=0.95)])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}), helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([{"steps": [{"goal": "make a document", "evidence": "a window titled Untitled"},
                                           {"goal": "name it", "evidence": ""}, {"goal": "save it", "evidence": ""}], "inputs": {}}])
    res = eng.do("make a new document and save it")
    assert seen[:3] == ["make a document", "make a document", "name it"], seen
    assert seen[3] == "name it", f"a sub-goal with no evidence was ticked off with no step taken: {seen}"
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New (⌘N)", "button 「Refresh」"], res["steps"]
