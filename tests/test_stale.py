"""A pid is a handle to a process that was running then, not to the app it belonged to.

`store.py` opens with "What is stored is the task's own state, never a live handle: pids, window numbers
and Accessibility references belong to the machine as it was and are deliberately left behind." `dump`
stores every field but `held`, so `desktop.initial_pids`, `desktop.opened` (keyed by pid) and
`task.target` all survive — and macOS reuses pids. A task resumed after a restart could quit whatever
process now holds the number its app had.

The fix is not to stop storing what a task opened — that record is the only thing that lets tidying touch
its own doing and nothing else. It is to check, at the moment of acting, that the process at that pid is
still the app the task opened.
"""

from typing import Any

import pytest

from macwork.config import Config
from macwork.model import Task
from macwork.store import dump, load


class Helper:
    def __init__(self, running):
        self.running = running
        self.quit: list[int] = []

    def call(self, method, timeout=30.0, **p):
        if method == "apps.running":
            return self.running
        if method == "apps.quit":
            self.quit.append(p["pid"])
            return {"terminated": True}
        raise AssertionError(method)


def engine(tmp_path, helper):
    from macwork.engine import Engine
    from tests.test_engine import ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=helper, decider=ScriptedDecider([]))
    return eng


# --------------------------------------------------------------------------- what survives a restart

def test_the_machine_as_it_was_is_not_carried_over():
    task = Task(goal="x", id="t1")
    task.desktop.initial_pids = {101, 102}
    task.desktop.initial_windows = {101: {5}}
    task.target = {"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"}
    back = load(dump(task))
    assert back.desktop.initial_pids is None, "the pids running before the task came back from disk"
    assert back.desktop.initial_windows is None
    assert not (back.target or {}).get("pid"), "a live pid came back as the app to work in"


def test_what_the_task_opened_does_survive_because_tidy_needs_it():
    task = Task(goal="x", id="t2")
    task.desktop.opened = {101: {"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"}}
    back = load(dump(task))
    assert back.desktop.opened, "the record of what the task opened was thrown away"
    assert "com.apple.mail" in str(back.desktop.opened)


# --------------------------------------------------------------------------- and is checked before use

def test_a_reused_pid_is_not_quit(tmp_path):
    """The number is the same and the process is not."""
    helper = Helper([{"pid": 101, "name": "Safari", "bundle_id": "com.apple.Safari"}])
    eng = engine(tmp_path, helper)
    task = Task(goal="x", id="t3")
    eng._quit(task, {"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"})
    assert helper.quit == [], "quit a process that is not the app the task opened"


def test_the_right_process_is_still_quit(tmp_path):
    helper = Helper([{"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"}])
    eng = engine(tmp_path, helper)
    assert eng._quit(Task(goal="x", id="t4"), {"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"})
    assert helper.quit == [101]


def test_a_pid_that_is_gone_is_not_an_error(tmp_path):
    helper = Helper([])
    eng = engine(tmp_path, helper)
    eng._quit(Task(goal="x", id="t5"), {"pid": 101, "name": "Mail", "bundle_id": "com.apple.mail"})
    assert helper.quit == []


def test_an_entry_with_no_bundle_id_is_left_alone(tmp_path):
    """Nothing to check it against, so nothing is quit on a guess."""
    helper = Helper([{"pid": 101, "name": "Mail"}])
    eng = engine(tmp_path, helper)
    eng._quit(Task(goal="x", id="t6"), {"pid": 101, "name": "Mail"})
    assert helper.quit == []
