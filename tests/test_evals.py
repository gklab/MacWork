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


def test_the_harness_opens_an_app_the_suites_name_by_bundle_id(_no_real_processes, monkeypatch):
    """The suites name apps the way the engine resolves them, and the harness has to resolve them the same
    way. It did not: `_launch` called a function it never imported, so every run died on its first task —
    436 tests passed over a harness that could not start one."""
    class _Helper:
        def call(self, method, **kw):
            return [] if method == "apps.running" else []

    class _Engine:
        helper = _Helper()

        def _resolve_app(self, hint, running):
            return None if not running else {"pid": 7}

        def _installed_named(self, hint):
            return {"name": "计算器", "bundle_id": "com.apple.calculator", "path": "/System/Applications/Calculator.app"}

    monkeypatch.setattr(evals.time, "sleep", lambda s: None)   # the harness waits for the app; the test need not
    evals._launch(_Engine(), "com.apple.calculator")
    assert ["open", "-g", "-a", "/System/Applications/Calculator.app"] in _no_real_processes


def test_a_run_whose_decider_changed_halfway_is_not_counted(tmp_path, monkeypatch):
    """The decider has no equal fallback, by design: when the one in front cannot answer, the next takes over
    and stays. A real suite lost its network mid-run and scored the fallback for the rest — 14 of 28 "passes"
    that were partly measuring a different model. A run decided by two deciders is not a run."""
    class Swapping:
        """Answers once as one decider, then reports itself as another — a mid-run switch."""
        name = "jev"
        calls, cost_usd, last_ms = 0, 0.0, 1.0

        def decide(self, state, questions):
            Swapping.name = "local:local"
            raise AssertionError("not reached: the engine is stubbed below")

    class Engine:
        decider = Swapping()
        cfg = evals_cfg = None
        cache: dict = {}
        models = None

        def do(self, *a, **k):
            self.decider.name = "local:local"    # what ChainDecider does when the one in front dies
            return {"status": "done", "steps": [], "decider": {"calls": 3, "cost_usd": 0.01}}

    suite = tmp_path / "s.yaml"
    suite.write_text("fresh: false\ntasks:\n  - id: t1\n    goal: do something\n    check:\n      expect_status: [done]\n",
                     encoding="utf-8")

    from tests.test_engine import cfg as engine_cfg
    engine = Engine()
    engine.cfg = engine_cfg(tmp_path)
    monkeypatch.setattr(evals, "_locked", lambda e: False)
    monkeypatch.setattr(evals, "_desktop", lambda e: {})
    monkeypatch.setattr(evals, "sweep", lambda e, before: [])
    monkeypatch.setattr(evals, "cleanup", lambda e, t, key: None)
    report = evals.run_suite(engine, suite, out_dir=tmp_path)

    row = report["rows"][0] if "rows" in report else report["results"][0]
    assert row["status"] == "error" and "decider changed mid-run" in row["why"]
    summary = report["summary"]
    assert summary["errors"] == 1 and summary["passed"] == 0 and summary["valid"] == 0


def test_the_harness_reads_why_a_task_ended_and_not_the_wording(tmp_path, monkeypatch):
    """"The service was unreachable" and "the screen was locked" say nothing about ability and are not
    counted. The harness told them apart by phrases in `reason` — prose written for a person, in the loop —
    so a reworded message would have counted an outage as a failure. `cause` is for the harness."""
    class Decider:
        name, calls, cost_usd, last_ms = "jev", 0, 0.0, 1.0

    class Engine:
        decider = Decider()
        cfg = None
        cache: dict = {}
        models = None
        helper = None

        def __init__(self, cause):
            self.cause = cause

        def do(self, *a, **k):
            return {"status": "failed", "reason": "worded however the loop likes", "cause": self.cause,
                    "steps": [], "decider": {"calls": 1, "cost_usd": 0.0}}

        def feedback(self, *a, **k): ...
        def tidy(self, *a, **k): return {}

    suite = tmp_path / "s.yaml"
    suite.write_text("fresh: false\ntasks:\n  - id: t1\n    goal: do something\n    check:\n      expect_status: [done]\n", encoding="utf-8")
    from tests.test_engine import cfg as engine_cfg
    for m in ("_locked", "_desktop", "sweep", "cleanup", "_leftovers"):
        monkeypatch.setattr(evals, m, lambda *a, **k: [] if m in ("sweep", "_leftovers") else False if m == "_locked" else {})

    def run(cause):
        e = Engine(cause)
        e.cfg = engine_cfg(tmp_path)
        return evals.run_suite(e, suite, out_dir=None)["rows"][0]

    assert run("decider_unreachable")["status"] == "error", "an outage is not a failure"
    assert run("screen_locked")["status"] == "error"
    assert run("budget")["status"] == "failed", "running out is the engine's own doing, and counts"


