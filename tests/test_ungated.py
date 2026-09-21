"""Every action the engine runs has to have met the floor — including the ones nobody chose on the spot.

The floor is described as covering "every action about to run". Four paths did not call it:

  * a task resumed from `ambiguous`: the caller picked *which* option, never that it was safe. `loop.py`
    sees `task.held` and goes straight to `_perform`, skipping `_judge` where `_floor` lives.
  * `tidy._back_out`, whose own docstring says "never one the safety floor gates" — its only filter is
    `_risky`, which does not ask the decider and falls back to the two-language word list.
  * `learn()` and `learn._way_back`, the same shape, in docstrings that promise the same thing.

All three pick their action with `screen_text` in the state, unlike the floor, which is deliberately blind
to it — so these are the paths where text on a page has the most influence and the least gating.

A confirmed action is different: the caller has just said yes to that exact action, and asking again would
be asking twice.
"""

from typing import Any

import pytest

from macwork.engine import Engine
from macwork.model import Affordance, Observation, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Floor(ScriptedDecider):
    """Classifies anything whose label contains 删 or Delete as a delete; everything else navigates."""

    def decide(self, state, questions):
        out = {}
        for k, q in questions.items():
            text = str(q.get("instructions", "")) + str(q.get("criteria", ""))
            bad = "删" in text or "Delete" in text or "Löschen" in text
            if q.get("type") == "noul":
                out[k] = {"type": "noul", "noul": 0.9 if bad else 0.1, "confidence": 0.95}
            else:
                pick = "delete" if bad else "navigate"
                out[k] = {"type": "choice", "choice": pick, "probabilities": {pick: 0.99}}
        return out


def engine(tmp_path, **over):
    return Engine(cfg(tmp_path, **over), helper=FakeHelper(), decider=Floor([]))


# --------------------------------------------------------------------------- resumed from `ambiguous`

def test_choosing_which_option_is_not_saying_it_is_safe(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="整理一下", id="t1")
    task.status, task.options = "ambiguous", {"o1": Affordance("o1", "window", "press", "删除全部", {})}
    eng.tasks[task.id] = task
    res = eng.resume(task.id, choice_id="o1")
    assert res["status"] == "need_confirm", f"an ambiguous pick ran ungated: {res['status']}"


def test_an_action_the_caller_did_confirm_is_not_asked_about_twice(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="删掉它", id="t2")
    task.status = "need_confirm"
    task.held = Affordance("w1", "window", "press", "删除全部", {})
    eng.tasks[task.id] = task
    res = eng.resume(task.id, confirm=True)
    assert res["status"] != "need_confirm", "asked again about the very action just confirmed"


def test_a_harmless_ambiguous_pick_is_not_stopped(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="打开它", id="t3")
    task.status, task.options = "ambiguous", {"o1": Affordance("o1", "window", "press", "打开文稿", {})}
    eng.tasks[task.id] = task
    assert eng.resume(task.id, choice_id="o1")["status"] != "need_confirm"


# --------------------------------------------------------------------------- tidy and learn

def fake_gate(monkeypatch, eng, wants: str):
    """A gate that answers both kinds of question a real one sees: the caller's "which of these" (it picks
    the option whose text contains `wants`) and the floor's "what does this do"."""
    def decide(redactor, state, questions, task=None):
        out = {}
        for name, q in questions.items():
            text = str(q.get("instructions", ""))
            if q.get("type") == "noul":
                out[name] = {"type": "noul", "noul": 0.9 if "Löschen" in text else 0.1, "confidence": 0.95}
            elif name in ("what",):                     # the floor classifying one action
                bad = "Löschen" in text or "删" in text
                pick = "delete" if bad else "navigate"
                out[name] = {"type": "choice", "choice": pick, "probabilities": {pick: 0.99}}
            else:                                        # the caller's own "which of these"
                opts = q.get("criteria") or {}
                out[name] = {"type": "choice", "confidence": 1.0,
                             "choice": next((k for k, v in opts.items() if wants in v), "none")}
        return out
    # Through monkeypatch, so it is undone: set on the class directly it outlived the test, and every engine
    # built afterwards in the same run answered through this fake — three unrelated tests failed that way.
    monkeypatch.setattr(type(eng), "gate", property(lambda self: type("G", (), {"decide": staticmethod(decide)})()),
                        raising=False)


def gated_pick(monkeypatch, eng, module, offers, wants="Löschen"):
    """Point the module's `observe` at a fixed screen and record what gets executed."""
    ran = []
    monkeypatch.setattr(module, "observe", lambda ctx: Observation(app=ctx.app, window="w", affordances=list(offers)))

    def execute(self, ctx, a, params):
        from macwork.act import Outcome
        ran.append(a.label)
        return Outcome(True), []
    monkeypatch.setattr(Engine, "_execute", execute)
    fake_gate(monkeypatch, eng, wants)
    return ran


# German, on purpose: `_risky` — the only filter these paths had — falls back to policy.yaml's word list,
# which is English and Chinese. "删除全部" would be caught by the words and hide the hole entirely.
DESTRUCTIVE = [Affordance("a1", "window", "press", "Alles Löschen", {}),
               Affordance("a2", "window", "press", "Abbrechen", {})]


def test_tidy_does_not_press_a_button_the_floor_never_saw(tmp_path, monkeypatch):
    import macwork.tidy as tidy
    eng = engine(tmp_path)
    ran = gated_pick(monkeypatch, eng, tidy, DESTRUCTIVE)
    task = Task(goal="x", id="t4")
    eng.tasks[task.id] = task
    eng._back_out(task, {"pid": 42, "name": "文本编辑"}, "cancel_quit")
    assert ran == [], f"tidy ran an ungated action: {ran}"


def test_learn_does_not_press_one_either(tmp_path, monkeypatch):
    import macwork.learn as learn
    eng = engine(tmp_path)
    ran = gated_pick(monkeypatch, eng, learn, DESTRUCTIVE)
    task = Task(goal="x", id="t5")
    eng.tasks[task.id] = task
    ctx = eng._ctx("", {}, {"pid": 42, "name": "文本编辑"}, task.id)
    ctx.gate = eng.gate
    obs = Observation(app=ctx.app, window="w", affordances=list(DESTRUCTIVE))
    pick = eng._way_back(ctx, obs, "w", "了什么")
    assert pick is not None, "the decider did pick the destructive one; the test is not exercising the gate"
    assert eng.vetted("learn", ctx, pick, "w") is None, "learn would have run an ungated action"
    assert ran == []


def test_backing_out_of_something_harmless_still_works(tmp_path, monkeypatch):
    """The point of these paths is to put things back; gating must not switch them off."""
    import macwork.tidy as tidy
    eng = engine(tmp_path)
    offers = [Affordance("a1", "window", "press", "Abbrechen", {}), Affordance("a2", "window", "press", "Später", {})]
    ran = gated_pick(monkeypatch, eng, tidy, offers, wants="Abbrechen")
    task = Task(goal="x", id="t6")
    eng.tasks[task.id] = task
    eng._back_out(task, {"pid": 42, "name": "文本编辑"}, "cancel_quit")
    assert ran == ["Abbrechen"]
