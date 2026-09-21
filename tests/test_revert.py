"""Putting back what a task changed.

`tidy` closes what a task *opened*. What it *changed* stayed changed — a task that typed into the wrong
document or half-finished an edit left the Mac that way, and the only recovery was doing it by hand.

Nothing here knows the word "undo". Every app publishes its own undo command, in its own words, and the menu
provider already offers it like any other action, so this asks the decider to pick among what the app itself
offers. Which steps changed anything is not judged again either: the safety floor classifies every action
before it runs, and `Step.effect` is that judgement.
"""

from typing import Any

import pytest

from macwork.config import Config
from macwork.engine import Engine
from macwork.model import Affordance, Observation, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Eng(Engine):
    """An engine whose looking and acting are recorded rather than done."""

    def __init__(self, *a: Any, offers: list[Affordance] | None = None, **k: Any) -> None:
        super().__init__(*a, **k)
        self.performed: list[str] = []
        self.offers = offers if offers is not None else [
            Affordance("m1", "menu", "press", "Undo Typing", {}, context="Edit"),
            Affordance("m2", "menu", "press", "Delete All", {}, context="Edit"),
        ]

    def _ctx(self, goal, inputs, app, key):          # type: ignore[no-untyped-def]
        c = super()._ctx(goal, inputs, app, key)
        c.app = app if isinstance(app, dict) else c.app
        return c

    def _execute(self, ctx, a, params):              # type: ignore[no-untyped-def]
        from macwork.act import Outcome
        self.performed.append(a.label)
        return Outcome(True), []


@pytest.fixture
def parts(tmp_path, monkeypatch):
    import macwork.tidy as tidy

    eng = Eng(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))

    def fake_observe(ctx):
        o = Observation(app=ctx.app, window="Document", affordances=list(eng.offers))
        return o
    monkeypatch.setattr(tidy, "observe", fake_observe)

    task = Task(goal="change the meeting time", id="t1")
    task.changed = [{"n": 0, "action": "type 「Thursday 15:40」", "effect": "write",
                     "app": {"pid": 42, "name": "TextEdit", "bundle_id": "com.apple.TextEdit"}}]
    eng.tasks[task.id] = task
    return eng, task


class Gate:
    """`Engine.gate` builds a fresh one each time, so the decision is intercepted here instead."""

    def __init__(self, answer, seen):
        self.answer, self.seen = answer, seen

    def decide(self, redactor, state, questions, task=None):
        self.seen.append(state)
        opts = questions["back"]["criteria"]
        got = next((k for k, v in opts.items() if self.answer and self.answer in v), "none")
        return {"back": {"type": "choice", "choice": got, "confidence": 1.0}}


def answer(eng, pick):
    """Make the decider choose the affordance whose label contains `pick` (or "none")."""
    eng.seen = []
    type(eng).gate = property(lambda self, p=pick: Gate(p, self.seen))


# --------------------------------------------------------------------------- what counts as a change

def test_navigating_is_not_a_change_to_put_back():
    from macwork.model import Step
    assert Step(0, "open the Edit menu", effect="navigate").effect == "navigate"


def test_a_task_that_changed_nothing_has_nothing_to_revert(parts):
    eng, task = parts
    task.changed = []
    assert eng.revert(task.id)["reverted"] == []


def test_an_unknown_task_is_said_so(parts):
    eng, _ = parts
    assert eng.revert("nope")["ok"] is False


# --------------------------------------------------------------------------- doing it

def test_the_app_s_own_undo_is_what_gets_performed(parts):
    eng, task = parts
    answer(eng, "Undo")
    res = eng.revert(task.id)
    assert res["ok"] and eng.performed == ["Undo Typing"]


def test_the_choice_is_never_made_by_reading_the_label():
    """The same task on a German Mac has to work, and nobody here has heard of "Widerrufen".

    Which action undoes something is the decider's judgement, made from labels the *app* wrote. So the test
    is not "does the word undo appear in this file" — it appears in a sentence explaining a refusal to the
    person — but "does this code ever look inside a label", which is the thing that would tie it to one
    language.
    """
    import ast
    import pathlib
    src = pathlib.Path("macwork/tidy.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # `"some word" in a.label`, `a.label == "some word"`, `a.label.startswith("…")`
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
            targets = ast.unparse(node.comparators[0])
            assert "label" not in targets and "title" not in targets, f"reads a label: {ast.unparse(node)}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
           node.func.attr in ("startswith", "endswith", "find", "index") and "label" in ast.unparse(node.func.value):
            raise AssertionError(f"reads a label: {ast.unparse(node)}")


def test_it_stops_rather_than_guessing_when_nothing_puts_it_back(parts):
    eng, task = parts
    answer(eng, None)                        # the decider says "none"
    res = eng.revert(task.id)
    assert res["ok"] is False and eng.performed == [] and "stopped_at" in res


def test_it_works_backwards_from_the_most_recent_change(parts):
    eng, task = parts
    task.changed = [dict(task.changed[0], action="first"), dict(task.changed[0], action="second")]
    answer(eng, "Undo")
    eng.revert(task.id)
    assert [s["what_was_done"] for s in eng.seen] == ["second", "first"]


def test_how_far_back_it_goes_is_bounded(parts):
    eng, task = parts
    task.changed = [dict(task.changed[0], action=f"change {i}") for i in range(20)]
    answer(eng, "Undo")
    assert len(eng.revert(task.id, max_steps=3)["reverted"]) == 3


# --------------------------------------------------------------------------- when it refuses

def test_it_refuses_while_someone_is_using_the_mac(parts):
    """Their keystrokes went into these same apps: an undo would take those back, not the task's work."""
    eng, task = parts
    eng.helper.call = lambda m, timeout=30.0, **p: {"idle_s": 0.2} if m == "input.idle" else FakeHelper().call(m, timeout, **p)
    res = eng.revert(task.id)
    assert res["ok"] is False and eng.performed == [] and "using the Mac" in res["error"]


def test_an_undo_meets_the_safety_floor_like_anything_else(parts, monkeypatch):
    """Putting something back by deleting it is not a quieter thing to do than what it undid."""
    eng, task = parts
    answer(eng, "Delete All")
    monkeypatch.setattr(Eng, "_floor", lambda self, tid, ctx, a, w=None, ask=True: ["delete"] if "Delete" in a.label else [])
    res = eng.revert(task.id)
    assert res["ok"] is False and eng.performed == []
