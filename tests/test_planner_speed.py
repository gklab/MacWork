"""What a planner call costs on a local model, and what it should not cost.

Measured on this Mac (Qwen 27B, 8-bit, mlx_lm.server): about 3.4 ms a token of prompt and 70 ms a token of
reply. In an eval run the planner was 79% of task time. Part of that was the model; part of it was the client:
a plan that ran past the 45 s timeout was asked again, and the server — never told the first was given up on —
went on writing it beside the second. One plan took 87 s that takes 11 s on its own. And nothing a local
server could have kept of a prompt was ever found again, because every prompt began with the goal and the
screen: 238 tokens cached of 1400, on every call.
"""

import json
import urllib.error

import pytest

from macwork import planner
from macwork.config import Config
from macwork.engine import Engine
from macwork.observe import observe
from macwork.planner import OpenAICompatPlanner, Planning, PlannerError, _whole_object
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


def sse(text: str) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": {"content": text}}]}).encode() + b"\n"


class Reply:
    """A streamed response, counting how much of it was read."""

    def __init__(self, lines, fail=None):
        self.lines, self.read, self.fail = list(lines), 0, fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for line in self.lines:
            if self.fail is not None and self.read == self.fail[0]:
                raise self.fail[1]
            self.read += 1
            yield line


class Server:
    """Stands in for `build_opener(...)`: hands out the replies in turn and keeps what was sent."""

    def __init__(self, *replies, refuse=None):
        self.replies, self.sent, self.refuse = list(replies), [], refuse

    def __call__(self, *handlers):
        return self

    def open(self, req, timeout=None):
        self.sent.append(json.loads(req.data))
        if self.refuse is not None:
            raise self.refuse
        return self.replies.pop(0)


def local(tmp_path, monkeypatch, server):
    c = cfg(tmp_path)
    p = OpenAICompatPlanner(c, "local", c.get("planner.endpoints.local"))
    p.spare = [f"cached-model-{i}" for i in range(12)]      # what a local server lists: everything it has on disk
    monkeypatch.setattr(planner.urllib.request, "build_opener", server)
    return p


PLAN = {"properties": {"steps": {"type": "array"}, "inputs": {"type": "object"}, "try": {"type": "array"}, "blocked": {"type": "string"}}}


def test_a_streamed_reply_is_read_up_to_its_json_and_no_further(tmp_path, monkeypatch):
    reply = Reply([b": keepalive 512/1180\n", b"\n",
                   sse('{"steps": [{"goal": "open {Settings}", "evidence": "a \\"Settings\\" window"}], '), b"\n",
                   sse('"inputs": {}, "try": [], "blocked": ""}'), b"\n",
                   sse("\n\nThat plan opens Settings first, then…"), b"data: [DONE]\n"])
    server = Server(reply)
    p = local(tmp_path, monkeypatch, server)

    assert p.complete("sys", "plan it", PLAN)["steps"] == [{"goal": "open {Settings}", "evidence": 'a "Settings" window'}]
    assert server.sent[0]["stream"] is True
    assert reply.read == 5, "closed once the object was whole: what the model writes after it is not waited for"
    assert "single line" in server.sent[0]["messages"][1]["content"], "indentation is written a token at a time"


def test_a_reply_that_times_out_is_not_asked_for_again(tmp_path, monkeypatch):
    server = Server(Reply([b": keepalive 512/1180\n", sse('{"steps": [')], fail=(1, TimeoutError("timed out"))))
    p = local(tmp_path, monkeypatch, server)
    with pytest.raises(PlannerError, match="no reply within"):
        p.complete("sys", "plan it", PLAN)
    assert len(server.sent) == 1, "a second copy would only double what the server is already doing"

    connect = Server(refuse=urllib.error.URLError(TimeoutError("timed out")))
    p = local(tmp_path, monkeypatch, connect)
    with pytest.raises(PlannerError, match="no reply within"):
        p.complete("sys", "plan it", PLAN)
    assert len(connect.sent) == 1


def test_an_unreadable_reply_is_asked_for_once_more_and_only_once(tmp_path, monkeypatch):
    """The retry count used to be 2 plus every model the server listed — 14 on this Mac."""
    server = Server(*[Reply([sse("I cannot plan this."), b"data: [DONE]\n"]) for _ in range(14)])
    p = local(tmp_path, monkeypatch, server)
    with pytest.raises(PlannerError):
        p.complete("sys", "plan it", PLAN)
    assert len(server.sent) == 2


