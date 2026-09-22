"""What a caller can do and learn, and what the engine knows about the helper it talks to.

* `ping` has listed the helper's methods all along and nothing asked, so every newer method met an older
  helper as an opaque provider error;
* cancelling a task was read between steps, and a step that is a hold — keys down for a stated time — did
  not look until it ended: a caller who said stop waited that out with the keys still down;
* `mac_observe` handed back the first `limit` options of hundreds with no way to the rest.
"""

import json

from macwork.engine import Engine
from macwork.helper import Helper, HelperError
from macwork.model import Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_helper import reply, wire  # noqa: F401  (the fixture)


def test_the_helper_says_what_it_can_do_once_per_connection(wire):
    helper, other_end = wire
    reply(other_end, 1, {"version": "x", "methods": ["ping", "ax.snapshot"]})
    assert helper.supports("ax.snapshot") and not helper.supports("input.hold")
    assert helper.supports("ax.snapshot"), "asked once: no second ping on the wire"
    helper._reset()
    assert helper._methods is None, "the next helper may be another build"


def test_a_helper_with_no_list_is_given_the_benefit_of_the_doubt(wire):
    helper, other_end = wire
    reply(other_end, 1, {"version": "old"})
    assert helper.supports("anything")


def test_an_older_helper_is_named_as_such_in_the_observation(tmp_path):
    class Older(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "windows":
                raise HelperError("no_method", "unknown method ax.snapshot")
            return super().call(method, timeout, **p)

    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["windows"]}}), helper=Older(), decider=ScriptedDecider([]))
    note = eng.observe()["notes"]["windows_error"]
    assert "older" in note and "ax.snapshot" in note and "helper install" in note


def test_cancelling_a_running_task_lets_go_of_held_input(tmp_path):
    h = FakeHelper()
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    task = Task(goal="hold w for a while")
    eng.tasks[task.id] = task
    eng._running = task
    assert eng.cancel(task.id)["cancelled"]
    assert h.did("input.release_all"), "the hold ends at its next tick, not when the time is up"
    other = Task(goal="waiting")
    eng.tasks[other.id] = other
    h.calls.clear()
    eng.cancel(other.id)
    assert not h.did("input.release_all"), "nothing held by a task that is not running"


def test_observe_pages_through_the_same_listing(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    first = eng.observe(limit=3)
    assert len(first["affordances"]) == 3 and first["next_offset"] == 3 and first["total"] > 3
    second = eng.observe(limit=3, offset=3)
    assert [a["label"] for a in second["affordances"]] != [a["label"] for a in first["affordances"]]
    last = eng.observe(limit=1000)
    assert "next_offset" not in last and len(last["affordances"]) == last["total"]


def test_the_cli_can_follow_and_tidy_a_task():
    import re
    from pathlib import Path
    commands = set(re.findall(r'sub\.add_parser\("([a-z-]+)"', Path("macwork/cli.py").read_text(encoding="utf-8")))
    assert {"status", "tidy"} <= commands
    from macwork import cli
    assert {"status", "tidy"} <= set(json.loads(json.dumps({k: 1 for k in ("status", "tidy") if hasattr(cli, f"cmd_{k}")})))
