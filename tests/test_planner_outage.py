"""When the planner is not there, the run says so.

The five tasks of a v2 run on 09-20 asked the local planner 9 times and got no plan back; they ended no_route,
need_input and failed with no cause, and the run scored 0/5 as if the engine had failed five tasks. Nothing
recorded what stopped the planner answering, or how long any ask took.
"""

import json
import sys
import types
import urllib.error

import pytest

from macwork import evals
from macwork.engine import Engine
from macwork.planner import AnthropicPlanner, OpenAICompatPlanner, Planning, PlannerError
from macwork.privacy import Audit
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def failing(exc):
    def post(path, body, timeout):
        raise exc
    return post


def test_what_stopped_a_planner_answering_is_said(tmp_path):
    local = OpenAICompatPlanner(cfg(tmp_path), "local", {"base_url": "http://127.0.0.1:9/v1", "local": True, "stream": False})
    cases = {"unreachable": urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
             "reply": urllib.error.HTTPError("http://127.0.0.1:9/v1", 500, "Internal Server Error", {}, None),
             "refused": urllib.error.HTTPError("http://127.0.0.1:9/v1", 401, "Unauthorized", {}, None),
             "timeout": urllib.error.URLError(TimeoutError("timed out"))}
    for kind, exc in cases.items():
        local._post = failing(exc)
        with pytest.raises(PlannerError) as got:
            local.complete("system", "plan something", {"properties": {}})
        assert got.value.kind == kind, (kind, str(got.value))


class _APIConnectionError(Exception):
    pass


class _APITimeoutError(_APIConnectionError):     # as in the SDK: a timeout is a kind of connection error
    pass


class _APIStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(status_code)
        self.status_code, self.message = status_code, "no"


def test_an_anthropic_timeout_is_a_timeout_and_not_a_lost_connection(tmp_path, monkeypatch):
    sdk = types.SimpleNamespace(APIConnectionError=_APIConnectionError, APITimeoutError=_APITimeoutError,
                                APIStatusError=_APIStatusError)
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    planner = AnthropicPlanner(cfg(tmp_path))
    for exc, kind in ((_APITimeoutError("slow"), "timeout"), (_APIConnectionError("gone"), "unreachable"),
                      (_APIStatusError(403), "refused"), (_APIStatusError(529), "reply")):
        def create(*a, exc=exc, **k):
            raise exc
        planner._client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create)))
        with pytest.raises(PlannerError) as got:
            planner.complete("system", "plan", {"type": "object"})
        assert got.value.kind == kind


class Down:
    """A local planner whose server is not there."""

    name, local, model = "local", True, ""

    def complete(self, system, prompt, schema):
        refused = PlannerError("local: <urlopen error [Errno 61] Connection refused>")
        refused.kind = "unreachable"
        raise refused


