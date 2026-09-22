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


def test_three_actions_from_one_unchanged_screen_is_said_and_asked_about(tmp_path):
    """30% of real steps followed one on an unchanged screen, most in stretches of three or more: each
    action "did" something and none moved the task, which neither the no-effect memory nor the circle
    check can see."""
    from tests.test_flex import FakeBackend
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    back = FakeBackend([{"steps": ["use the menu instead"], "inputs": {}, "try": [{"keys": "cmd+n"}]}])
    eng._planner = back
    task = eng._new_task("make it", {}, None)
    first = eng._look(task, eng._step_context(task))
    for n in range(3):
        task.steps.append(Step(n=n, action="button 「Refresh」", ok=True, events=["x"], before=f"{first.sig}:0",
                               decision={"progress_after": 0.05}))          # each did something; none moved the task
    look = eng._look(task, eng._step_context(task))
    assert "no_progress" in look.state and "3 actions" in look.state["no_progress"]
    assert back.prompts, "the planner was asked for a route from here"
    assert any("cmd+n" in v for v in look.options.values()), "and what it suggested is among the options"


def test_the_planners_blocked_is_an_opinion_until_the_decider_agrees(tmp_path):
    """A real run ended `blocked` at step 0: the planner, shown a path as ~/…, decided it "did not know the
    real user name". The decider had said "find another route". Two judgements have to agree."""
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New", "move": "rethink"}, {"pick": "New"}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "blocked": "the user must sign in first"}])
    res = eng.do("make a new document")
    assert res["status"] == "done", f"the planner's word alone ended the task: {res['status']} {res.get('reason')}"
    later = [s for s, q in d.seen if "planner_thinks" in s]
    assert later and "sign in" in later[0]["planner_thinks"], "the decider was shown the opinion"

    d = ScriptedDecider([{"pick": "New", "move": "rethink"}, {"pick": "New", "move": "blocked"}])
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "blocked": "the user must sign in first"}])
    res = eng.do("make a new document")
    assert res["status"] == "blocked" and "sign in" in res["reason"], "when the decider agrees, the planner's reason is the reason"


def test_done_needs_a_second_opinion_after_acting(tmp_path):
    """The decider judged "done" in the same request that chose the step, on a bar set low after acting.
    Real runs ended done with the Open dialog up instead of Settings, and with 144 still on the display."""
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New"}, {"pick": "done", "done": 0.95}, {"pick": "Refresh"}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=d)
    back = FakeBackend([{"done": False, "why": "the document window is not open yet"}, {"done": True, "why": ""}])
    eng._planner = back
    res = eng.do("make a new document")
    assert res["status"] == "done" and [s[:12] for s in res["steps"]] == ["menu File ▸ ", "button 「Refr"], res["steps"]
    assert res["outputs"]["done_doubted"] == ["the document window is not open yet"]
    redecided = [s for s, q in d.seen if "not_done_yet" in s]
    assert redecided and "not open yet" in redecided[0]["not_done_yet"], "the doubt is what the decider is sent back with"
    assert len(back.prompts) == 2


def test_the_second_opinion_is_bounded_and_optional(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New"}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"planner": {"second_opinion_on_done": False}}), helper=EnglishMac(), decider=d)
    back = FakeBackend([{"done": False, "why": "never asked"}])
    eng._planner = back
    assert eng.do("make a new document")["status"] == "done" and not back.prompts
