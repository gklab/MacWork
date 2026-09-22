"""Budgets, identity and cleanup across a task's life.

What these protect:
* the step and time budgets used to run from when the *task* started, so a caller who took longer than
  `budget_s` to answer a `need_confirm` got a task that failed on its first iteration after resuming;
* `act` resolved an affordance id against whatever observation happened to be in the engine's one slot, so
  two callers interleaving `observe` made one act on the other's screen;
* `cancel` answered "cancelled: true" for ids it had never heard of, and never forgot an id afterwards;
* a helper that died and restarted left the caches that held its (now dead) references in place.
"""

import time

from macwork.engine import Engine
from macwork.model import Step, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def test_a_resumed_task_gets_a_fresh_budget_but_a_total_ceiling(tmp_path):
    engine = Engine(cfg(tmp_path, config={"engine": {"budget_s": 60, "max_steps": 3, "total_steps": 5}}),
                    helper=FakeHelper(), decider=ScriptedDecider([]))
    budget = engine.cfg.section("engine")
    task = Task(goal="send this email")
    task.steps = [Step(n, f"step {n}") for n in range(3)]     # a first run that used its whole step budget
    task.spent_s = 20.0                                       # 20 s of work so far
    task.started = time.monotonic() - 3600                    # and then the caller thought about it for an hour

    task.begin_run(0, 0.0)                                    # …and answered: a new run starts here
    assert engine._overspent(task, budget) is None, "neither the earlier steps nor the waiting are this run's"

    task.steps += [Step(3, "one more"), Step(4, "and another")]
    assert engine._overspent(task, budget).whole_task, "but five steps is the whole task's ceiling"

    task.steps = task.steps[:3]
    task.spent_s = 10_000.0                                   # a task that really has been working all day stops
    assert engine._overspent(task, budget).whole_task


def test_the_run_budget_still_ends_a_run_that_overruns(tmp_path):
    engine = Engine(cfg(tmp_path, config={"engine": {"budget_s": 0}}), helper=FakeHelper(), decider=ScriptedDecider([]))
    result = engine.do("do something")
    assert result["status"] == "failed"
    assert "time budget" in result["reason"]
    assert engine.decider.calls == 0, "it should not have asked anything before noticing"


def test_an_id_from_an_earlier_observation_does_not_act(tmp_path):
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    first = engine.observe()
    stale = first["affordances"][0]["id"]

    engine.observe()                                   # a second caller looks; the slot now holds their screen
    refused = engine.act(stale)
    assert refused["ok"] is False
    assert "earlier observation" in refused["error"]


def test_an_id_from_the_current_observation_still_acts(tmp_path):
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    seen = engine.observe()
    pressable = next(a for a in seen["affordances"] if a["verb"] == "press" and not a.get("slots"))
    assert engine.act(pressable["id"])["ok"] is True


def test_cancelling_an_id_nobody_knows_says_so(tmp_path):
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert engine.cancel("never-existed") == {"task_id": "never-existed", "cancelled": False,
                                              "error": "unknown or expired task"}


def test_a_restarted_helper_takes_its_stale_references_with_it(tmp_path):
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    engine.cache.update({"menu.snap": ("key", 0, {"nodes": []}), "ambient": {"42|Window"},
                         "vision.ocr": {"x": 1}, "installed": [{"name": "Calculator"}]})
    engine.observe()
    assert engine._last is not None

    engine._helper_restarted()
    assert "menu.snap" not in engine.cache and "ambient" not in engine.cache and "vision.ocr" not in engine.cache
    assert engine.cache["installed"], "what is installed does not depend on the helper's references"
    assert engine._last is None


def test_looking_does_not_wait_for_a_task_to_finish(tmp_path):
    """There is one Mac, so driving it goes one at a time — but a caller asking what is on screen was made to
    wait out the whole task, and then came back describing a screen from minutes ago."""
    import threading
    import time as _t

    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    driving = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with engine._lock:          # what a running task holds
            driving.set()
            release.wait(2)

    threading.Thread(target=hold, daemon=True).start()
    assert driving.wait(2)
    assert engine.busy() is True

    started = _t.monotonic()
    seen = engine.observe()
    assert _t.monotonic() - started < 1.5, "looking waited for the lock"
    assert seen["affordances"], "and it really did look"
    release.set()


# --- staying installed, and staying running ---------------------------------------------------------------

def test_a_developer_id_is_found_rather_than_written_down(monkeypatch):
    """Accessibility is granted to a signature, not a path. Ad-hoc signing gives a new one on every rebuild,
    which is why the permission had to be granted again each time and the helper could never just stay put."""
    import subprocess

    from macwork import cli

    listing = (
        '  1) AAAA "Apple Development: Someone (X1)"\n'
        '  2) BBBB "Developer ID Application: Their Company (B3K3T2WPR6)"\n'
        '     2 valid identities found\n')
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": listing, "returncode": 0})())
    assert cli.signing_identity() == "Developer ID Application: Their Company (B3K3T2WPR6)"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "  0 valid identities found\n",
                                                                          "returncode": 1})())
    assert cli.signing_identity() is None, "no identity is not the same as a made-up one"


def test_the_daemon_plist_says_what_it_should(tmp_path, monkeypatch):
    import plistlib
    import subprocess

    from macwork import cli
    from macwork.config import Config

    monkeypatch.setattr(cli, "daemon_path", lambda: tmp_path / "dev.macwork.agent.plist")
    monkeypatch.setattr(cli.shutil, "which", lambda _: str(tmp_path / "macwork"))
    (tmp_path / "macwork").write_text("#!/bin/sh\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    args = type("A", (), {"action": "install", "transport": "streamable-http"})()
    assert cli.cmd_daemon(Config.load(user_dir=tmp_path / "none"), args) == 0

    plist = plistlib.loads((tmp_path / "dev.macwork.agent.plist").read_bytes())
    assert plist["ProgramArguments"][1:] == ["serve", "--transport", "streamable-http"]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}, "come back from a crash, stay down after a clean stop"
    assert plist["ProcessType"] == "Interactive", "it drives the UI and must not be throttled"


def test_what_the_cache_keeps_per_task_goes_with_the_task(tmp_path):
    """The pictures of every screen a task saw — about 1.4 KB each, up to 200 — and the windows it asked to
    read by sight were keyed on the task id and never collected: `_gc` dropped the task and left them."""
    import time as _time
    from macwork.engine import Engine
    from macwork.model import Task
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path, config={"engine": {"tasks_ttl_s": 0.01, "persist": False}}),
                 helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="look around")
    eng.tasks[task.id] = task
    eng.scope(task.id).pictures["sig"] = b"..."
    eng.scope(task.id).vision_wanted.add("w1")
    eng.scope("another-task")
    _time.sleep(0.02)
    eng._gc()
    assert task.id not in eng.tasks
    assert task.id not in eng._scopes, "its live state went with it"
    assert "another-task" in eng._scopes, "only what belonged to the expired task"