def test_a_task_whose_planner_could_not_be_reached_ends_with_that_cause(tmp_path):
    d = ScriptedDecider([{"pick": "New Document", "move": "blocked"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = Down()
    res = eng.do("make a new document")
    assert res["status"] == "blocked" and res["cause"] == "planner_unreachable", res
    assert res["planner"]["calls"] == 1 and res["planner"]["answered"] == 0 and res["planner"]["errors"]["unreachable"] == 1
    ended = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r.get("cause") for r in ended if r["kind"] == "task"] == ["planner_unreachable"], "the audit says why it ended"

    d = ScriptedDecider([{"pick": "New Document", "move": "blocked"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = Down()
    task = eng._new_task("make a new document", {}, None)
    task.planner_use.update({"calls": 2, "answered": 1, "failed": 1, "errors": {"unreachable": 1}})
    assert eng._finish(task, "blocked", "only the user can go on", cause="needs_user")["cause"] == "needs_user", \
        "a planner that answered once was there"
    task = eng._new_task("make a new document", {}, None)
    task.planner_use.update({"calls": 1, "answered": 0, "failed": 1, "errors": {"unreachable": 1}})
    assert eng._finish(task, "failed", "out of steps", cause="budget")["cause"] == "budget", "a spent budget says more"


def test_a_run_whose_planner_was_down_is_an_error_and_one_that_lost_an_ask_is_not(tmp_path, monkeypatch):
    class Decider:
        name, calls, cost_usd, last_ms = "jev", 0, 0.0, 1.0

    down = {"status": "failed", "cause": "planner_unreachable", "reason": "no route to the goal was found", "task_id": "a",
            "steps": [], "decider": {"calls": 3, "cost_usd": 0.0},
            "planner": {"calls": 2, "answered": 0, "failed": 2, "errors": {"unreachable": 2}}}
    lost_one = {"status": "failed", "cause": "no_route", "reason": "no route to the goal was found", "task_id": "b",
                "steps": ["a step"], "decider": {"calls": 3, "cost_usd": 0.0},
                "planner": {"calls": 2, "answered": 1, "failed": 1, "errors": {"timeout": 1}}}
    script = [down, lost_one]

    class Eng:
        decider = Decider()
        cfg = None
        cache: dict = {}
        models = None
        helper = None

        def do(self, *a, **k):
            return script.pop(0)

        def feedback(self, *a, **k): ...
        def tidy(self, *a, **k): return {}

    suite = tmp_path / "s.yaml"
    suite.write_text("fresh: false\ntasks:\n  - {id: t1, goal: do something, check: {expect_status: [done]}}\n", encoding="utf-8")
    for m in ("_locked", "_desktop", "sweep", "cleanup", "_leftovers"):
        monkeypatch.setattr(evals, m, lambda *a, m=m, **k: [] if m in ("sweep", "_leftovers") else False if m == "_locked" else {})
    monkeypatch.setattr(evals.time, "sleep", lambda s: None)
    e = Eng()
    e.cfg = cfg(tmp_path)
    report = evals.run_suite(e, suite, out_dir=tmp_path, repeat=2)
    first, second = report["rows"]
    assert first["status"] == "error" and first["error"] == "planner_unreachable" and not first["valid"]
    assert second["status"] == "failed" and second["valid"], "a planner that failed once and then answered is not an outage"
    s = report["summary"]
    assert s["errors_by"] == {"planner_unreachable": 1} and s["valid"] == 1
    assert s["planner_failures"] == {"runs": 2, "asks": 3, "by_kind": {"timeout": 1, "unreachable": 2}}
    assert "planner_unreachable 1" in next(tmp_path.glob("*.md")).read_text(encoding="utf-8")


class Answers:
    name, local = "fake-cloud", False

    def complete(self, system, prompt, schema):
        return {"steps": [{"goal": "open it", "evidence": "it is open"}], "inputs": {}, "try": [], "blocked": ""}


def test_every_planner_ask_is_timed_in_the_audit(tmp_path):
    c = cfg(tmp_path)
    audit = Audit(c)
    use: dict = {}
    Planning(c, Answers(), None, audit, "t1", usage=use).plan("open it", {})
    with pytest.raises(PlannerError):
        Planning(c, Down(), None, audit, "t1", usage=use).plan("open it", {})
    plans = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(isinstance(r.get("ms"), int), r.get("error")) for r in plans] == [(True, None), (True, "unreachable")]
    assert (use["calls"], use["answered"], use["failed"]) == (2, 1, 1)
    assert use["errors"] == {"unreachable": 1, "timeout": 0, "refused": 0, "reply": 0}
    assert use["by"] == {"fake-cloud": 1, "local": 1}


def test_output_contains_never_matches_planner_usage_or_timing(tmp_path):
    """unseen.yaml checks a web task's outputs for "2025". What the planner cost is not an output."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([{"pick": "New Document", "move": "rethink"},
                                                                               {"pick": "done"}]))
    eng._planner = Answers()
    res = eng.do("make a new document")
    task = eng.tasks[res["task_id"]]
    assert res["planner"]["calls"] >= 1, "what the planner was asked is part of the result"
    task.planner_use["ms"] = 2025
    res = task.result()
    assert res["planner"]["ms"] == 2025 and "planner" not in (res.get("outputs") or {})
    ok, why = evals.check(eng, {"check": {"output_contains": ["2025"]}}, res)
    assert not ok, "an output check matched how long the planner took"
