"""A question to the planner is one call at a time, can be stopped, and says where its time went.

The loop waited on every question it put to the planner: on 09-23 the two key tasks, 7ffdee1b4ce3 and
4e69ff4e791e, spent 28.8 of 44.3 s and 27.3 of about 35 s waiting on a plan and a replan each. A question can now
run on a thread of its own, beside the loop, and be stopped. So it must be stopped when a newer question replaces it
or its run ends; a local server must never be asked twice at once (it works on both answers: one plan took 87 s that
takes 11 s on its own); and the ledger says how much of the planner's time the loop waited and how much ran beside
it, for the run it is the ledger of.
"""

import json
import threading
import time

import pytest

from macwork.engine import Engine
from macwork.planner import PlannerError
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

PLAN = {"steps": [{"goal": "open the settings", "evidence": "a window titled Settings"}], "inputs": {},
        "try": [{"keys": "cmd+,"}], "blocked": ""}


class Held:
    """A planner that answers when the test lets it (`release`), after `patience` seconds at the latest, or, when
    it can be stopped, once it is — at its next line, every `line_s` seconds, as a stream is. It counts how many
    questions it holds at once."""

    name, beside = "held", True

    def __init__(self, reply=None, local=True, stoppable=True, patience=5.0, line_s=0.01):
        self.reply, self.local, self.stoppable, self.patience, self.line_s = reply or PLAN, local, stoppable, patience, line_s
        self.release, self.asked = threading.Event(), threading.Event()
        self.prompts, self.stops = [], []
        self.live = self.most = 0
        self._count = threading.Lock()

    def complete(self, system, prompt, schema, stop=None):
        with self._count:
            self.live += 1
            self.most = max(self.most, self.live)
        self.prompts.append(prompt)
        self.stops.append(stop)
        self.asked.set()
        try:
            end = time.monotonic() + self.patience
            while time.monotonic() < end and not self.release.is_set():
                if stop is not None and stop.is_set():
                    raise PlannerError(f"{self.name}: stopped", kind="stopped")
                time.sleep(self.line_s)
            return dict(self.reply)
        finally:
            with self._count:
                self.live -= 1


def plan(task):
    """The question every test here asks: a first plan for the task's goal."""
    return lambda planning, stop: planning.plan(task.goal, {}, stop=stop)


def step_records(tmp_path):
    return [r for r in (json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines())
            if r.get("kind") == "step"]


def test_a_stream_is_stopped_between_two_lines_and_not_asked_again(monkeypatch, tmp_path):
    """Closing the connection is how mlx_lm is told to stop, and it stops at its next write. A stream is looked at
    between two lines, and a question stopped on purpose is not sent again as if its reply had been lost."""
    import urllib.request

    from macwork.planner import OpenAICompatPlanner
    stop, connection = threading.Event(), []

    class Reply:
        def __enter__(self):
            def lines():
                yield b": keepalive 2048/9000\n"          # the server is still reading the prompt…
                stop.set()                                 # …when the engine gives the question up
                yield b": keepalive 4096/9000\n"
                yield b'data: {"choices":[{"delta":{"content":"{}"}}]}\n'
            return lines()

        def __exit__(self, *a):
            connection.append("closed")
            return False

    class Opener:
        def open(self, req, timeout=None):
            connection.append("opened")
            return Reply()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    c = cfg(tmp_path)
    local = OpenAICompatPlanner(c, "local", {**c.get("planner.endpoints.local"), "stream": True})
    assert local.stoppable and not OpenAICompatPlanner(c, "local", {**c.get("planner.endpoints.local"), "stream": False}).stoppable
    with pytest.raises(PlannerError) as got:
        local.complete("system", "prompt", {"properties": {}}, stop=stop)
    assert got.value.stopped and got.value.kind == "stopped"
    assert connection == ["opened", "closed"], "a question stopped on purpose was sent again, or its connection left open"


