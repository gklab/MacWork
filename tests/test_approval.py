"""A confirmation answers "do this now", not "do this whenever".

`task.approved` was a set of bare labels. Two consequences, both reachable in one task:

  * Labels repeat constantly — 「Delete」, "OK", "Send", "Don't Save". Confirming one dialog's button
    released every later action anywhere in the task that happened to be called the same thing, in any app.
  * It also switched off the re-judge of text: `_perform` classifies an action again once the text to be
    typed is known, because typed text can change what the action does — and skipped that entirely when
    the base label was approved. Confirming "type at the cursor" once released typing anything after it.

An approval now names the action it was given for and is spent when that action runs.
"""

from typing import Any

import pytest

from macwork.engine import Engine
from macwork.model import Affordance, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def eng(tmp_path):
    return Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))


def aff(label, context="", key="", channel="window", verb="press"):
    return Affordance("x", channel, verb, label, {}, context=context, key=key)


# --------------------------------------------------------------------------- what an approval covers

def test_confirming_one_action_does_not_release_the_next_one_called_the_same(tmp_path):
    e = eng(tmp_path)
    task = Task(goal="x", id="t1")
    here = aff("press 「Delete」", context="dialog A")
    e.approve(task, here)
    assert not e._needs_confirm(here, 0.0, task.approved, floor=["delete"])
    later = aff("press 「Delete」", context="another window B")
    assert e._needs_confirm(later, 0.0, task.approved, floor=["delete"]), \
        "a button somewhere else with the same name went through on an earlier confirmation"


def test_an_approval_is_spent_when_its_action_runs(tmp_path):
    e = eng(tmp_path)
    task = Task(goal="x", id="t2")
    a = aff("press 「Delete」", context="dialog A")
    e.approve(task, a)
    assert not e._needs_confirm(a, 0.0, task.approved, floor=["delete"])
    e.spend(task, a)
    assert e._needs_confirm(a, 0.0, task.approved, floor=["delete"]), \
        "the same action ran twice on one confirmation"


def test_the_identity_is_the_language_independent_one_where_there_is_one(tmp_path):
    """A relabelled button is the same button; an approval should survive the interface language changing
    under it, which is what `Affordance.key` is for."""
    e = eng(tmp_path)
    task = Task(goal="x", id="t3")
    e.approve(task, aff("press 「Delete」", context="c", key="axid:deleteButton"))
    assert not e._needs_confirm(aff("press 「Delete」", context="c", key="axid:deleteButton"),
                                0.0, task.approved, floor=["delete"])


def test_a_different_button_in_the_same_place_is_not_covered(tmp_path):
    e = eng(tmp_path)
    task = Task(goal="x", id="t4")
    e.approve(task, aff("press 「Delete」", context="c", key="axid:deleteButton"))
    assert e._needs_confirm(aff("press 「Delete All」", context="c", key="axid:deleteAllButton"),
                            0.0, task.approved, floor=["delete"])


# --------------------------------------------------------------------------- the text re-judge

def test_confirming_the_act_of_typing_does_not_release_what_is_typed(tmp_path):
    """`_perform` re-judges once the text is known, because the text is what makes it dangerous. That
    check read `chosen.label not in task.approved`, and the base label is the same every time."""
    import inspect

    from macwork import loop
    src = inspect.getsource(loop.LoopMixin._perform)
    assert "chosen.label not in task.approved" not in src, \
        "the text re-judge is still keyed on the base label, which one confirmation covers forever"


def test_a_typed_action_is_judged_with_its_text_in_it(tmp_path):
    """The re-judge itself must still happen — it is what catches `rm -rf /` going into a terminal."""
    import inspect

    from macwork import loop
    src = inspect.getsource(loop.LoopMixin._perform)
    assert "doing" in src and "_floor(" in src


# --------------------------------------------------------------------------- it survives a restart

def test_an_approval_round_trips_through_the_store(tmp_path):
    from macwork.store import Store
    from macwork.config import Config
    e = eng(tmp_path)
    task = Task(goal="x", id="t5")
    a = aff("press 「Delete」", context="c")
    e.approve(task, a)
    store = Store(Config.load(overrides={"config": {"store": {"path": str(tmp_path / "s.db")}}}))
    store.save(task)
    back = store.get("t5")
    assert back is not None and not e._needs_confirm(a, 0.0, back.approved, floor=["delete"])


def test_confirming_the_text_re_judge_actually_gets_past_it(tmp_path):
    """The re-judge asks about the action *with its text in it*, so that is the identity it has to be
    confirmed under. Approving the bare action instead would leave the caller confirming forever."""
    from macwork.model import Task
    e = eng(tmp_path)
    task = Task(goal="x", id="t6")
    typed = aff("type at the cursor — typing 「rm -rf /」", context="Terminal", channel="keys", verb="type")
    task.held = aff("type at the cursor", context="Terminal", channel="keys", verb="type")
    task.confirm_key = e.approval_key(typed)
    e.resume_approval(task)
    assert not e._needs_confirm(typed, 0.0, task.approved, floor=["execute"])


def test_without_one_the_held_action_itself_is_what_gets_approved(tmp_path):
    from macwork.model import Task
    e = eng(tmp_path)
    task = Task(goal="x", id="t7")
    a = aff("press 「Delete」", context="c")
    task.held = a
    e.resume_approval(task)
    assert not e._needs_confirm(a, 0.0, task.approved, floor=["delete"])


def test_the_caller_is_not_asked_the_same_question_twice_in_a_row(tmp_path):
    """The loop-level regression: the re-judge asks about the text, the caller says yes, and the very next
    turn asked again because the approval was filed under a different name. That is an infinite loop with
    a human in it."""
    class Decide(ScriptedDecider):
        """Routed by question name — `action` and `move` both offer a "done" option, so anything keying on
        the options answers the wrong question."""

        def decide(self, state, questions):
            out = {}
            for k, q in questions.items():
                crit = q.get("criteria") or {}
                if q.get("type") == "noul":
                    out[k] = {"type": "noul", "noul": 0.9, "confidence": 0.95}
                elif k == "action":
                    pick = next((c for c, v in crit.items() if "cursor" in v or "cursor" in v), "done")
                    out[k] = {"type": "choice", "choice": pick, "probabilities": {pick: 0.95}}
                elif k == "move":
                    out[k] = {"type": "choice", "choice": "act", "probabilities": {"act": 0.95}}
                else:                                               # the floor classifying an action
                    bad = "rm -rf" in str(q.get("instructions", ""))
                    pick = "execute" if bad else "navigate"
                    out[k] = {"type": "choice", "choice": pick, "probabilities": {pick: 0.99}}
            return out

    e = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "keys", "typing"]}}),
               helper=FakeHelper(), decider=Decide([]))
    res = e.do("type this command in", inputs={"text": "rm -rf /tmp/x"})
    assert res["status"] == "need_confirm", f"a shell command was typed without asking: {res['status']}"
    asked = 1
    while res["status"] == "need_confirm" and asked < 4:
        res = e.resume(res["task_id"], confirm=True)
        asked += 1
    assert res["status"] != "need_confirm", f"still asking after {asked} confirmations"
