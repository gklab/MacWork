"""What must hold, checked where it must hold — and said, never assumed.

A run on a real Mac found engine bugs no reading of the code had: a switch recorded as taken while the old
app was still in front, an observation whose ids collided. A broken invariant is a fact on the task, not
an exception: the task goes on, and the report says "engine bug" instead of leaving it in a trace.
"""

from macwork.engine import Engine
from macwork.invariants import check_ending, check_look, check_step
from macwork.model import Affordance, Step, Task
from tests.english_mac import EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def test_a_look_must_offer_things_that_can_be_remembered_and_told_apart():
    task = Task(goal="x")
    check_look(task, [Affordance("w1", "window", "press", "button 「OK」", {}), Affordance("w1", "window", "press", "button 「Cancel」", {}),
                      Affordance("w2", "window", "press", "", {})], "sig", {"pid": 1})
    said = task.outputs["invariants_broken"]
    assert any("same id twice" in x for x in said) and any("nothing to remember" in x for x in said)
    clean = Task(goal="x")
    check_look(clean, [Affordance("w1", "window", "press", "button 「OK」", {})], "sig", {"pid": 1})
    assert "invariants_broken" not in clean.outputs


def test_a_step_must_say_where_it_was_taken_from_and_what_it_promised():
    task = Task(goal="x")
    check_step(task, Step(0, "button 「OK」", ok=True, before=None, promise="", kept=False), had_look=True)
    said = task.outputs["invariants_broken"]
    assert len(said) == 3 and any("no promise" in x for x in said) and any("recorded ok" in x for x in said)


def test_an_ending_must_be_known_and_explained():
    task = Task(goal="x")
    check_ending(task, "exploded")
    check_ending(task, "failed")
    assert task.outputs["invariants_broken"] == ["ended in a status nobody knows: 'exploded'", "ended failed with no reason", "ended failed with no cause"]


def test_a_whole_run_breaks_no_invariant(tmp_path):
    """The checks run on every look, step and ending of every task; an ordinary run breaks none."""
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([{"pick": "New"}, {"pick": "done", "done": 0.95}]))
    res = eng.do("make a new document")
    assert res["status"] == "done" and "invariants_broken" not in res["outputs"]


def test_the_report_counts_runs_with_a_broken_invariant(tmp_path):
    from macwork import evals
    rows = [{"id": "a", "category": "-", "status": "done", "passed": True, "valid": True, "seconds": 1.0, "decider_calls": 1,
             "cost_usd": 0.0, "why": "", "invariants_broken": ["step 2 made no promise"]},
            {"id": "b", "category": "-", "status": "done", "passed": True, "valid": True, "seconds": 1.0, "decider_calls": 1,
             "cost_usd": 0.0, "why": "", "invariants_broken": []}]
    summary = evals._report(tmp_path / "s.yaml", b"", rows, 1, tmp_path)["summary"]
    assert summary["runs_with_broken_invariants"] == 1
    assert "broken engine invariant" in next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