def test_a_second_local_call_is_not_sent_while_one_runs(tmp_path):
    """A newer question stops the one running beside the loop, and is sent once that one has let go, which it does
    at its next line; one stopped while it waited its turn is never sent; the floor's words wait their turn the same
    way. A planner that is not on this Mac is not waited for: the doubling is a local server's, and an answer that
    cannot be stopped would only be sat out to be dropped."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = back = Held(line_s=0.2)
    task = eng._new_task("open the settings", {}, None)
    first = eng._planner_call(task, plan(task), beside=True)
    assert back.asked.wait(2)
    waiting = eng._planner_call(task, plan(task), beside=True)       # stops the first, and waits for it to let go
    second = eng._planner_call(task, plan(task))                     # stops that one before it was ever sent
    assert first.stopped and waiting.stopped and eng.scope(task.id).planning is None, \
        "the newer question did not replace the one beside the loop"
    assert first.wait(time.monotonic() + 2) and first.error is not None and first.error.stopped
    back.release.set()
    assert eng._planner_wait(task, second, until=time.monotonic() + 5)["steps"] == ["open the settings"]
    assert waiting.wait(time.monotonic() + 2) and waiting.error is not None and waiting.error.stopped
    assert back.most == 1, "two questions were at the local server at once"
    assert len(back.prompts) == 2, "a question stopped before its turn came was sent all the same"

    back.release.clear()
    back.asked.clear()
    third = eng._planner_call(task, plan(task), beside=True)
    assert back.asked.wait(2)
    words = threading.Thread(target=eng._derive_floor_words, args=("de", {"delete": "removes something for good"}))
    words.start()
    time.sleep(0.3)
    assert len(back.prompts) == 3, "the floor's words were asked while a question was at the local server"
    back.release.set()
    words.join(5)
    assert third.wait(time.monotonic() + 5) and len(back.prompts) == 4 and back.most == 1

    eng._planner = cloud = Held(local=False, stoppable=False)
    eng._planner_call(task, plan(task), beside=True)
    assert cloud.asked.wait(2)
    other = eng._planner_call(task, plan(task))
    deadline = time.monotonic() + 2
    while len(cloud.prompts) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cloud.most == 2, "a question to a cloud planner waited for one it can only drop"
    cloud.release.set()
    assert other.wait(time.monotonic() + 5)


def test_a_chain_is_beside_only_if_every_member_is_and_stop_reaches_the_ones_that_can_stop(tmp_path):
    from macwork.planner import AnthropicPlanner, Chain, FoundationPlanner, Planning

    class Refuses:                  # a planner from elsewhere, which knows nothing of stopping; its key is rejected
        name, local, beside = "front", True, True

        def __init__(self):
            self.asked = 0

        def complete(self, system, prompt, schema):
            self.asked += 1
            raise PlannerError("front: credentials rejected (401)", refused=True)

    class Answers:                  # one that can be stopped, behind it
        name, local, beside, stoppable = "behind", True, True, True

        def __init__(self):
            self.stops = []

        def complete(self, system, prompt, schema, stop=None):
            self.stops.append(stop)
            return dict(PLAN)

    class Plain:                    # one that does not say whether it may be asked beside the loop
        name, local = "plain", True

    c = cfg(tmp_path)
    assert Chain([Answers(), AnthropicPlanner(c)]).beside
    assert not Chain([Answers(), FoundationPlanner(c, FakeHelper())]).beside, "the on-device planner, behind another"
    assert not Chain([AnthropicPlanner(c), Plain()]).beside, "a planner that does not say it may run beside the loop"
    front, behind = Refuses(), Answers()
    stop = threading.Event()
    got = Planning(c, Chain([front, behind])).plan("open the settings", {}, stop=stop)
    assert got["steps"] == ["open the settings"] and front.asked == 1
    assert behind.stops == [stop], "the member that can be stopped was not handed the stop"


def test_a_waited_beside_call_is_counted_as_waited_only(tmp_path):
    """Seconds the loop sat waiting are waited seconds; they are not counted a second time as the planner's work
    beside the loop when the call is let go. A call never waited on is beside time, all of it."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = Held(patience=0.3)
    task = eng._new_task("open the settings", {}, None)
    eng._planner_wait(task, eng._planner_call(task, plan(task), beside=True))
    eng._stop_planning(task)
    waited = eng.ledger(task)["planner_seconds"]
    assert waited["waited"] >= 0.3 and waited["beside"] == 0.0, waited

    eng._planner_call(task, plan(task), beside=True)
    time.sleep(0.3)
    eng._stop_planning(task)
    beside = eng.ledger(task)["planner_seconds"]
    assert beside["beside"] >= 0.3 and beside["waited"] == waited["waited"], beside


