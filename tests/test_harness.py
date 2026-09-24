"""The measuring instrument, measured.

The project's own criterion is that the decider's accuracy is something to measure rather than assume, so
the harness is load-bearing — and three things in it bent the number the same way, upwards:

  * `fresh: true` promises "every task is solved by reasoning on the live screen, not by recall". It
    emptied the app models once, at the start of the suite. Task 2 ran on what task 1 learned, and the
    safety-floor verdict cache spanned all 29 tasks and all 3 repeats.
  * A task that ends `need_continue` was scored a failure, although both the CLI and the MCP server resume
    it without being asked. Being long was counted as being wrong.
  * A must-not task can pass on the engine's own report of what it did, with nothing checked in the world.
"""

import pathlib

import pytest
import yaml

from macwork import evals


# --------------------------------------------------------------------------- fresh means fresh

def test_recall_is_cleared_between_tasks_not_once_per_suite():
    import inspect
    src = inspect.getsource(evals)
    assert "_forget" in src, "nothing resets recall per task"
    assert "_forget(engine)" in inspect.getsource(evals._run_task), \
        "recall is reset for the suite but not between its tasks"


def test_what_forgetting_covers():
    """Anything the engine carries from one task to the next that stands in for having seen the screen."""
    class Engine:
        def __init__(self):
            self.cache = {"floor.verdicts": {"a": 1}, "floor.harmless": {"a": True}, "floor.gated": {"a": ["delete"]},
                          "ambient": {"42|Clock"}, "vision.ocr": {"a": 1}, "apps.installed": (0, ["Mail"]), "entities": {"x": []}}
            self.cfg = __import__("macwork.config", fromlist=["Config"]).Config.load()
            self.models = None

    eng = Engine()
    evals._forget(eng)
    assert not eng.cache["floor.verdicts"], "a classification from an earlier task was kept"
    assert not eng.cache["floor.harmless"]
    assert not eng.cache["floor.gated"], "a gate from an earlier task held its question gated in the next"
    assert not eng.cache["vision.ocr"], "a screen read in an earlier task was kept"
    assert not eng.cache.get("ambient"), "a window judged to change by itself in an earlier task stayed judged so"
    # not recall: what is installed is a fact about the Mac, and re-reading it costs 1.8 s a task
    assert eng.cache["apps.installed"], "the installed-app list was thrown away; that is not recall"
    assert eng.cache["entities"], "the pseudonym cache is privacy, not recall"


# --------------------------------------------------------------------------- being long is not being wrong

def test_a_task_that_asks_to_be_continued_is_continued():
    """`need_continue` means "getting somewhere, out of budget for this turn". Every real caller resumes
    it; a harness that scores it a failure is measuring the budget, not the engine."""
    import inspect
    assert "need_continue" in inspect.getsource(evals._run_task), \
        "the harness does not mention the status its own engine returns for a long task"


def test_how_many_times_it_will_continue_is_bounded():
    import inspect
    src = inspect.getsource(evals._run_task)
    assert "max_continues" in src or "continues" in src


# --------------------------------------------------------------------------- a must-not needs a witness

SUITES = sorted(pathlib.Path("evals").glob("*.yaml"))


def must_nots():
    out = []
    for path in SUITES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out += [(path.name, t) for t in (doc.get("tasks") or [])
                if str(t.get("category", "")).startswith("E")]
    return out


@pytest.mark.parametrize("name,task", must_nots(), ids=lambda x: x if isinstance(x, str) else x.get("id", "?"))
def test_a_must_not_task_checks_the_world_and_not_only_the_report(name, task):
    """A task that only asserts `expect_status` passes if the engine does the thing and then says it did
    not. For the four tasks whose whole point is that something must not happen, the engine's own account
    of itself is not evidence."""
    checks = set(task.get("check") or {})
    witnesses = checks - {"expect_status"}
    assert witnesses, f"{name}:{task['id']} is scored on the engine's self-report alone"


def test_every_must_not_task_in_v2_names_what_it_must_stop_at():
    """Any stop passed a must-not task, whatever it held: a sign-in task passed on a menu item held as
    unclassified, and an overwrite task on the Delete key held before the save. Each now names the floor
    categories its stop must be for, and they are categories the floor has."""
    from macwork.config import Config
    categories = set((Config.load().policy.get("confirm") or {}).get("categories") or {})
    doc = yaml.safe_load(pathlib.Path("evals/v2.yaml").read_text(encoding="utf-8"))
    for task in [t for t in doc["tasks"] if str(t.get("category", "")).startswith("E")]:
        named = (task.get("check") or {}).get("held_for")
        assert named, f"{task['id']} passes on any stop"
        assert set(named) <= categories, f"{task['id']} names {set(named) - categories}, which the floor never gives"
