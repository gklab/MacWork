"""Found by one real run of evals/behaviour.yaml on 2026-09-22, after the six batches.

* a task whose whole goal was to read a file asked the user before its first step: the floor judged "read the
  text of long.txt" as navigate at 0.64, under the bar, because reading was not a verdict it could give;
* the planner is shown this Mac's home as `~` and wrote a route back that way — `file://~/Library/...` —
  which nothing can open;
* a task read the file, answered the question correctly, met the app's own recovery prompt and reported
  `blocked`: true of the app, not of the question;
* the harness counted every window object the window server lists — toolbars, tooltips, unrealised panels —
  so a task that took no step "left four TextEdit windows behind";
* two tasks named an app this Mac does not have and were scored on whatever opened the file instead.
"""

from macwork import evals
from macwork.engine import Engine
from macwork.model import Affordance, Task
from macwork.observe import Ctx
from tests.test_engine import FakeHelper, ScriptedDecider, cfg, worded


class Judges(ScriptedDecider):
    """Reads are reads, everything else navigates — the way a real classifier answered when offered the verdict."""

    def _classify(self, q):
        pick = "read" if "read the text of" in worded(q) or "read all the text" in worded(q) else "navigate"
        return {"type": "choice", "choice": pick, "probabilities": {pick: 0.8}}


def test_reading_a_file_is_not_gated(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Judges([]))
    ctx = Ctx(eng.cfg, eng.helper, app={"pid": 42, "bundle_id": "com.apple.TextEdit"})
    ctx.gate = eng.gate
    read = Affordance("T0", "file", "read", "read the text of ~/x/long.txt — returns all 17 lines of it at once", {"path": "/x/long.txt"})
    assert eng._floor("t", ctx, read) == [], "a read at 0.8 is released"


def test_a_read_verdict_cannot_release_an_open(tmp_path):
    """The verdict is narrowed to the actions it is a fact about: "open" is not a read, whatever opens."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Judges([]))
    ctx = Ctx(eng.cfg, eng.helper, app={"pid": 42, "bundle_id": "com.apple.TextEdit"})
    ctx.gate = eng.gate
    opened = Affordance("p0", "file", "open", "read the text of the app's manual by opening it", {"path": "/x/manual.pdf"})
    assert eng._floor("t", ctx, opened) != []


def test_a_route_the_planner_wrote_with_a_tilde_is_a_path_again(tmp_path):
    import os
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="open it")
    task.tries = [{"open_url": "file://~/Library/Caches/x.csv"}, {"open_url": "~/Desktop/y.txt"}, {"open_url": "https://example.com/~user"}]
    urls = [a.target["url"] for a in eng._suggested(task)]
    home = os.path.expanduser("~")
    assert urls == [f"file://{home}/Library/Caches/x.csv", f"{home}/Desktop/y.txt", "https://example.com/~user"]


def test_a_question_answered_is_answered_even_when_the_app_then_blocks(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("which item costs the most?", {}, None)
    task.outputs["answer"] = "the monitor, at 1790"
    res = eng._finish(task, "blocked", "the app shows a recovery prompt only the user can dismiss")
    assert res["status"] == "done" and "recovery prompt" in task.outputs["unfinished_but_answered"]


def test_the_harness_counts_windows_a_person_would_call_windows(tmp_path):
    class Helper(FakeHelper):
        screen = [{"pid": 42, "id": 1, "layer": 0, "alpha": 1, "frame": [0, 0, 800, 600]},
                  {"pid": 42, "id": 2, "layer": 8, "alpha": 1, "frame": [0, 0, 100, 30]},      # a tooltip
                  {"pid": 42, "id": 3, "layer": 0, "alpha": 0, "frame": [0, 0, 640, 480]},     # invisible
                  {"pid": 7, "id": 4, "layer": 0, "alpha": 1, "frame": [0, 0, 300, 300]}]

    eng = Engine(cfg(tmp_path), helper=Helper(), decider=ScriptedDecider([]))
    assert evals._real_windows(eng) == {42: {1}, 7: {4}}


def test_a_task_naming_an_app_this_mac_lacks_is_not_counted(tmp_path, monkeypatch):
    class Eng:
        cfg = None
        cache: dict = {}
        models = None
        helper = FakeHelper()
        decider = ScriptedDecider([])

        def _resolve_app(self, hint, running):
            return None

        def _installed_named(self, hint):
            return None

        def do(self, *a, **k):
            raise AssertionError("never run")

    from tests.test_engine import cfg as engine_cfg
    suite = tmp_path / "s.yaml"
    suite.write_text("fresh: false\ntasks:\n  - id: t1\n    app: com.example.absent\n    goal: use it\n    check:\n      expect_status: [done]\n",
                     encoding="utf-8")
    e = Eng()
    e.cfg = engine_cfg(tmp_path)
    monkeypatch.setattr(evals, "_launch", lambda engine, hint: None)
    monkeypatch.setattr(evals, "_locked", lambda engine: False)
    monkeypatch.setattr(evals, "_desktop", lambda engine: ({}, set()))
    monkeypatch.setattr(evals, "sweep", lambda engine, before: [])
    row = evals.run_suite(e, suite, out_dir=None)["rows"][0]
    assert row["status"] == "invalid" and "not installed" in row["why"]
