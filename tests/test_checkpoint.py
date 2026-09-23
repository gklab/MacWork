"""A run's rows are kept as it goes, and a report is what its rows say.

The v2 run of 09-23 03:29 finished 13 tasks, was stopped with Ctrl-C, and left no report at all: the report
was written only after the last task, and Ctrl-C swept the desktop while the engine's worker thread was still
driving the Mac. Now each row is on disk before the next task starts, an interrupted run cancels its task,
waits for the Mac, sweeps, and writes what it has, and a report can be rebuilt from rows files alone.
"""

import json

import pytest

from macwork import evals


class Decider:
    name, calls, cost_usd, last_ms = "jev", 0, 0.0, 1.0


class Engine:
    """Stands in for the engine: `do` answers from a script; queue, cancel and busy say what ran."""

    decider = Decider()
    cache: dict = {}
    models = None
    helper = None

    def __init__(self, cfg, script, events=None):
        self.cfg, self.script, self.events = cfg, list(script), events if events is not None else []

    def do(self, goal, *a, **k):
        step = self.script.pop(0)
        return step(goal) if callable(step) else step

    def feedback(self, *a, **k): ...
    def tidy(self, *a, **k): return {}

    def queue(self):
        return [{"task_id": "running-task", "running": True}]

    def cancel(self, task_id):
        self.events.append(("cancel", task_id))

    def busy(self):
        return False


def done(steps=1, passed=True):
    return {"status": "done" if passed else "failed", "task_id": "x", "steps": ["a step"] * steps,
            "decider": {"calls": 2, "cost_usd": 0.0}}


@pytest.fixture
def suite(tmp_path, monkeypatch):
    path = tmp_path / "s.yaml"
    path.write_text("fresh: false\ntasks:\n"
                    "  - {id: t1, category: A, goal: first thing, check: {expect_status: [done]}}\n"
                    "  - {id: t2, category: B, goal: second thing, check: {expect_status: [done]}}\n", encoding="utf-8")
    for m in ("_locked", "_desktop", "cleanup", "_leftovers"):
        monkeypatch.setattr(evals, m, lambda *a, m=m, **k: [] if m == "_leftovers" else False if m == "_locked" else {})
    monkeypatch.setattr(evals.time, "sleep", lambda s: None)
    return path


def engine_cfg(tmp_path):
    from tests.test_engine import cfg
    return cfg(tmp_path)


def test_each_row_is_kept_as_it_finishes(tmp_path, suite, monkeypatch):
    monkeypatch.setattr(evals, "sweep", lambda e, before, forced=None, among=None: [])
    out = tmp_path / "reports"
    seen = []

    def second(goal):
        rows_file = next(out.glob("*.rows.jsonl"))
        seen.append([json.loads(x)["kind"] for x in rows_file.read_text(encoding="utf-8").splitlines()])
        return done()

    report = evals.run_suite(Engine(engine_cfg(tmp_path), [done(), second]), suite, out_dir=out)
    assert seen == [["run", "row"]], "the first task's row was not on disk when the second began"
    lines = [json.loads(x) for x in next(out.glob("*.rows.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert [x["kind"] for x in lines] == ["run", "row", "row", "end"]
    assert lines[0]["suite_sha256"] == report["summary"]["suite_sha256"] and lines[0]["tasks"] == ["t1", "t2"]
    stamp = next(out.glob("*.rows.jsonl")).name.removesuffix(".rows.jsonl")
    assert (out / f"{stamp}.json").exists(), "the report is written beside its rows, under the same stamp"


def test_an_interrupted_run_still_writes_its_report(tmp_path, suite, monkeypatch):
    events = []
    monkeypatch.setattr(evals, "sweep", lambda e, before, forced=None, among=None: events.append(("sweep",)) or [])

    def stopped(goal):
        raise KeyboardInterrupt

    out = tmp_path / "reports"
    try:
        report = evals.run_suite(Engine(engine_cfg(tmp_path), [done(), stopped], events), suite, out_dir=out)
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C went straight through the run: no report of the task that finished")
    assert events[-2:] == [("cancel", "running-task"), ("sweep",)], "the desktop was swept while the task still ran"
    s = report["summary"]
    assert s["runs"] == 1 and s["interrupted"]["after_runs"] == 1 and s["interrupted"]["rest"] == {"t2": 1}
    written = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert written["summary"]["interrupted"]["of_runs"] == 2
    md = next(out.glob("*.md")).read_text(encoding="utf-8")
    assert "INTERRUPTED" in md and "--only t2 --repeat 1" in md and "--report-from" in md


def _rows_file(path, header, rows=()):
    path.write_text("\n".join(json.dumps(x) for x in [{"kind": "run", **header}] + [{"kind": "row", "row": r} for r in rows]
                              + [{"kind": "end", "forced_quits": [], "left_after_run": [], "interrupted": None}]) + "\n",
                    encoding="utf-8")
    return path


def test_a_report_from_rows_files_refuses_rows_of_another_suite_or_commit(tmp_path):
    row = {"id": "t1", "category": "A", "status": "done", "passed": True, "valid": True, "seconds": 1.0,
           "decider_calls": 1, "cost_usd": 0.0}
    base = {"suite": "s.yaml", "suite_sha256": "aaaa", "repeat": 1, "tasks": ["t1"], "env": {"commit": "c1"}}
    one = _rows_file(tmp_path / "one.rows.jsonl", base, [row])
    other_suite = _rows_file(tmp_path / "two.rows.jsonl", {**base, "suite_sha256": "bbbb"}, [row | {"id": "t2"}])
    other_commit = _rows_file(tmp_path / "three.rows.jsonl", {**base, "env": {"commit": "c2"}}, [row | {"id": "t2"}])
    with pytest.raises(ValueError, match="another version of the suite"):
        evals.report_from([one, other_suite], None)
    with pytest.raises(ValueError, match="another commit"):
        evals.report_from([one, other_commit], None)
    same = _rows_file(tmp_path / "four.rows.jsonl", {**base, "tasks": ["t2"]}, [row | {"id": "t2", "passed": False}])
    merged = evals.report_from([one, same], None)["summary"]
    assert (merged["runs"], merged["passed"], merged["suite_sha256"]) == (2, 1, "aaaa")


def test_a_report_rebuilt_from_its_rows_says_what_the_run_said(tmp_path, suite, monkeypatch):
    monkeypatch.setattr(evals, "sweep", lambda e, before, forced=None, among=None: [])
    out = tmp_path / "reports"
    held = {"status": "need_confirm", "task_id": "x", "steps": [], "decider": {"calls": 1, "cost_usd": 0.0},
            "pending": {"confirm": {"label": "press the delete key"}, "because": ["delete"]}}
    report = evals.run_suite(Engine(engine_cfg(tmp_path), [done(), done(passed=False), held, done(3)]), suite,
                             out_dir=out, repeat=2)
    rebuilt = evals.report_from([next(out.glob("*.rows.jsonl"))], tmp_path / "again")
    drop = {"at"}
    assert {k: v for k, v in rebuilt["summary"].items() if k not in drop} == \
        {k: v for k, v in report["summary"].items() if k not in drop}
