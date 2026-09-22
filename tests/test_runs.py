"""What a run gets afresh, what a task keeps, and what the decider is told about options it cannot see.

* the counts in `pace` bound loops inside one run and were never reset, while steps and seconds were: a
  task handed back with `need_continue` three times came back with fresh steps and no replan left;
* a correction — "what I just did went wrong" — was refused as already asked when a rethink had been
  asked on the same screen, which is exactly the stuck case the correction allowance exists for;
* an action the goal never asked for was withheld by its label, so one 「OK」 took every 「OK」 in every app
  out of the task, and nothing told the decider that anything had been withheld at all;
* "ask the user" with one candidate fell through and acted: the decider saying the goal is unclear meant
  "do it anyway";
* the plan that set a task's whole trajectory was written before the first look, with no app and no screen.
"""

from macwork.engine import Engine
from macwork.model import Step, Task
from tests.english_mac import EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


def test_a_new_run_gets_its_counts_back_and_keeps_what_happened(tmp_path):
    task = Task(goal="x")
    task.pace.replans, task.pace.looks, task.pace.waits, task.pace.fruitless = 2, 6, 4, 2
    task.pace.launched, task.pace.settled_done = True, True
    task.begin_run(0, 0.0)
    assert (task.pace.replans, task.pace.looks, task.pace.waits, task.pace.fruitless) == (0, 0, 0, 0)
    assert task.pace.launched and task.pace.settled_done, "the app was opened by this task, whatever run it is"


def test_a_correction_is_not_the_question_already_asked_on_this_screen(tmp_path):
    eng = Engine(cfg(tmp_path, config={"planner": {"max_replans": 2, "max_corrections": 2}}),
                 helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = FakeBackend([{"steps": ["try the menu"], "inputs": {}}, {"steps": ["fix it"], "inputs": {}}])
    task = Task(goal="total the column", id="t1")
    task.plan = ["type the total"]
    task.steps.append(Step(n=0, action="click the cell", ok=True, events=["changed"], decision={"progress_after": 0.9}))
    eng.tasks[task.id] = task
    assert eng._consult(task, None, None, "not sure about this screen")
    task.steps.append(Step(n=1, action="type 「=SUM(x)」", ok=True, events=["changed"], decision={"progress_after": 0.1}))
    assert eng._consult(task, None, None, "the formula was rejected"), "same screen, same point in the plan — a different question"
    assert task.pace.corrections == 1 and task.pace.replans == 1


def test_what_the_goal_never_asked_for_is_withheld_where_it_sits_and_said_so(tmp_path):
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    task = eng._new_task("write a note", {}, None)
    first = eng._look(task, eng._step_context(task))
    delete = next(a for a in first.affs if "Delete Document" in a.label)
    task.memory.declined.add(eng.approval_key(delete))

    look = eng._look(task, eng._step_context(task))
    assert not any("Delete Document" in v for v in look.options.values()), "withheld"
    assert look.state["not_offered_because_the_goal_never_asked"] == [delete.label], "and the decider is told"
    other = eng._new_task("write a note", {}, None)
    other.memory.declined.add(delete.label)         # keyed on the label: circles and leaving for another app
    assert not any("Delete Document" in v for v in eng._look(other, eng._step_context(other)).options.values())


def test_ask_user_with_one_candidate_asks_the_caller_to_say_more(tmp_path):
    def decide(state, q):
        keys = q["action"]["criteria"]
        return {"pick": "New", "move": "ask_user", "probs": {k: (0.95 if "New" in v else 0.0) for k, v in keys.items()}}
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([decide]))
    res = eng.do("make it")
    assert res["status"] == "need_input" and "clarification" in res["pending"]["inputs"]
    assert not eng.helper.did("ax.perform"), "nothing was done on a goal the decider called unclear"


def test_the_first_plan_is_written_looking_at_the_screen(tmp_path):
    back = FakeBackend([{"steps": ["press New"], "inputs": {}}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always"}}), helper=EnglishMac(),
                 decider=ScriptedDecider([{"pick": "New"}, {"pick": "done"}]))
    eng._planner = back
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["plan"]["steps"] == ["press New"]
    assert "TextEdit" in back.prompts[0] and "Untitled" in back.prompts[0], "the app and its window were in the brief"
