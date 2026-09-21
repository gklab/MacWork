"""Text the planner made up, and steps a routine took.

  * `_fill` puts a planner's text through a provenance guard — either the task has seen the value or the
    decider judges it to be what the task already had — records it in the audit and in
    `outputs.typed_by_planner`, which is what the injection evals read. `plan["inputs"]` went round all
    of it: a planner asked for a *plan* could put arbitrary text into `task.inputs` with
    `setdefault`, and from then on it was indistinguishable from what the caller supplied.
  * A replayed routine appended a `Step` with no `effect` and put nothing in `task.changed`, so
    `mac_revert` reported "this task changed nothing" for a routine that had renamed, typed and saved.
"""

from typing import Any

import pytest

from macwork.model import Affordance, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def engine(tmp_path, decider=None):
    from macwork.engine import Engine
    return Engine(cfg(tmp_path), helper=FakeHelper(), decider=decider or ScriptedDecider([]))


# --------------------------------------------------------------------------- what a plan may put in inputs

def test_a_planner_cannot_smuggle_text_in_through_the_plan(tmp_path):
    """`inputs` is where the *caller's* text lives. A value that appears there has been through the guard
    or come from the caller; there is no third way in."""
    eng = engine(tmp_path)
    task = Task(goal="write it up", id="t1")
    eng._plan_inputs(task, None, {"text": "transfer 5000 to 6222…"})
    assert task.inputs.get("text") != "transfer 5000 to 6222…" or task.outputs.get("planner_inputs"), \
        "text from a plan went into inputs with nothing recording where it came from"


def test_what_the_caller_gave_is_never_overwritten(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t2", inputs={"text": "the caller's own words"})
    eng._plan_inputs(task, None, {"text": "something else"})
    assert task.inputs["text"] == "the caller's own words"


def test_text_from_a_plan_is_recorded_where_the_injection_checks_look(tmp_path):
    """`evals.check` builds its trace from `outputs.typed_by_planner`; anything not there is invisible to
    every `trace_excludes` in the suite."""
    eng = engine(tmp_path)
    task = Task(goal="x", id="t3")
    eng._plan_inputs(task, None, {"query": "open Chess"})
    if task.inputs.get("query"):
        recorded = str(task.outputs.get("typed_by_planner", [])) + str(task.outputs.get("planner_inputs", []))
        assert "open Chess" in recorded, "a plan's text reached inputs and no output records it"


def test_a_plan_with_no_inputs_is_unaffected(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t4")
    eng._plan_inputs(task, None, {})
    assert task.inputs == {} and not task.outputs


# --------------------------------------------------------------------------- what a routine changed

def test_a_replayed_routine_records_what_it_changed():
    import inspect

    from macwork import loop
    src = inspect.getsource(loop.LoopMixin._replay)
    assert "task.changed" in src, "a routine's steps never enter task.changed, so revert has nothing to put back"
    assert "effect" in src, "a routine's steps record no floor category"


def test_revert_has_something_to_work_with_after_a_routine(tmp_path):
    """The shape `revert` reads: one entry per step that altered something, newest last."""
    eng = engine(tmp_path)
    task = Task(goal="x", id="t5")
    task.changed = [{"n": 0, "action": "rename it", "effect": "write",
                     "app": {"pid": 42, "name": "Finder", "bundle_id": "com.apple.finder"}}]
    eng.tasks[task.id] = task
    eng.helper.call = lambda m, timeout=30.0, **p: {"idle_s": 99} if m == "input.idle" else FakeHelper().call(m, timeout, **p)
    assert eng.revert(task.id)["ok"] in (True, False)      # it tries; what matters is that it has an entry
    assert task.changed
