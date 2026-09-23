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
from macwork.helper import HelperError
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
                  {"pid": 42, "id": 5, "layer": 0, "alpha": 1, "frame": [0, 0, 1512, 33]},    # a strip, not a window
                  {"pid": 42, "id": 6, "layer": 0, "alpha": 1, "frame": [0, 0, 700, 500], "on_screen": False},  # another Space
                  {"pid": 7, "id": 4, "layer": 0, "alpha": 1, "frame": [0, 0, 300, 300]}]

    eng = Engine(cfg(tmp_path), helper=Helper(), decider=ScriptedDecider([]))
    assert evals._real_windows(eng) == {42: {1, 6}, 7: {4}}, "a window left on another Space is still left"
    assert eng.helper.did("screen.windows")[-1].get("all") is True


def test_an_app_the_run_launched_that_will_not_quit_is_forced(tmp_path, monkeypatch):
    """An afternoon of runs left the Mac full of apps that had been asked politely and stayed."""
    class Stubborn(FakeHelper):
        def __init__(self):
            super().__init__()
            self.gone = False

        def call(self, method, timeout=30.0, **p):
            if method == "apps.running":
                base = super().call(method, timeout, **p)
                return base + ([] if self.gone else [{"pid": 99, "name": "Stubborn", "bundle_id": "x.stubborn"}])
            if method == "apps.quit":
                self.calls.append((method, p))
                if p.get("force"):
                    self.gone = True
                return {"terminated": self.gone, "asked": True}
            return super().call(method, timeout, **p)

    h = Stubborn()
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    monkeypatch.setattr(evals, "_discard_prompt", lambda engine, pid: False)
    left = evals.sweep(eng, ({}, {42, 7}))          # 99 was not running when the run began
    assert not left and any(p.get("force") for m, p in h.calls if m == "apps.quit")


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


def test_an_answer_that_says_there_is_no_answer_does_not_end_the_task_as_done(tmp_path):
    """"The date is not shown, so today's date cannot be known" stood — nothing in it was invented — and it
    turned a task that had failed into `done`, twice in one ×3 run. It is reported; it is not an answer."""
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([])
    d.answers = 0.05
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"answer": "the date is not shown on this screen, so it cannot be known"}])
    task = eng._new_task("what day of the month is it?", {}, None)
    task.wants_answer = 1.0
    task.memory.facts.record("Clock", "World Clock", "Beijing, 11:23, Daytime, Today", 0)
    res = eng._finish(task, "failed", "no route to the goal was found")
    assert res["status"] == "failed" and task.outputs["answer"] and task.outputs["answer_is_no_answer"]

    d.answers = 0.95
    eng._planner = FakeBackend([{"answer": "Today: Beijing, 11:23"}])
    task = eng._new_task("what time is it in Beijing?", {}, None)
    task.wants_answer = 1.0
    task.memory.facts.record("Clock", "World Clock", "Beijing, 11:23, Daytime, Today", 0)
    assert eng._finish(task, "failed", "ran out")["status"] == "done"


def test_making_something_new_is_not_gated(tmp_path):
    """"File ▸ New Folder" was judged `other` and asked the user, twice in one run, for a goal that asked for
    exactly that. Making something new is not what the floor is for."""
    class Judges(ScriptedDecider):
        def _classify(self, q):
            pick = "create" if "New Folder" in worded(q) else "navigate"
            return {"type": "choice", "choice": pick, "probabilities": {pick: 0.8}}

    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Judges([]))
    ctx = Ctx(eng.cfg, eng.helper, app={"pid": 42, "bundle_id": "com.apple.finder"})
    ctx.gate = eng.gate
    assert eng._floor("t", ctx, Affordance("m1", "menu", "press", "menu File ▸ New Folder (⇧⌘N)", {}, context="File")) == []
    made_elsewhere = Affordance("s1", "shortcut", "run", "run shortcut 「New Folder」", {"name": "New Folder"})
    assert eng._floor("t", ctx, made_elsewhere) != [], "a Shortcut that 'creates' can create anywhere: still gated"


def test_a_menu_item_whose_reference_expired_is_found_again_and_pressed(tmp_path):
    """The menu tree is cached across looks and its references expire as snapshots pile up: a real task
    lost two steps in a row to "unknown or expired element ref" on a menu item that had not moved."""
    from macwork.act import CHANNELS
    from macwork.observe import PROVIDERS
    from tests.english_mac import APP, EnglishMac, MENUBAR
    import copy

    class Expiring(EnglishMac):
        def __init__(self):
            super().__init__()
            self.generation = 1

        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "menubar":
                self.calls.append((method, p))
                snap = copy.deepcopy(MENUBAR)
                for n in snap["nodes"]:          # every read hands out references of a new generation
                    n["ref"] = n["ref"].replace("g1.", f"g{self.generation}.")
                    if "parent" in n:
                        n["parent"] = n["parent"].replace("g1.", f"g{self.generation}.")
                return snap
            if method == "ax.perform" and str(p.get("ref", "")).startswith("g1."):
                raise HelperError("stale_ref", f"unknown or expired element ref {p['ref']}; take a new snapshot")
            return super().call(method, timeout, **p)

    helper = Expiring()
    from macwork.observe import Ctx, Observation
    ctx = Ctx(cfg(tmp_path), helper, app=APP, goal="", inputs={}, task="t", gate=None, cache={})
    obs = Observation(app=APP, window="Untitled", affordances=[])
    PROVIDERS["menu"](ctx, obs)                                   # generation 1 references, then they expire
    new = next(a for a in obs.affordances if "New" in a.label and "Delete" not in a.label)
    helper.generation = 9
    out = CHANNELS["menu"](ctx, new, {})
    assert out.ok
    pressed = [p["ref"] for m, p in helper.calls if m == "ax.perform"]
    assert pressed == ["g9.6"], pressed


