"""One Mac, one keyboard, one front app — and a queue in front of them.

Running one task at a time is not a limitation to design away. What was wrong is that waiting for that turn
was invisible: a second `do` blocked on a lock with no id, no position and no way to change its mind, and a
Python lock is not first-come-first-served, so there was no promise about what ran next either. An MCP
client that called `mac_do` twice simply stopped responding.
"""

import threading
import time

import pytest

from macwork.engine import Engine
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Eng(Engine):
    """An engine whose loop is a sleep, so the ordering is what is under test."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ran: list[str] = []
        self.hold = threading.Event()
        self.hold.set()

    def _loop(self, task, progress):
        self.hold.wait(5)
        self.ran.append(task.goal)
        task.status = "done"


@pytest.fixture
def eng(tmp_path):
    e = Eng(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    yield e
    e.hold.set()


def test_handing_in_work_comes_back_at_once_with_an_id(eng):
    eng.hold.clear()
    out = eng.submit("the first thing")
    assert out["status"] == "queued" and out["task_id"] and out["position"] == 1


def test_the_position_is_the_answer_to_when(eng):
    """How many are ahead of me — the one that already has the Mac counts."""
    eng.hold.clear()
    eng.submit("first")
    time.sleep(0.05)
    assert eng.submit("second")["position"] == 2


def test_they_run_in_the_order_they_were_handed_in(eng):
    eng.hold.clear()
    for n in ("first", "second", "third"):
        eng.submit(n)
    eng.hold.set()
    for _ in range(200):
        if len(eng.ran) == 3:
            break
        time.sleep(0.01)
    assert eng.ran == ["first", "second", "third"]


def test_only_one_runs_at_a_time(eng):
    """The part that was always true and must stay true."""
    inside = []

    def loop(task, progress):
        inside.append(task.goal)
        assert len(inside) == 1, "two tasks are driving the Mac at once"
        time.sleep(0.02)
        inside.pop()
        task.status = "done"
    eng._loop = loop            # type: ignore[assignment]
    for n in range(4):
        eng.submit(str(n))
    time.sleep(0.4)
    assert inside == []


def test_waiting_for_a_turn_is_visible(eng):
    eng.hold.clear()
    eng.submit("first")
    eng.submit("second")
    time.sleep(0.05)
    shown = eng.queue()
    assert [q["goal"] for q in shown] == ["first", "second"]
    assert shown[0].get("running") and shown[1]["position"] == 2


def test_something_still_waiting_can_be_taken_back(eng):
    eng.hold.clear()
    eng.submit("first")
    second = eng.submit("second")["task_id"]
    out = eng.cancel(second)
    eng.hold.set()
    time.sleep(0.3)
    assert out["was_queued"] and "second" not in eng.ran


def test_a_cancelled_one_does_not_run_even_if_the_worker_reached_it(eng):
    eng.hold.clear()
    first = eng.submit("first")["task_id"]
    eng._cancelled.add(first)
    eng.hold.set()
    time.sleep(0.3)
    assert eng.ran == [] and eng.tasks[first].status == "cancelled"


def test_do_waits_its_turn_like_everything_else(eng):
    """Otherwise a caller jumps the queue by choosing a different method."""
    eng.hold.clear()
    eng.submit("the one in front")
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(eng.do("the later one")))
    t.start()
    time.sleep(0.05)
    eng.hold.set()
    t.join(5)
    assert eng.ran == ["the one in front", "the later one"] and result.get("status") == "done"


def test_one_task_blowing_up_does_not_take_the_queue_with_it(eng):
    calls = []

    def loop(task, progress):
        calls.append(task.goal)
        if task.goal == "the bad one":
            raise RuntimeError("something the loop never expected")
        task.status = "done"
    eng._loop = loop            # type: ignore[assignment]
    eng.submit("the bad one")
    eng.submit("the good one")
    time.sleep(0.4)
    assert calls == ["the bad one", "the good one"] and eng.tasks[[t for t in eng.tasks][0]].status == "failed"


def test_giving_up_waiting_says_so_rather_than_hanging_forever(eng):
    eng.hold.clear()
    eng.submit("the one that never ends")
    out = eng._run_queued(eng._new_task("mine", None, None), None, timeout=0.2)
    assert out["status"] == "failed" and "waiting" in out["reason"]


def test_the_worker_is_not_left_behind_when_there_is_nothing_to_do(eng):
    """An engine used for one `observe` should not leave a thread running."""
    eng.submit("one thing")
    for _ in range(200):
        if eng._worker is None:
            break
        time.sleep(0.01)
    assert eng._worker is None


def test_a_handed_in_task_can_be_followed_by_its_id(eng):
    """`submit` said "follow it with status", and status took no id."""
    eng.hold.clear()
    first = eng.submit("first")["task_id"]
    second = eng.submit("second")["task_id"]
    time.sleep(0.05)
    waiting = eng.status(second)
    assert waiting["task_id"] == second and waiting["status"] == "queued" and waiting["position"] == 2
    eng.hold.set()
    for _ in range(200):
        if eng.status(first).get("status") == "done":
            break
        time.sleep(0.01)
    assert eng.status(first)["status"] == "done" and "position" not in eng.status(first)
    assert eng.status("nobody")["error"] == "unknown or expired task"


def test_a_task_that_crashes_the_engine_still_ends_properly(tmp_path):
    """The status alone was set: the task was not kept, not audited, and what it had opened stayed open."""
    import json

    class Broken(Engine):
        def _loop(self, task, progress):
            raise RuntimeError("a bug in a provider")

    conf = {"engine": {"store": str(tmp_path / "tasks.db"), "tidy_background": False}}
    eng = Broken(cfg(tmp_path, config=conf), helper=FakeHelper(), decider=ScriptedDecider([]))
    res = eng.do("anything")
    assert res["status"] == "failed" and res["cause"] == "engine_error" and "RuntimeError" in res["reason"]
    assert eng.store.get(res["task_id"]) is not None, "kept, like any other ending"
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r["kind"] == "task" and r["task"] == res["task_id"] and r["status"] == "failed" for r in log)