def test_a_pass_by_answering_without_finishing_is_counted_apart(tmp_path):
    """The engine reports `done` when the question is answered though the task itself flailed. That is the
    right thing to tell a caller and a different thing to add up: a suite where half the passes are this
    is not the suite the total describes."""
    rows = [{"id": "a", "category": "-", "status": "done", "passed": True, "valid": True, "seconds": 1.0, "decider_calls": 1,
             "cost_usd": 0.0, "answered_anyway": True},
            {"id": "b", "category": "-", "status": "done", "passed": True, "valid": True, "seconds": 1.0, "decider_calls": 1,
             "cost_usd": 0.0, "answered_anyway": False}]
    summary = evals._report(tmp_path / "s.yaml", b"", rows, 1, tmp_path)["summary"]
    assert summary["passed_tasks"] == 2 and summary["passed_by_answering_anyway"] == 1
    assert "1 passed by answering without finishing" in next(tmp_path.glob("*.md")).read_text(encoding="utf-8")


def test_the_baseline_is_where_compare_looks(tmp_path):
    assert evals.baseline_for("evals/behaviour.yaml").name == "behaviour.json"
    assert evals.baseline_for("evals/behaviour.yaml").exists(), "the committed baseline for the behaviour suite"
    assert evals.baseline_for("evals/behaviour.yaml").parent.name == "baseline"


def test_a_report_never_names_this_user(tmp_path, monkeypatch):
    """A report is meant to be committed, and a trace names the files a task opened — under the home
    directory, by user name. The first committed baseline carried `/Users/<name>` twice."""
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/somebody")))
    rows = [{"id": "a", "category": "-", "status": "done", "passed": True, "valid": True, "seconds": 1.0, "decider_calls": 1,
             "cost_usd": 0.0, "trace": ["open the file /Users/somebody/Library/Caches/x.txt"]}]
    evals._report(tmp_path / "s.yaml", b"", rows, 1, tmp_path)
    for f in tmp_path.glob("*.json"):
        assert "/Users/somebody" not in f.read_text(encoding="utf-8")
        assert "~/Library/Caches/x.txt" in f.read_text(encoding="utf-8")


def test_the_headline_counts_runs_and_tasks_that_count(tmp_path):
    """`runs` was overwritten by the valid count and then had the invalid rows subtracted again: a ×3 run
    with six invalid rows read "17/12 runs". And a task invalid every time is not one the run speaks about."""
    def row(tid, status, passed, valid=True):
        return {"id": tid, "category": "-", "status": status, "passed": passed, "valid": valid, "seconds": 1.0,
                "decider_calls": 1, "cost_usd": 0.0, "why": ""}
    rows = [row("a", "done", True), row("a", "done", True), row("b", "failed", False), row("b", "done", True),
            row("c", "invalid", False, valid=False), row("c", "invalid", False, valid=False)]
    summary = evals._report(tmp_path / "s.yaml", b"", rows, 2, tmp_path)["summary"]
    assert (summary["runs"], summary["valid"], summary["invalid"], summary["passed"]) == (6, 4, 2, 3)
    assert (summary["tasks"], summary["tasks_listed"], summary["passed_tasks"]) == (2, 3, 1)
    md = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "1/2 tasks passed" in md and "3/4 runs" in md and "1 of 3 tasks never counted" in md


def test_today_is_something_the_harness_knows():
    import time as _time
    assert evals._sub({"check": {"answer_contains": ["{day}"]}}, "")["check"]["answer_contains"] == [str(_time.localtime().tm_mday)]