def test_an_action_the_app_will_not_complete_is_done_with_the_pointer(tmp_path):
    """Finder answered AXOpen on a file icon with "attribute unsupported" and AXShowMenu with "cannot
    complete", several times in one run. A person double-clicks the icon."""
    from macwork.act import CHANNELS

    class Refusing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform" and p.get("action") in ("AXOpen", "AXShowMenu"):
                raise HelperError("ax_error", f"{p['action']} failed on {p['ref']}: AXError -25205")
            return super().call(method, timeout, **p)

    h = Refusing()
    c = Ctx(cfg(tmp_path), h, app={"pid": 42, "name": "Finder"}, goal="", inputs={}, task="t", gate=None, cache={})
    icon = Affordance("w9", "window", "press", "open image 「note.txt」", {"ref": "g2.9", "pid": 42, "action": "AXOpen", "frame": [100, 200, 64, 64]})
    assert CHANNELS["window"](c, icon, {}).ok
    clicks = h.did("input.click")
    assert clicks and clicks[-1]["count"] == 2 and (clicks[-1]["x"], clicks[-1]["y"]) == (132, 232)
    menu = Affordance("w10", "window", "press", "open the context menu of image 「note.txt」", {"ref": "g2.9", "pid": 42, "action": "AXShowMenu", "frame": [100, 200, 64, 64]})
    assert CHANNELS["window"](c, menu, {}).ok and h.did("input.click")[-1]["button"] == "right"
    bare = Affordance("w11", "window", "press", "button 「x」", {"ref": "g2.1", "pid": 42, "action": "AXOpen"})
    import pytest
    with pytest.raises(HelperError):
        CHANNELS["window"](c, bare, {})          # no frame: nothing to point at, and the refusal stands


def test_a_suggestion_taken_is_a_suggestion_gone(tmp_path):
    """A key suggestion is labelled with the menu item it turns out to be, so matching on the bare suggestion
    never removed it: a real task pressed cmd+shift+g on four steps out of eight."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("go somewhere", {}, None)
    task.tries = [{"keys": "cmd+shift+g"}, {"type": "hello"}]
    offered = eng._suggested(task)
    chosen = offered[0]
    chosen.label += " (menu Go ▸ Go to Folder…)"      # what the loop appends when the combo has a menu item
    eng._perform(task, eng._step_context(task), None, chosen, lambda m: None)
    assert task.tries == [{"type": "hello"}], task.tries


def test_switching_to_an_app_is_verified_and_escalated(tmp_path):
    """The switch every cross-app task hinges on was asked once and assumed: the step was recorded as taken
    while the app it left was still in front."""
    from macwork.act import CHANNELS

    class Slow(FakeHelper):
        """Activation takes; the front app changes only after a second ask (LaunchServices)."""
        def __init__(self):
            super().__init__()
            self.front = 42
            self.asked = 0

        def call(self, method, timeout=30.0, **p):
            if method == "apps.frontmost":
                return {"app": {"pid": self.front, "name": "TextEdit" if self.front == 42 else "Finder"}}
            if method == "apps.activate":
                self.asked += 1
                return {"ok": True}
            return super().call(method, timeout, **p)

    h = Slow()
    c = Ctx(cfg(tmp_path, config={"engine": {"activate_wait_s": 0.2}}), h, app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t",
            gate=None, cache={}, running=h.call("apps.running"))
    finder = Affordance("a1", "app", "activate", "switch to app Finder", {"pid": 7, "name": "Finder", "bundle_id": "com.apple.finder", "path": "/System/Library/CoreServices/Finder.app"})
    import macwork.act as act
    calls = []
    original = act.subprocess.run

    def fake_open(cmd, **kw):
        calls.append(cmd)
        h.front = 7                     # LaunchServices did what Accessibility could not
        class R: returncode, stderr, stdout = 0, "", ""
        return R()
    act.subprocess.run = fake_open
    try:
        out = CHANNELS["app"](c, finder, {})
    finally:
        act.subprocess.run = original
    assert out.ok and h.asked == 1 and calls and calls[0][:2] == ["open", "-a"]

    h.front = 42
    act.subprocess.run = lambda cmd, **kw: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    try:
        out = CHANNELS["app"](c, finder, {})
    finally:
        act.subprocess.run = original
    assert not out.ok and "front" in out.error, "nothing came forward: the step says so instead of pretending"
