"""One ledger for everything a task is allowed.

Seven counters, fourteen sites, three modules, each with its own default and its own reading of "at the
limit" — and no single answer to "what has this task used, and of what?", which is the first question
about a task that stopped.
"""

from macwork.budget import ALLOWANCES
from macwork.engine import Engine
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def engine(tmp_path, **conf):
    return Engine(cfg(tmp_path, config=conf), helper=FakeHelper(), decider=ScriptedDecider([]))


def test_an_allowance_is_spent_until_it_is_gone(tmp_path):
    eng = engine(tmp_path, engine={"max_looks": 2})
    task = eng._new_task("x", {}, None)
    assert eng.allowance_left(task, "looks") and eng.spend_allowance(task, "looks") and eng.spend_allowance(task, "looks")
    assert not eng.allowance_left(task, "looks") and not eng.spend_allowance(task, "looks"), "the third is refused and not counted"
    assert task.pace.looks == 2


def test_the_first_plan_is_not_a_replan(tmp_path):
    """`planner.max_replans: 2` allowed three plans: the first, and two more. The table says so instead of a +1
    hidden at the one site that read it."""
    eng = engine(tmp_path, planner={"max_replans": 2})
    task = eng._new_task("x", {}, None)
    assert eng.allowance("replans") == 3 and ALLOWANCES["replans"][2] == 1
    for _ in range(3):
        assert eng.spend_allowance(task, "replans")
    assert not eng.spend_allowance(task, "replans")


def test_every_allowance_is_named_once_and_read_from_the_configuration(tmp_path):
    eng = engine(tmp_path)
    for name, (key, default, free) in ALLOWANCES.items():
        assert eng.allowance(name) == int(eng.cfg.get(key, default)) + free, name
        assert hasattr(eng._new_task("x", {}, None).pace, name), f"{name} has no counter on Pace"


def test_a_task_that_stopped_says_what_it_used_of_what(tmp_path):
    eng = engine(tmp_path, engine={"max_looks": 6, "max_steps": 12})
    task = eng._new_task("x", {}, None)
    eng.spend_allowance(task, "looks"); eng.spend_allowance(task, "waits")
    res = eng._finish(task, "failed", "step budget used up")
    ledger = res["outputs"]["budget"]
    assert ledger["looks"] == {"used": 1, "of": 6} and ledger["waits"]["used"] == 1
    assert ledger["steps"] == {"used": 0, "of": 12} and "decisions" in ledger and "total_seconds" in ledger


def test_a_new_run_starts_the_allowances_over(tmp_path):
    eng = engine(tmp_path, engine={"max_waits": 1})
    task = eng._new_task("x", {}, None)
    assert eng.spend_allowance(task, "waits") and not eng.allowance_left(task, "waits")
    task.begin_run(0, 0.0)
    assert eng.allowance_left(task, "waits")