def test_planner_seconds_are_per_run(tmp_path):
    """The ledger is the run's, like its seconds: a run resumed after the caller answered starts from nothing, and
    the task keeps the whole."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("open the settings", {}, None)
    task.planner_use.update({"waited_ms": 12000, "beside_ms": 3000})          # the run before this one
    task.begin_run(0, 0.0)
    assert eng.ledger(task)["planner_seconds"] == {"waited": 0.0, "beside": 0.0}
    task.planner_use["waited_ms"] += 1500
    assert eng.ledger(task)["planner_seconds"] == {"waited": 1.5, "beside": 0.0}
    assert task.planner_use["waited_ms"] == 13500


def test_an_ask_counted_on_its_own_thread_never_grows_what_is_being_read(tmp_path):
    """An ask made beside the loop is counted on its own thread, while the loop may be reading the task or writing
    it to disk, and a table that grows while it is read out raises. Every key is there before the first ask, and the
    table of planners is replaced whole, never grown in place."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = Held(patience=0)
    task = eng._new_task("open the settings", {}, None)
    planning = eng._planning(task)
    use = task.planner_use
    reading = [iter(use.items()), iter(use.get("by", {}).items())]      # the loop, halfway through writing it out
    planning.plan(task.goal, {})                                          # an ask, counted meanwhile
    for table in reading:
        list(table)
    assert use["calls"] == 1 and use["by"] == {"held": 1}


def test_the_budget_on_ending_includes_what_ran_beside(tmp_path):
    """The ledger an ending writes, and its step record, count the route that was still running beside the loop
    when the task ended, and the route is stopped."""
    back, calls = Held(), []

    def asks_beside(state, questions):          # a question put beside the loop, as a rethink does
        task = next(iter(eng.tasks.values()))
        calls.append(eng._planner_call(task, plan(task), beside=True))
        back.asked.wait(2)
        time.sleep(0.3)
        return {"pick": "done"}

    eng = Engine(cfg(tmp_path, config={"planner": {"second_opinion_on_done": False}}), helper=FakeHelper(),
                 decider=ScriptedDecider([asks_beside]))
    eng._planner = back
    res = eng.do("open the settings")
    assert res["status"] == "done"
    seconds = res["outputs"]["budget"]["planner_seconds"]
    assert seconds["beside"] >= 0.3 and seconds["waited"] == 0.0, seconds
    assert calls[0].stopped, "the route asked for beside the loop outlived the task"
    ending = step_records(tmp_path)[-1]
    assert ending["end"] == "done" and ending["planner_beside_ms"] >= 300 and ending["planner_waited_ms"] == 0, ending


def test_a_background_route_is_stopped_when_the_run_ends(tmp_path):
    """A route asked for beside the loop is for the run that asked it: when the run hands the task back
    (need_continue here), the question is stopped, not left running into whatever the caller does next."""
    calls = []

    def asks_beside(state, questions):
        task = next(iter(eng.tasks.values()))
        calls.append(eng._planner_call(task, plan(task), beside=True))
        return {"pick": "press the tab key"}

    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 1}}), helper=FakeHelper(), decider=ScriptedDecider([asks_beside]))
    eng._planner = Held()
    res = eng.do("open the settings")
    assert res["status"] == "need_continue", res
    assert calls[0].stopped and eng.scope(res["task_id"]).planning is None, "the route outlived the run that asked for it"