def test_a_whole_object_is_told_apart_from_braces_in_strings_and_in_reasoning():
    assert not _whole_object('{"goal": "type }}} here"')
    assert _whole_object('{"goal": "type }}} here"}')
    assert not _whole_object('<think>so {"a": 1} would do')
    assert _whole_object('<think>so {"a": 1} would do</think>{"a": 1}')
    assert not _whole_object('Sure: "{"')


def test_a_question_about_the_screen_is_not_handed_the_whole_route(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    ctx = eng._ctx("make a new document", {}, None, "t")
    obs = observe(ctx)
    obs.notes["fields"] = ["Recipient: zw@example.com"]
    task = eng._new_task("make a new document", {}, None)

    route = eng._brief(task, ctx, obs)
    asked = eng._brief(task, ctx, obs, acting=False)
    assert {"actions_available", "app_model", "running_apps"} <= set(route)
    assert not {"actions_available", "app_model", "running_apps"} & set(asked)
    assert asked["field_contents"] == ["Recipient: zw@example.com"], "what a field holds was read off the action labels"
    assert asked["window"] == route["window"] and asked["screen_text"] == route["screen_text"]


@pytest.mark.parametrize("ask", [
    lambda pl, goal, ctx: pl.plan(goal, ctx, ["it deletes something"]),
    lambda pl, goal, ctx: pl.replan(goal, ctx, ["menu File ▸ New"], "nothing happened", ["it deletes something"]),
    lambda pl, goal, ctx: pl.judge_done(goal, ctx),
    lambda pl, goal, ctx: pl.answer(goal, ctx),
    lambda pl, goal, ctx: pl.fill(goal, "type the name", "type into Name", ctx),
], ids=["plan", "replan", "done", "answer", "fill"])
def test_what_is_the_same_on_every_call_comes_first(tmp_path, ask):
    """A local server keeps the longest prefix it has seen. Two calls about different goals and screens now
    share everything up to the goal; before, they shared nothing past the system prompt."""
    replies = [{"steps": [], "inputs": {}, "try": [], "blocked": "", "done": True, "why": "", "answer": "", "text": ""}] * 2
    back = FakeBackend(replies)
    pl = Planning(Config.load(), back)
    ask(pl, "make a new document", {"app": "TextEdit", "screen_text": "Untitled"})
    ask(pl, "total the sales column", {"app": "Numbers", "screen_text": "sales.csv"})
    a, b = back.prompts
    shared = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
    assert a[:shared].rstrip().endswith(("Goal:", "The user asked:")), a[:shared][-200:]
    assert shared > 200, "the instructions are part of what is kept"


def test_the_same_plan_again_is_no_new_route(tmp_path):
    """A real task was handed the same two sub-goals three times running, 25-40 s apiece, and each time looked
    again instead of acting. The same answer counts as no answer, so "rethink" with nothing new is fruitless."""
    from macwork.model import Step

    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    same = {"steps": [{"goal": "compute 17×23", "evidence": "391 on the display"}], "inputs": {}, "try": [{"keys": "cmd+n"}], "blocked": ""}
    other = {**same, "steps": [{"goal": "open TextEdit first", "evidence": "a TextEdit window"}]}
    eng._planner = FakeBackend([dict(same), dict(same), dict(other)])
    task = eng._new_task("compute 17×23 and write it into TextEdit", {}, None)
    ctx = eng._step_context(task)
    obs = observe(ctx)

    assert eng._consult(task, ctx, obs, "the actions on screen do not lead toward the goal")
    task.steps.append(Step(0, "menu File ▸ New Document (⌘N)", ok=True, events=["AXWindowCreated"]))
    assert not eng._consult(task, ctx, obs, "the actions on screen do not lead toward the goal"), "the same route again"
    task.steps.append(Step(1, "press cmd+n", ok=False))                  # something went wrong: asked afresh
    assert eng._consult(task, ctx, obs, "the actions on screen do not lead toward the goal"), "a different route is one"
    assert task.plan == ["open TextEdit first"]
