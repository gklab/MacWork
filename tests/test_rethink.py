"""A question to the planner is one call at a time, can be stopped, and says where its time went; a rethink asks
it, and waits for the answer only on evidence.

The loop waited on every question it put to the planner: on 09-23 the two key tasks, 7ffdee1b4ce3 and
4e69ff4e791e, spent 28.8 of 44.3 s and 27.3 of about 35 s waiting on a plan and a replan each. A question can now
run on a thread of its own, beside the loop, and be stopped. So it must be stopped when a newer question replaces it
or its run ends; a local server must never be asked twice at once (it works on both answers: one plan took 87 s that
takes 11 s on its own); and the ledger says how much of the planner's time the loop waited and how much ran beside
it, for the run it is the ledger of.

A rethink vote stopped the task for a planner call, and when a route came the action chosen with it was dropped. On
09-22..23, 23 rethink looks chose the planner's own suggestion (7 waited for a call), and 96 chose an action on offer
where the step before had not failed and was not judged below progress_bad (60 calls, 575 s). And every rethink that
brought nothing counted toward the no_route ending alike: 16 of the 20 no_route endings came on a question refused as
asked already, with its allowance left. So the planner's own suggestion is taken unasked; after a step that got
somewhere (none of what the no-progress rule counts), on an action that needs no text from the planner, that the floor
lets through, on a screen not judged risky, the route is asked for beside the loop while the action goes through every
gate; otherwise it is waited for, never past the run's time; and a question refused as asked already on this exact
screen is no rethink that found nothing.
"""

import json
import threading
import time

import pytest

from macwork.engine import Engine
from macwork.model import Step
from macwork.planner import PlannerError
from tests.test_engine import WINDOW, FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend

PLAN = {"steps": [{"goal": "open the settings", "evidence": "a window titled Settings"}], "inputs": {},
        "try": [{"keys": "cmd+,"}], "blocked": ""}
TAB = {"pick": "press the tab key", "move": "rethink"}
KEYS = ["tab", "space", "up", "down", "left", "right", "home", "end"]


class Held:
    """A planner that answers when the test lets it (`release`), after `patience` seconds at the latest, or, when
    it can be stopped, once it is — at its next line, every `line_s` seconds, as a stream is. It counts how many
    questions it holds at once. Given a list of replies, it gives them in turn, and the last one again after that."""

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
            n = len(self.prompts)
            self.prompts.append(prompt)
        self.stops.append(stop)
        self.asked.set()
        try:
            end = time.monotonic() + self.patience
            while time.monotonic() < end and not self.release.is_set():
                if stop is not None and stop.is_set():
                    raise PlannerError(f"{self.name}: stopped", kind="stopped")
                time.sleep(self.line_s)
            return dict(self.reply[min(n, len(self.reply) - 1)] if isinstance(self.reply, list) else self.reply)
        finally:
            with self._count:
                self.live -= 1


def at_once(reply=None, **kw):
    """A planner beside the loop that answers as soon as it is asked."""
    back = Held(reply, **kw)
    back.release.set()
    return back


def landed(eng):
    """Wait for the question the engine's tasks have running beside the loop, if any, to land."""
    for scope in list(eng._scopes.values()):
        if scope.planning is not None:
            scope.planning.wait(time.monotonic() + 5)


class Lands(FakeHelper):
    """Carrying out an action takes as long as the route asked for beside the loop takes to land, so it is in by
    the next look — as on a Mac, where acting and looking again take longer than a quick answer. Its window's one
    line of text is a display that shows what `shows(n)` says after n keys (the text of FakeHelper's window when
    not given); its structure never changes."""

    engine = None

    def __init__(self, shows=None):
        super().__init__()
        self.shows = shows

    def call(self, method, timeout=30.0, **p):
        if method in ("input.key", "input.type", "ax.perform") and self.engine is not None:
            landed(self.engine)
        if method == "ax.snapshot" and p.get("scope") != "menubar" and self.shows is not None:
            self.calls.append((method, p))
            shown = self.shows(len(self.did("input.key")))
            return {"nodes": [n if n["ref"] != "g2.4" else {**n, "value": shown} for n in WINDOW["nodes"]], "ms": 4}
        return super().call(method, timeout, **p)


class Still(Lands):
    """Nothing any key does shows on screen: each key press breaks its promise, and got nowhere."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.wait":
            self.calls.append((method, p))
            return {"events": [], "timed_out": True}
        return super().call(method, timeout, **p)


class Writes(Held):
    """A planner that writes the text for a slot at once (`fills`), and answers any other question as Held does."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fills = []

    def complete(self, system, prompt, schema, stop=None):
        if "text" in schema.get("properties", {}):
            self.fills.append(prompt)
            return {"text": "zw@example.com"}
        return super().complete(system, prompt, schema, stop=stop)


def beside_engine(tmp_path, script, back, helper=None, **config):
    """An engine whose planner is `back`, whose actions take as long as its answers take to land, and which asks no
    second opinion on done (not what these tests are about)."""
    helper = helper or Lands()
    over = {"second_opinion_on_done": False, **config.pop("planner", {})}
    eng = Engine(cfg(tmp_path, config={"planner": over, **config}), helper=helper, decider=ScriptedDecider(script))
    eng._planner, helper.engine = back, eng
    return eng


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


