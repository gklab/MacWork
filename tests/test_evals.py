"""The suites themselves, checked without running them.

A task is only exercised on a real Mac with a real decider, which makes a mistake in one of these files the
kind nobody finds until the day it matters. Two were found this way: `v2.yaml` — the comprehensive suite,
29 tasks over navigation, multi-step, cross-app, answers, must-not and injection — had gone unmentioned
while its weaker siblings were being reported; and every suite named apps by display name, so on an English
Mac 8 of 11 did not resolve and no eval could run at all. The measuring tool was making exactly the
assumption the engine is not allowed to make.
"""

import pathlib
import re

import pytest
import yaml

from macwork import evals

SUITES = sorted(pathlib.Path("evals").glob("*.yaml"))

# what check() and cleanup() in evals.py actually read
CHECKS = {"screen_contains", "window_contains", "frontmost", "screen_excludes", "trace_excludes",
          "output_contains", "answer_contains", "file_exists", "file_missing", "file_contains", "expect_status"}
TASK_KEYS = {"id", "app", "check_app", "goal", "inputs", "setup", "cleanup", "check", "fixtures",
             "accept_blocked", "repeat", "skip", "why",
             "category"}      # documentary: the harness ignores it, the suites group by it
STEP_KEYS = {"activate", "key", "press", "within", "wait", "close_untitled", "dont_save", "app"}
STATUSES = {"done", "failed", "blocked", "cancelled", "need_input", "need_confirm", "ambiguous", "need_continue"}

# Apps whose bundle id could not be asked of this Mac, because it does not have them. Leave the display
# name and fill the id in on a Mac that does — typing one from memory is how a task silently stops matching.
UNRESOLVED = {"Keynote讲演", "Numbers表格", "Safari浏览器", "VLC"}


def tasks():
    out = []
    for path in SUITES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out += [(path.name, t) for t in (doc.get("tasks") or [])]
    return out


def _id(x):
    return x if isinstance(x, str) else x.get("id", "?")


def test_there_are_suites_and_they_parse():
    assert SUITES and tasks()


@pytest.mark.parametrize("name,task", tasks(), ids=_id)
def test_every_task_is_shaped_the_way_the_harness_reads_it(name, task):
    unknown = set(task) - TASK_KEYS
    assert not unknown, f"{name}:{task.get('id')} has keys the harness never reads: {unknown}"
    assert task.get("id") and task.get("goal"), f"{name}: a task needs an id and a goal"

    for key in task.get("check") or {}:
        assert key in CHECKS, f"{name}:{task['id']} checks {key!r}, which check() would reject"
    expect = (task.get("check") or {}).get("expect_status")
    for want in (expect if isinstance(expect, list) else [expect] if expect else []):
        assert want in STATUSES, f"{name}:{task['id']} expects {want!r}, which no task can end in"

    for phase in ("setup", "cleanup"):
        for step in task.get(phase) or []:
            unknown = set(step) - STEP_KEYS
            assert not unknown, f"{name}:{task['id']} {phase} step has {unknown}, which the harness ignores"

    for rx in (task.get("check") or {}).get("trace_excludes") or []:
        re.compile(rx)      # these are regexes: a bad one fails open, and silently


@pytest.mark.parametrize("name,task", tasks(), ids=_id)
def test_apps_are_named_in_a_way_that_works_in_any_language(name, task):
    for key in ("app", "check_app"):
        app = task.get(key)
        if not app or app in UNRESOLVED:
            continue
        assert re.fullmatch(r"[a-z][a-z0-9-]*(\.[A-Za-z0-9._-]+)+", app), \
            f"{name}:{task['id']} names {app!r} by display name, which resolves in one language only"


@pytest.mark.parametrize("name,task", tasks(), ids=_id)
def test_a_task_that_makes_files_declares_them(name, task):
    """`{eval_dir}` points somewhere only because the harness built a sandbox from `fixtures`."""
    if "{eval_dir}" in yaml.safe_dump(task, allow_unicode=True):
        assert task.get("fixtures"), f"{name}:{task['id']} uses {{eval_dir}} but declares no fixtures"


def test_the_harness_substitutes_the_sandbox_path():
    task = {"goal": "open {eval_dir}/a.txt", "check": {"file_exists": ["{eval_dir}/a.txt"]}}
    out = evals._sub(task, "/tmp/box")
    assert out["goal"] == "open /tmp/box/a.txt" and out["check"]["file_exists"] == ["/tmp/box/a.txt"]


def test_the_comprehensive_suite_is_still_there_and_still_comprehensive():
    v2 = [t for name, t in tasks() if name == "v2.yaml"]
    assert len(v2) >= 25
    assert {t.get("category") for t in v2} >= {"E must-not", "F injection"}
    checks = {k for t in v2 for k in (t.get("check") or {})}
    assert {"trace_excludes", "answer_contains", "file_exists", "expect_status"} <= checks


def test_the_later_suite_covers_only_what_the_frozen_one_predates():
    later = [t for name, t in tasks() if name == "behaviour.yaml"]
    assert later, "nothing covers what v2 predates"
    assert any(t.get("inputs") for t in later), "v2 passes caller-supplied text in no task at all"
    expected = " ".join(str((t.get("check") or {}).get("expect_status", "")) for t in later)
    assert "need_continue" in expected, "the status a long job now ends in is untested"
