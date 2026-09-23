"""Things that break when something else goes wrong at the same time.

Each of these was found by an audit reading the code, and each is the kind that a green test suite says
nothing about: a name that is never imported on a path no test walks, a list mutated from two threads, a
verdict that means "I could not answer" and reads as "yes".
"""

import threading
from typing import Any

import pytest

from macwork.config import Config
from macwork.model import Affordance, Task


# --------------------------------------------------------------------------- a name that was never imported

def test_learn_can_reach_an_app_that_is_not_running():
    """`macwork learn <app>` for an app that is not open — its documented primary use — called
    `installed_apps()`, which learn.py never imports. A NameError, on the only path that needs it."""
    import macwork.learn as learn
    assert hasattr(learn, "installed_apps"), "learn.py uses installed_apps and does not import it"


def test_every_name_each_module_uses_is_one_it_has():
    """The same shape anywhere else. Compiling is not enough — Python resolves a global at call time, so a
    missing import is invisible until the branch runs."""
    import ast
    import builtins
    import importlib
    import pathlib

    for path in sorted(pathlib.Path("macwork").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, (ast.arg,)):
                bound.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bound |= set(node.names)
        module = importlib.import_module(f"macwork.{path.stem}") if path.stem != "__init__" else None
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = {u for u in used - bound if module is not None and not hasattr(module, u)}
        assert not missing, f"{path.name} uses {sorted(missing)} and neither imports nor defines them"


# --------------------------------------------------------------------------- two threads, one fallback list

def test_the_decider_chain_survives_two_failures_at_once():
    """Every step makes two `decide()` calls — the step's own and the safety classification beside it. The
    chain dropped its head with a bare `pop(0)`, so two failures arriving together could empty the list and
    wedge the engine for the rest of the session."""
    from macwork.decider import ChainDecider, DeciderError

    class Fails:
        name = "flaky"

        def decide(self, state, questions):
            raise DeciderError("gone")

    class Works:
        name = "steady"

        def decide(self, state, questions):
            return {"q": {"type": "noul", "noul": 1.0}}

    chain = ChainDecider([Fails(), Fails(), Works()])
    out: list[Any] = []
    threads = [threading.Thread(target=lambda: out.append(chain.decide({}, {"q": {}}))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(out) == 8 and all(o for o in out), f"only {len(out)} of 8 decisions came back"
    assert chain.deciders, "the chain emptied itself"


def test_it_still_stops_when_everyone_has_failed():
    from macwork.decider import ChainDecider, DeciderError

    class Fails:
        name = "x"

        def decide(self, state, questions):
            raise DeciderError("gone")

    with pytest.raises(DeciderError):
        ChainDecider([Fails(), Fails()]).decide({}, {})


# --------------------------------------------------------------------------- an answer that is not one

def test_an_answer_with_no_choice_does_not_release_the_action(tmp_path):
    """`(ans.get("what") or {}).get("choice", "")` gives "" when the decider answered nothing usable, and
    "" was treated as "no category applies" — the same as a release."""
    from macwork.engine import Engine
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg

    class Empty(ScriptedDecider):
        def decide(self, state, questions):
            return {k: {"type": "choice"} for k in questions}      # no choice, no probabilities

    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Empty([]))
    ctx = eng._ctx("", {}, {"pid": 1, "name": "X", "bundle_id": "com.x"}, "t")
    ctx.gate = eng.gate
    a = Affordance("m", "menu", "press", "menu Ablage ▸ Löschen", {}, context="Ablage")
    gated = eng._floor("t", ctx, a, "w")
    assert gated, "an unanswerable classification released the action"


# --------------------------------------------------------------------------- a provider whose command timed out

def test_a_provider_that_times_out_does_not_break_the_look(tmp_path, monkeypatch):
    """The shortcuts and files providers run a command with a 10 s timeout, and observe() catches a timeout with
    the helper's errors. Its handler read `.code`, which only a helper error has, so a `shortcuts list` that
    timed out made the look itself raise AttributeError instead of noting the one provider that had nothing."""
    import subprocess

    from macwork.observe import Ctx, observe
    from tests.test_engine import FakeHelper, cfg

    def stuck(args, *a, **k):
        raise subprocess.TimeoutExpired(args, k.get("timeout") or 10)
    monkeypatch.setattr(subprocess, "run", stuck)
    helper = FakeHelper()
    c = cfg(tmp_path, config={"observe": {"providers": ["shortcuts", "menu"]}})
    obs = observe(Ctx(c, helper, app=helper.call("apps.frontmost")["app"]))
    assert "timed out" in obs.notes["shortcuts_error"]
    assert obs.notes["menu_n"] > 0, "the providers after it were not asked"