def test_a_chain_a_local_server_may_answer_is_asked_one_call_at_a_time(tmp_path):
    """planner.auto_order puts the local server before deepseek, so a Mac with both has a chain of the two, and that
    chain is not local throughout (`Chain.local`, which decides redaction). Read as not local, it had a newer question
    sent to the local server while the one it replaced was still there, and the floor's words too."""
    from macwork.planner import Chain

    class Cloud:                    # behind it, never reached while the local server answers
        name, local, beside = "cloud", False, True

        def complete(self, system, prompt, schema):
            raise AssertionError("the planner behind the local server was asked")

    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    local = Held(line_s=0.2)
    eng._planner = Chain([local, Cloud()])
    task = eng._new_task("open the settings", {}, None)
    first = eng._planner_call(task, plan(task), beside=True)
    assert local.asked.wait(2)
    second = eng._planner_call(task, plan(task))                     # stops the first, and waits for it to let go
    deadline = time.monotonic() + 2
    while len(local.prompts) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    local.release.set()
    assert eng._planner_wait(task, second, until=time.monotonic() + 5)["steps"] == ["open the settings"]
    assert first.stopped and local.most == 1, "two questions were at the local server at once"

    local.release.clear()
    local.asked.clear()
    third = eng._planner_call(task, plan(task), beside=True)
    assert local.asked.wait(2)
    words = threading.Thread(target=eng._derive_floor_words, args=("de", {"delete": "removes something for good"}))
    words.start()
    time.sleep(0.3)
    assert len(local.prompts) == 3, "the floor's words were asked while a question was at the local server"
    local.release.set()
    words.join(5)
    assert third.wait(time.monotonic() + 5) and len(local.prompts) == 4 and local.most == 1


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


# ----------------------------------------------------------------- what a rethink does
def test_a_rethink_with_nothing_gone_wrong_takes_the_action_it_chose(tmp_path):
    """Nothing had gone wrong and the floor let the action through: it is taken, and the route asked for while it
    was carried out is there at the next look. It was dropped for a planner call the loop sat waiting on."""
    back = at_once()
    eng = beside_engine(tmp_path, [TAB, {"pick": "done"}], back)
    res = eng.do("open the settings")
    assert res["steps"][:1] == ["press the tab key"], "the action chosen with the rethink was dropped"
    assert len(back.prompts) == 1 and eng.decider.seen[1][0].get("plan") == ["open the settings"], \
        "the route asked for is not there at the next look"


def test_the_route_comes_while_the_chosen_action_is_carried_out(tmp_path):
    """The action goes first; the route lands while it is carried out and is taken in at the first look after it.
    Its seconds ran beside the loop, and none of them were waited."""
    back, seen = Held(patience=5.0), {}

    class Acts(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "input.key" and "acted" not in seen:
                seen["acted"] = not back.release.is_set()       # the planner cannot have answered yet
                back.asked.wait(2)
                time.sleep(0.2)
                back.release.set()
                landed(eng)
            return super().call(method, timeout, **p)

    def first(state, questions):
        seen["plan_first"] = state.get("plan")
        return TAB

    def then(state, questions):
        seen["plan_then"] = state.get("plan")
        return {"pick": "done"}

    eng = beside_engine(tmp_path, [first, then], back, helper=Acts())
    res = eng.do("open the settings")
    assert seen.get("acted"), "the loop waited for the planner before carrying out what the decider chose"
    assert seen["plan_first"] is None and seen["plan_then"] == ["open the settings"], seen
    spent = res["outputs"]["budget"]["planner_seconds"]
    assert spent["beside"] >= 0.2 and spent["waited"] == 0.0, spent


def test_the_planners_own_suggestion_is_tried_before_it_is_asked_again(tmp_path):
    """Both of 09-23's key tasks chose a move the planner had suggested — 「type 17」, 「type serendipity」 — with a
    rethink vote, waited on a replan, and the new route no longer held it. The planner's own suggestion is taken
    as it stands, a move it proposed or an action on screen it named, and the planner is not asked about it."""
    typing = {**PLAN, "try": [{"type": "17"}]}
    elsewhere = {**PLAN, "steps": [{"goal": "start over", "evidence": ""}]}
    d = ScriptedDecider([{"pick": "press the tab key"}, {"pick": "type 「17」", "move": "rethink", "progress": 0.3}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([dict(typing), dict(elsewhere)])
    res = eng.do("type 17")
    assert len(eng._planner.prompts) == 1, "asked again about the route it had just given"
    assert eng.helper.did("input.type")[0]["text"] == "17" and res["outputs"]["budget"]["fruitless"]["used"] == 0

    naming = {**PLAN, "try": [{"action": "menu File ▸ New Document (⌘N)"}]}
    d = ScriptedDecider([{"pick": "press the tab key"}, {"pick": "New Document", "move": "rethink"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([dict(naming), dict(elsewhere)])
    res = eng.do("make a new document")
    assert len(eng._planner.prompts) == 1 and res["steps"][-1] == "menu File ▸ New Document (⌘N)", res["steps"]


def test_a_step_that_got_nowhere_is_waited_on_but_never_past_the_runs_time(tmp_path):
    """After a step judged to have got nowhere the route is waited for, and the action chosen with the rethink
    waits with it — but never past the run's time: the question is stopped then, and the run ends on its budget."""
    back = Held(patience=10.0)
    script = [{"pick": "press the tab key"}, {"pick": "press the space key", "move": "rethink", "progress": 0.1}] + [{"pick": "done"}] * 3
    eng = beside_engine(tmp_path, script, back, engine={"budget_s": 3.0})
    eng.decider.what = {"navigate": 1.0}      # the floor is not what this is about (see the late questions below)
    t0 = time.monotonic()
    res = eng.do("open the settings")
    assert time.monotonic() - t0 < 5.5, "the loop waited out the planner past the run's time"
    assert res.get("cause") == "budget", (res["status"], res.get("cause"), res.get("reason"))
    assert back.stops and back.stops[0] is not None and back.stops[0].is_set(), "the question was not stopped"
    assert "press the space key" not in res["steps"], "on evidence the chosen action waits for the route"


def test_an_already_answered_screen_is_not_a_rethink_that_found_nothing(tmp_path):
    """Asked again about the exact screen it has answered, at the same point of the plan and for the same reason,
    the planner is not asked, and the answer it gave stands: that is no rethink that found nothing. Counted as one,
    it ended the task no_route after four steps."""
    d = ScriptedDecider([{"pick": f"press the {k} key", "move": "rethink"} for k in KEYS] + [{"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"planner": {"second_opinion_on_done": False}}), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([dict(PLAN)] * 3)
    res = eng.do("open the settings")
    assert res["status"] == "done", (res["status"], res.get("cause"), res.get("reason"))
    assert res["steps"] == [f"press the {k} key" for k in KEYS[1:]] and len(eng._planner.prompts) == 2


def test_a_replan_on_a_changed_display_is_asked_again(tmp_path):
    """Keyed on the window's structure, aff49b2812ff's rethink on a calculator showing 「1」 was refused as asked
    already — a plan had been made while it showed 「12+30×4」 — and the task ended no_route there. What was asked
    about is the screen as it shows."""
    d = ScriptedDecider([{"pick": "press the tab key"}, {"pick": "press the space key", "move": "rethink"},
                         {"pick": "press the up key"}, {"pick": "press the down key", "move": "rethink"}, {"pick": "done"}])
    helper = Lands(shows=lambda n: "132" if n >= 2 else "42")
    eng = Engine(cfg(tmp_path, config={"planner": {"second_opinion_on_done": False}}), helper=helper, decider=d)
    eng._planner = FakeBackend([dict(PLAN), {**PLAN, "steps": [{"goal": "clear the display", "evidence": "0"}]}])
    eng.do("work out (12+30)×4")
    assert len(eng._planner.prompts) == 2, "the display changed, and the planner was not asked about it"
    assert "132" in eng._planner.prompts[1]


def test_a_planner_that_cannot_be_reached_ends_with_its_own_cause(tmp_path):
    """Asked beside the loop or waited for, a planner that never answers brings no route: the task ends as it did,
    on rethinks that brought nothing, and the ending names the outage (`planner_unreachable`, from `_finish`). A
    question it never answered is asked again, not refused as answered."""
    class Down(Held):
        def complete(self, system, prompt, schema, stop=None):
            self.prompts.append(prompt)
            raise PlannerError("local: <urlopen error [Errno 61] Connection refused>", kind="unreachable")

    back = Down()
    eng = beside_engine(tmp_path, [{"pick": f"press the {k} key", "move": "rethink"} for k in KEYS], back)
    res = eng.do("open the settings")
    assert res["status"] == "failed" and res["cause"] == "planner_unreachable", (res["status"], res.get("cause"), res.get("reason"))
    assert res["steps"] == [f"press the {k} key" for k in KEYS[:4]] and len(back.prompts) == 5, (res["steps"], len(back.prompts))


def test_a_consult_says_why_it_brought_nothing(tmp_path):
    from macwork.consult import Route

    eng = Engine(cfg(tmp_path, config={"planner": {"max_replans": 1}}), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("open the settings", {}, None)
    assert eng._consult(task, None, None, "?") is Route.NONE, "no planner"
    eng._planner = FakeBackend([dict(PLAN), dict(PLAN), {"steps": [], "inputs": {}, "try": []}])
    first = eng._consult(task, None, None, "?")
    assert first is Route.NEW and first and task.plan == ["open the settings"]
    assert eng._consult(task, None, None, "?") is Route.ASKED, "the same question about the same screen"
    task.steps.append(Step(0, "press the tab key", ok=True, events=["x"], decision={"progress_after": 0.9}))
    same = eng._consult(task, None, None, "?")
    assert same is Route.SAME and not same
    task.steps.append(Step(1, "press the space key", ok=True, events=["x"], decision={"progress_after": 0.9}))
    assert eng._consult(task, None, None, "?", kind="stuck") is Route.SPENT, "the allowance: the first plan and one more"
    task.steps.append(Step(2, "press the up key", ok=False, error="gone"))        # went wrong: its own allowance
    assert eng._consult(task, None, None, "?") is Route.EMPTY and task.tries == PLAN["try"], \
        "an answer with nothing in it, and the last one stands"
    eng._planner = FakeBackend([])
    assert eng._consult(task, None, None, "?", kind="stuck") is Route.ERROR and task.outputs["planner_errors"], \
        "no answer to be had"


def test_a_suggestion_is_known_by_what_it_is_not_by_its_id(tmp_path):
    """The window's "read all the text" option is t<n>, and so was a planner suggestion: under one id the options
    held only one of the two, and taking it removed the suggestion from the planner's tries."""
    d = ScriptedDecider([{"pick": "read all the text"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["readall", "keys"], "readall": {"offer_over": 0}},
                                       "planner": {"when": "always", "second_opinion_on_done": False}}),
                 helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{**PLAN, "try": [{"keys": "cmd+n"}]}])
    eng.do("read what the window says")
    first = d.seen[0][1]["action"]["criteria"].values()
    assert any(v.startswith("read all the text") for v in first) and any(v.startswith("press cmd+n (suggested") for v in first), \
        "the window's read-all option and the planner's suggestion shared an id, and only one of them was offered"
    assert any(v.startswith("press cmd+n (suggested") for v in d.seen[1][1]["action"]["criteria"].values()), \
        "reading the window removed the planner's suggestion"


def test_a_doubted_action_the_floor_stops_for_waits_for_the_route(tmp_path):
    """Taken beside the route, an action the floor stops for — or any action on a screen judged risky — would stop
    the task at the confirmation, on an action the decider itself doubted; waited for, the route gives it a way
    round. The same verdict `_pick` reads next."""
    plan = {**PLAN, "try": [{"action": "menu File ▸ New Document (⌘N)"}]}
    eng = beside_engine(tmp_path, [{"pick": "Delete Document", "move": "rethink"}, {"pick": "New Document"}, {"pick": "done"}],
                        at_once(plan))
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"], (res["status"], res["steps"])

    eng = beside_engine(tmp_path, [{**TAB, "risky_screen": 0.7}, {"pick": "done"}], at_once())
    res = eng.do("open the settings")
    assert res["status"] == "done" and res["steps"] == [], (res["status"], res["steps"])


def test_with_beside_off_a_rethink_waits_and_decides_again_with_the_route(tmp_path):
    """`planner.beside` is the switch. On, the action chosen with a rethink is taken while the route is asked for;
    off, the rethink waits for its route and decides again with it, as every rethink did — and the seconds it
    waited are counted as waited, not a second time as run beside the loop."""
    for beside in (True, False):
        seen = {}

        def then(state, questions):
            seen["plan"] = state.get("plan")
            return {"pick": "done"}

        back = Held(patience=0.3)          # answers after 0.3 s: nothing lets it go sooner
        eng = beside_engine(tmp_path, [TAB, then], back, planner={"beside": beside})
        res = eng.do("open the settings")
        assert res["steps"] == (["press the tab key"] if beside else []) and seen["plan"] == ["open the settings"], \
            (beside, res["steps"], seen)
        assert len(back.prompts) == 1
    spent = res["outputs"]["budget"]["planner_seconds"]
    assert spent["waited"] >= 0.3 and spent["beside"] == 0.0, spent


@pytest.mark.parametrize("landing_look", ["ends the task", "waits", "lands during a rethink"])
def test_a_route_that_just_landed_is_tried_before_the_planner_is_asked_again(tmp_path, landing_look):
    """A route asked for beside the loop landed on the look where three steps had got nowhere, and was replaced by
    the answer to a question about those steps, asked and waited for before the decider ever saw it. Nor is it
    replaced on the look after a wait there, with no step taken in between: the look it was taken in at judged the
    step before it, which made the stretch one step longer, and marked by its length that was a new stretch to ask
    about. One that lands while a rethink is decided is decided with at once, and marks the stretch the same way."""
    back = Held([PLAN, {**PLAN, "steps": [{"goal": "something else", "evidence": ""}]}], patience=5.0)
    seen = []

    def lands(state, questions):
        back.release.set()
        landed(eng)
        return {"pick": "press the down key", "progress": 0.1, "move": "rethink" if landing_look == "lands during a rethink" else "act"}

    def waits(state, questions):
        seen.append(state.get("plan"))
        return {"pick": "press the left key", "move": "wait", "progress": 0.1}

    def then(state, questions):
        seen.append(state.get("plan"))
        return {"pick": "done", "progress": 0.1}

    script = [TAB, {"pick": "press the space key", "progress": 0.1}, {"pick": "press the up key", "progress": 0.1}, lands] \
        + ([waits] if landing_look == "waits" else []) + [then]
    eng = beside_engine(tmp_path, script, back, helper=FakeHelper(), engine={"wait_s": 0})
    eng.do("open the settings")
    assert len(back.prompts) == 1 and seen == [["open the settings"]] * (2 if landing_look == "waits" else 1), \
        (len(back.prompts), seen)


@pytest.mark.parametrize("move", ["rethink", "blocked", "impossible", "ask_user"])
def test_a_route_that_lands_while_the_decider_decides_is_decided_with(tmp_path, move):
    """The route asked for beside the loop landed while the decider was answering, and it voted rethink again — or
    blocked, impossible, or ask the user with one candidate: the decision is made again with that route, not a new
    question sent, which would have stopped the call the route came in on and lost it unread."""
    back = Held([PLAN, {**PLAN, "steps": [{"goal": "something else", "evidence": ""}]}], patience=5.0)
    seen = {}

    def lands(state, questions):
        back.release.set()
        landed(eng)
        return {"pick": "press the space key", "move": move}

    def then(state, questions):
        seen["plan"] = state.get("plan")
        return {"pick": "done"}

    eng = beside_engine(tmp_path, [TAB, lands, then], back, helper=FakeHelper())
    res = eng.do("open the settings")
    assert res["steps"] == ["press the tab key"] and len(back.prompts) == 1 and seen["plan"] == ["open the settings"], \
        (res["steps"], len(back.prompts), seen)


def test_a_route_that_lands_while_nothing_is_left_to_act_on_is_decided_with(tmp_path):
    """Nothing is left to act on, and the planner is asked for a route before the task says so: a route that
    landed beside the loop while the decider decided is that route. Asked again, the call it came in on was
    stopped, and it was lost unread."""
    back = Held([PLAN, {**PLAN, "steps": [{"goal": "something else", "evidence": ""}]}], patience=5.0)
    seen = {}

    def lands(state, questions):         # the key did nothing, so it is not offered here again: nothing is left
        back.release.set()
        landed(eng)
        return {"pick": "none"}

    def then(state, questions):
        seen["plan"] = state.get("plan")
        return {"pick": "done"}

    helper = Still()
    eng = beside_engine(tmp_path, [TAB, lands, then], back, helper=helper, observe={"providers": ["keys"], "keys": ["tab"]})
    helper.engine = None                 # the route lands while the decider decides, not while the key is pressed
    res = eng.do("open the settings")
    assert res["steps"] == ["press the tab key"] and len(back.prompts) == 1 and seen["plan"] == ["open the settings"], \
        (res["steps"], len(back.prompts), seen)


def test_a_route_that_landed_is_not_lost_to_a_question_for_text(tmp_path):
    """Text for a slot, the answer and the opinion on "done" are no routes: asked while a route that has landed is
    waiting for the next look, they leave it there. Asked for the text of the action chosen meanwhile, the planner's
    question stopped the call the route came in on, and it was dropped unread."""
    back, seen = Writes(patience=5.0), {}

    def lands(state, questions):
        back.release.set()
        landed(eng)
        return {"pick": "Recipient"}

    def then(state, questions):
        seen["plan"] = state.get("plan")
        return {"pick": "done"}

    eng = beside_engine(tmp_path, [TAB, lands, then], back, helper=FakeHelper())
    res = eng.do("write to zw@example.com")
    assert len(back.fills) == 1 and res["steps"] == ["press the tab key", "type into text field 「Recipient」"], res["steps"]
    assert seen["plan"] == ["open the settings"], "the route that had landed was dropped by the question for the text"


def test_a_rethink_on_an_action_whose_text_the_planner_writes_waits_for_its_route(tmp_path):
    """Taken beside its route, an action whose text the planner is asked to write asks for that text in the same
    step, and that question stopped the route before it came; on a local server the text then waited for the
    stopped call to let go. Such a rethink waits for its route, and decides again with it."""
    back, seen = Writes(patience=0.5), {}          # the route takes half a second; the text would come at once

    def then(state, questions):
        seen["plan"] = state.get("plan")
        return {"pick": "done"}

    eng = beside_engine(tmp_path, [{"pick": "Recipient", "move": "rethink"}, then], back, helper=FakeHelper())
    res = eng.do("write to zw@example.com")
    assert not back.stops[0].is_set(), "the route was stopped by the question for the text"
    assert res["steps"] == [] and not back.fills and seen["plan"] == ["open the settings"], (res["steps"], seen)


def test_a_decider_that_answers_through_a_local_planner_has_its_rethinks_wait(tmp_path):
    """decider.kind: local (or auto, once it has fallen back to it) answers every decision through a backend built
    from planner.endpoints, the same local server, and takes no turn at it: a route asked beside the loop would be
    worked on there beside the loop's own decisions. Its rethinks wait for their route, and decide again with it."""
    from macwork.localdecider import LocalDecider

    class Local(LocalDecider):
        """The local decider, its answers read from a script: where it would send them is what matters here."""
        calibrated = True                # so the floor asks it what it asks any decider in these tests

        def __init__(self, c, backend, script):
            super().__init__(c, backend=backend)
            self.scripted = ScriptedDecider(script)

        def decide(self, state, questions):
            self.calls += 1
            return self.scripted.decide(state, questions)

    back = at_once()
    c = cfg(tmp_path, config={"planner": {"second_opinion_on_done": False}})
    local = Local(c, back, [TAB, {"pick": "done"}])
    helper = Lands()
    eng = Engine(c, helper=helper, decider=local)
    eng._planner, helper.engine = back, eng
    res = eng.do("open the settings")
    assert res["steps"] == [] and local.scripted.seen[1][0].get("plan") == ["open the settings"], res["steps"]
    assert len(back.prompts) == 1


def test_a_first_plan_that_failed_is_not_asked_again_before_the_first_step(tmp_path):
    """planner.when: always asks for the first plan after the first look, once a run, whatever it came to. Remembered
    as asked only once answered, one that failed was asked again on every look before the first step — four times
    across three waits — and each can cost the planner's whole timeout_s (45 s local, 60 s deepseek)."""
    class TimesOut(Held):
        def complete(self, system, prompt, schema, stop=None):
            self.prompts.append(prompt)
            raise PlannerError("local: no reply within 45 s", kind="timeout")

    back = TimesOut()
    script = [{"pick": "press the tab key", "move": "wait"}] * 3 + [{"pick": "press the tab key"}, {"pick": "done"}]
    eng = beside_engine(tmp_path, script, back, planner={"when": "always"}, engine={"wait_s": 0})
    res = eng.do("open the settings")
    assert res["steps"] == ["press the tab key"] and len(back.prompts) == 1, (res["steps"], len(back.prompts))
    assert len(res["outputs"]["planner_errors"]) == 1


def test_a_rethink_that_decides_whether_the_task_ends_waits_for_its_answer(tmp_path):
    """With no fruitless rethink left, what this rethink brings decides whether the task ends here: it is waited for,
    as every rethink was. Asked beside the loop, it would be ended on before its answer could come."""
    same, other = dict(PLAN), {**PLAN, "steps": [{"goal": "open the preferences", "evidence": "a window titled Preferences"}]}
    back = Held([same, same, same, other], patience=5.0)
    back.release.set()
    script = [{"pick": f"press the {k} key"} for k in KEYS[:3]] + [{"pick": f"press the {k} key", "move": "rethink"} for k in KEYS[3:7]] \
        + [{"pick": "done"}]
    eng = beside_engine(tmp_path, script, back, helper=Lands(shows=str), planner={"max_replans": 5})
    res = eng.do("open the settings")
    assert res["status"] == "done", (res["status"], res.get("cause"), res.get("reason"))
    assert len(back.prompts) == 4 and res["plan"]["steps"] == ["open the preferences"], (len(back.prompts), res["plan"])


def test_the_on_device_planner_never_runs_beside(tmp_path):
    """It answers through the helper's main connection, held for the whole generation: asked beside the loop, every
    look and every action would queue behind it. A rethink with nothing gone wrong waits for it, and decides again
    with its route."""
    from macwork.planner import FoundationPlanner

    class OnDevice(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "llm.generate":
                self.calls.append((method, p))
                return {"text": json.dumps(PLAN)}
            return super().call(method, timeout, **p)

    helper = OnDevice()
    eng = beside_engine(tmp_path, [TAB, {"pick": "done"}], None, helper=helper)
    eng._planner = FoundationPlanner(eng.cfg, helper)
    assert eng._planner.beside is False
    res = eng.do("open the settings")
    assert res["steps"] == [] and eng.decider.seen[1][0].get("plan") == ["open the settings"], res["steps"]
    assert len(helper.did("llm.generate")) == 1 and not helper.did("input.key")


LATE = {  # a waited question, and what the task had done when it was asked
    "first plan": ({"planner": {"when": "always"}}, Lands, [{"pick": "press the tab key"}], 0),
    "blocked": ({}, Lands, [{"pick": "press the tab key", "move": "blocked"}], 0),
    "ask the user": ({}, Lands, [{"pick": "press the tab key", "move": "ask_user"}], 0),
    "nothing left": ({"observe": {"providers": ["keys"], "keys": []}}, Lands, [{"pick": "none"}], 0),
    # three whole steps before the question: a run of 1.5 s asked it 1.4-1.5 s in, and one in a full suite ran out
    # before it was sent. Every other one here is asked on the first look.
    "no progress": ({"engine": {"budget_s": 3.0}}, Still, [{"pick": f"press the {k} key"} for k in KEYS[:4]], 3),
}


@pytest.mark.parametrize("asked", list(LATE))
def test_a_late_first_plan_or_blocked_second_opinion_decides_again(tmp_path, asked):
    """A route waited for that has not come when the run's time is up — a first plan (planner.when: always), a
    second opinion on "blocked", the one-candidate "ask the user", nothing left to act on, a stretch that got
    nowhere — is given up on, and the loop's own budget check says what happens next. Each was waited out as long
    as the planner took, and then the task went on to act, or ended on its own terms, with its time gone."""
    config, helper, script, steps = LATE[asked]
    engine = {"budget_s": 1.5, **config.get("engine", {})}
    back = Held(patience=10.0)
    eng = beside_engine(tmp_path, script + [{"pick": "done"}] * 2, back, helper=helper(), **{**config, "engine": engine})
    # The floor is not what this is about, and the fake classifies each action by reading the policy from disk: that
    # was about 1.1 of the 1.4 s before the stretch of "no progress" was asked about. Here it says navigation of every
    # action, as it did of the keys, the only actions these tasks take.
    eng.decider.what = {"navigate": 1.0}
    t0 = time.monotonic()
    res = eng.do("open the settings")
    assert time.monotonic() - t0 < engine["budget_s"] + 2.5, "waited for the planner past the run's time"
    assert res.get("cause") == "budget" and len(res["steps"]) == steps, (res["status"], res.get("cause"), res.get("reason"), res["steps"])
    assert back.stops and back.stops[0].is_set(), "the question was not stopped"


def test_a_task_that_reaches_its_goal_after_a_stuck_stretch_ends_done(tmp_path):
    """Three steps judged to have got nowhere, then one that reaches the goal, and the decider says done on the look
    after it: the task ends done. An ending decided on the stretch before the decider had judged the newest step
    would have ended it no_route."""
    d = ScriptedDecider([{"pick": "press the tab key"}, {"pick": "press the space key", "progress": 0.1},
                         {"pick": "press the up key", "progress": 0.1}, {"pick": "press the down key", "progress": 0.1},
                         {"pick": "done", "progress": 0.9}])
    res = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d).do("open the settings")     # no planner configured
    assert res["status"] == "done", (res["status"], res.get("cause"), res.get("reason"))


# ----------------------------------------------------------------- taking a route in
def test_a_route_that_lands_steps_later_leaves_out_what_was_done_meanwhile(tmp_path):
    """Asked beside the loop, a route lands a step or more after it was asked: a move in it that the task took
    meanwhile is not offered again."""
    reply = {**PLAN, "try": [{"action": "menu File ▸ New Document (⌘N)"}, {"keys": "cmd+,"}]}
    back, seen = Held(reply, patience=5.0), {}

    class Acts(FakeHelper):              # the route lands while New Document is carried out
        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform":
                back.release.set()
                landed(eng)
            return super().call(method, timeout, **p)

    def then(state, questions):
        seen["suggests"] = state.get("planner_suggests")
        return {"pick": "done"}

    eng = beside_engine(tmp_path, [TAB, {"pick": "New Document"}, then], back, helper=Acts())
    res = eng.do("make a new document")
    assert res["steps"] == ["press the tab key", "menu File ▸ New Document (⌘N)"] and seen["suggests"] == ["press cmd+,"], \
        (res["steps"], seen)


def test_a_blocked_that_lands_beside_the_loop_is_an_opinion(tmp_path):
    """The planner's word that only the user can go on ends nothing by itself when it comes beside the loop either:
    the decider is shown it as an opinion, and the task goes on."""
    back, seen = at_once({"steps": [], "inputs": {}, "try": [], "blocked": "the user must sign in first"}), {}

    def then(state, questions):
        seen["thinks"] = state.get("planner_thinks")
        return {"pick": "done"}

    res = beside_engine(tmp_path, [TAB, then], back).do("open the settings")
    assert res["status"] == "done" and "sign in" in (seen["thinks"] or ""), (res["status"], seen)


def test_a_stretch_that_got_nowhere_is_asked_about_once(tmp_path):
    """The no-progress rule asks the planner once per stretch: seen again with no step taken since — after a sub-goal
    is ticked off, a wait, or a look into a group — it is the question already asked. And a rethink on the look where
    it asked adds no second question: that answer stands."""
    two = {**PLAN, "steps": [{"goal": "open the settings", "evidence": "a window titled Settings"},
                             {"goal": "turn it on", "evidence": "the switch is on"}]}
    ticked = {"pick": "press the down key", "step_done": 0.9, "step_evidence": 0.9}
    eng = beside_engine(tmp_path, [{"pick": f"press the {k} key"} for k in KEYS[:3]] + [ticked, {"pick": "done"}], at_once([two]),
                        helper=Still())
    res = eng.do("open the settings")
    assert len(eng._planner.prompts) == 1 and res["status"] == "done", (len(eng._planner.prompts), res["status"])

    # Steps judged to have got nowhere count once the look after them has judged them, and the look that asks judges
    # the newest step as well: the look after it, with no step in between, finds the same stretch one step longer.
    script = [{"pick": "press the tab key"}] + [{"pick": f"press the {k} key", "progress": 0.1} for k in KEYS[1:4]] \
        + [{**ticked, "progress": 0.1}, {"pick": "done"}]
    eng = beside_engine(tmp_path, script, at_once([two]), helper=FakeHelper())
    res = eng.do("open the settings")
    assert len(eng._planner.prompts) == 1 and res["status"] == "done", (len(eng._planner.prompts), res["status"])

    script = [{"pick": "press the tab key"}] + [{"pick": f"press the {k} key", "progress": 0.1} for k in KEYS[1:4]] \
        + [{"pick": "press the left key", "move": "rethink", "progress": 0.9}, {"pick": "done"}]
    eng = beside_engine(tmp_path, script, at_once())
    res = eng.do("open the settings")
    assert len(eng._planner.prompts) == 1 and res["steps"][-1] == "press the left key", (len(eng._planner.prompts), res["steps"])


def test_a_stretch_found_on_a_look_nobody_could_read_is_asked_about_once_the_app_answers(tmp_path):
    """Not asked while the app could not be read and can still be waited for, the stretch is still to be asked
    about: marked as asked, it would never be."""
    from tests.test_readiness import MAIN, Starting

    helper = Starting(screen=[MAIN])                 # it does not answer
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["apps", "menu", "window", "windows", "keys"]},
                                       "engine": {"open_front_s": 0.05, "ready_poll_ms": 1}}),
                 helper=helper, decider=ScriptedDecider([]))
    eng._planner = back = FakeBackend([dict(PLAN)])
    task = eng._new_task("open the settings", {}, None)
    first = eng._look(task, eng._step_context(task))
    for n in range(3):
        task.steps.append(Step(n=n, action="press the tab key", ok=False, error="gone", before=f"{first.sig}:0"))
    eng._look(task, eng._step_context(task))
    assert not back.prompts, "the planner was asked about a look nobody could read"
    helper.answer_on = 0                             # now it answers
    eng._look(task, eng._step_context(task))
    assert len(back.prompts) == 1, "the stretch was never asked about"


def test_a_rethink_while_its_route_is_still_on_the_way_waits_for_a_route(tmp_path):
    """A second rethink with nothing gone wrong, while the route the first asked for is still on its way, is not
    asked beside the loop again: it waits for a route, and decides again with it."""
    class Slow(Held):                    # the first question is held until it is stopped; the next is answered at once
        def complete(self, system, prompt, schema, stop=None):
            if self.prompts:
                self.release.set()
            return super().complete(system, prompt, schema, stop=stop)

    back = Slow(patience=5.0)
    eng = beside_engine(tmp_path, [TAB, {"pick": "press the space key", "move": "rethink"}, {"pick": "done"}], back,
                        helper=FakeHelper())
    res = eng.do("open the settings")
    assert res["steps"] == ["press the tab key"] and len(back.prompts) == 2, (res["steps"], len(back.prompts))
    assert back.stops[0].is_set(), "the question it replaced was left running"


def test_a_step_that_broke_its_promise_is_what_a_rethink_waits_on(tmp_path):
    """What the no-progress rule counts is what a rethink waits for its route on: a step that failed, broke its
    promise, only led back, or was judged below progress_bad. Here the decider judged the step fine, and nothing it
    promised showed: the action chosen with the rethink waits for the route."""
    back = at_once()
    eng = beside_engine(tmp_path, [{"pick": "press the tab key"}, {"pick": "press the space key", "move": "rethink"}, {"pick": "done"}],
                        back, helper=Still())
    res = eng.do("open the settings")
    assert res["steps"] == ["press the tab key"] and len(back.prompts) == 1, (res["steps"], len(back.prompts))


def test_a_new_route_puts_the_rethinks_that_found_nothing_behind_the_task(tmp_path):
    """Two rethinks that bring nothing new end the task once it has taken four steps — two in a row, not two in
    all: a new route between them starts the count again, as it always did."""
    other = {**PLAN, "steps": [{"goal": "open the preferences", "evidence": "a window titled Preferences"}]}
    keys = KEYS + ["pageup", "pagedown"]
    script = [{"pick": f"press the {k} key"} for k in keys[:4]] + [{"pick": f"press the {k} key", "move": "rethink"} for k in keys[4:]] \
        + [{"pick": "done"}]
    eng = beside_engine(tmp_path, script, None, helper=Lands(shows=str), planner={"max_replans": 5})
    eng._planner = FakeBackend([dict(PLAN), dict(PLAN), dict(other), dict(other)])
    res = eng.do("open the settings")
    assert res["status"] == "done", (res["status"], res.get("cause"), res.get("reason"))
    assert len(eng._planner.prompts) == 4 and res["outputs"]["budget"]["fruitless"]["used"] == 1


def test_a_route_given_up_on_is_not_taken_in(tmp_path):
    """A question stopped before its answer came is no answer: nothing of it is taken in, counted fruitless, or
    recorded as the planner's error."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng._planner = back = Held(patience=5.0)
    task = eng._new_task("open the settings", {}, None)
    assert eng._consult_beside(task, None, None, "?") == "pending"
    call = eng.scope(task.id).planning
    assert back.asked.wait(2)
    call.cancel()
    assert call.wait(time.monotonic() + 2) and call.error is not None and call.error.stopped
    assert eng._merge_beside(task, None) is None and eng.scope(task.id).planning is None
    assert task.plan is None and task.pace.fruitless == 0 and "planner_errors" not in task.outputs


def test_the_words_an_ending_is_owed_are_waited_for_past_the_runs_time(tmp_path):
    """A task that ends blocked on its budget still has the planner say what only the user can do: that question
    is owed to the caller however late it is, and is not bounded by the run's time as a route is."""
    def slow(state, questions):
        time.sleep(0.6)
        return {"pick": "press the tab key"}

    d = ScriptedDecider([slow])
    d.why = "blocked"
    eng = Engine(cfg(tmp_path, config={"engine": {"budget_s": 0.5, "checkpoint": False}}), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "try": [], "blocked": "the user must sign in first"}])
    res = eng.do("open the settings")
    assert res["status"] == "blocked" and "sign in" in res["reason"], (res["status"], res["reason"])


def test_nothing_left_on_a_look_the_planner_was_asked_at_asks_it_nothing_more(tmp_path):
    """The planner is asked once a look. Asked about a stretch that got nowhere, and its answer leaving nothing on
    offer, the look does not ask it again because nothing is left: that answer stands, and the task says so."""
    first = {**PLAN, "try": [{"keys": "cmd+1"}, {"keys": "cmd+2"}, {"keys": "cmd+3"}]}
    over = {**PLAN, "steps": [{"goal": "start over", "evidence": ""}], "try": []}
    script = [{"pick": f"cmd+{n}"} for n in (1, 2, 3)] + [{"pick": "none"}]
    eng = beside_engine(tmp_path, script, None, helper=Still(), planner={"when": "always"},
                        observe={"providers": ["keys"], "keys": []})
    eng._planner = FakeBackend([dict(first), dict(over), dict(over)])
    res = eng.do("open the settings")
    assert len(eng._planner.prompts) == 2 and res["cause"] == "no_actions", (len(eng._planner.prompts), res["status"], res.get("cause"))
