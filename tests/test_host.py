"""Where a task begins, and the app the engine runs under.

A task that names no app began in whatever app was in front. In this Mac's audit 17 tasks took their first
look in the terminal these runs are started from, which offered its own window as options and sent its text
in looks and plans; and in an eval run a nameless task began wherever the task before it had left the screen.
Beginning in no app instead takes away the one thing that kept the host's keys out of reach — the host rule
charged a key to the working app, and with none it charged it to nobody.
"""

import logging
import os
import subprocess
import time
from types import SimpleNamespace

from macwork.act import get_channel
from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import PROVIDERS, Ctx
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

TERMINAL = {"pid": 50, "name": "Terminal", "bundle_id": "com.apple.Terminal", "path": "/System/Applications/Utilities/Terminal.app"}


def test_the_host_is_found_from_the_bundle_it_was_launched_from_when_its_terminal_is_gone(tmp_path, monkeypatch, caplog):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    # reparented to launchd: nothing above this process is an app any more
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{os.getpid()} 1\n", stderr="", returncode=0))
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    with caplog.at_level(logging.INFO, logger="macwork.policy"):
        assert eng._host_bundles() == {"com.apple.Terminal"}
        eng._host_bundles()
    said = [r.getMessage() for r in caplog.records if "never driven" in r.getMessage()]
    assert said == ["the engine runs under com.apple.Terminal: never driven"], "which apps are off limits is said, once"


def test_a_task_that_names_no_app_never_begins_in_the_app_running_the_engine(tmp_path):
    running = FakeHelper().call("apps.running")
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert eng._resolve_app(None, running)["bundle_id"] == "com.apple.TextEdit", "the app in front, when it is not the host"

    d = ScriptedDecider([{"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng.cache["host.bundles"] = {"com.apple.TextEdit"}          # the app in front is the one running the engine
    assert eng._resolve_app(None, running) is None
    eng.do("what chip does this Mac have?")
    assert d.seen[0][0]["app"] is None, "the first look was taken in the host"
    assert eng._resolve_app("TextEdit", running)["pid"] == 42, "an app the caller names is still that app"


class Launches(FakeHelper):
    """Calculator starts, and comes to the front, once the task opens it."""

    def __init__(self):
        super().__init__()
        self.launched = False

    def call(self, method, timeout=30.0, **p):
        calc = {"pid": 77, "name": "Calculator", "bundle_id": "com.apple.calculator", "path": "/System/Applications/Calculator.app"}
        if method == "apps.running" and self.launched:
            self.calls.append((method, p))
            return super().call(method, timeout, **p) + [calc]
        if method == "apps.frontmost" and self.launched:
            self.calls.append((method, p))
            return {"app": calc}
        return super().call(method, timeout, **p)


def test_an_eval_begins_a_task_that_names_no_app_in_none(tmp_path):
    c = cfg(tmp_path, config={"engine": {"start_in_front_app": False},
                              "observe": {"providers": ["apps", "menu", "window", "keys", "clipboard"]}})
    h = Launches()
    looks = []

    def at(pick, then=None):
        def step(state, questions):
            looks.append((state["app"], any("press the escape key" in v for v in questions["action"]["criteria"].values())))
            if then:
                then()
            return {"pick": pick}
        return step

    eng = Engine(c, helper=h, decider=ScriptedDecider([at("open app Calculator", lambda: setattr(h, "launched", True)),
                                                       at("press the escape key"), at("done")]))
    res = eng.do("work out 17 times 23")
    assert res["status"] == "done"
    assert looks == [(None, False), ("Calculator", True), ("Calculator", True)], looks
    assert h.did("input.key") == [{"combo": "escape"}], "in the app it opened, keys work as before"

    # …and once it has done something that says nothing about where it went, a look follows the app in front
    h, looks = FakeHelper(), []
    eng = Engine(c, helper=h, decider=ScriptedDecider([at("put the given text on the clipboard"), at("done")]))
    eng.do("put hello on the clipboard", {"text": "hello"})
    assert [app for app, _ in looks] == [None, "TextEdit"], looks

    from macwork import evals
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    suite = tmp_path / "s.yaml"
    suite.write_text("fresh: false\ntasks: []\n", encoding="utf-8")
    evals.run_suite(engine, suite, out_dir=None)
    assert engine.cfg.get("engine.start_in_front_app") is False, "an eval run begins nameless tasks in no app"


def test_with_the_host_in_front_and_no_app_nothing_is_offered_that_types_or_presses(tmp_path):
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["apps", "menu", "window", "keys", "typing"]}}),
                 helper=FakeHelper(), decider=ScriptedDecider([]))
    eng.cache["host.bundles"] = {"com.apple.TextEdit"}          # the app in front runs the engine
    drives = ("keys", "hold", "pointer")

    seen = eng.observe()
    assert seen["app"] is None and not [a for a in seen["affordances"] if a["channel"] in drives]

    task = eng._new_task("write hello", {}, None)
    task.tries = [{"type": "hello"}, {"keys": "cmd+v"}]          # the planner's typing and keys, offered as options
    look = eng._look(task, eng._step_context(task))
    assert look.ctx.app is None and not [a.label for a in look.affs if a.channel in drives], "the loop offered a key"

    key = Affordance("k1", "keys", "key", "press the return key", {"combo": "return"})
    assert eng._denied(key, None), "a key with no app to go to was not charged to anything"
    ctx = Ctx(eng.cfg, eng.helper, app=None)
    out = get_channel("keys")(ctx, key, {})
    held = get_channel("hold")(ctx, Affordance("h1", "hold", "hold", "hold w", {"keys": ["w"], "ms": 100}), {})
    assert not out.ok and not held.ok and "no app is being worked in" in (out.error or "")
    assert not eng.helper.did("input.key") and not eng.helper.did("input.hold"), "a key was sent to whatever is in front"
    click = get_channel("pointer")(ctx, Affordance("p1", "pointer", "click", "click at a point", {"x": 40.0, "y": 60.0}), {})
    assert not click.ok and not eng.helper.did("input.click"), "a click went to whatever is in front"


class WithTerminal(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "apps.running":
            self.calls.append((method, p))
            return super().call(method, timeout, **p) + [TERMINAL]
        return super().call(method, timeout, **p)


class Schemes:
    """Stands in for AppModels: each app's own URL schemes, as its Info.plist declares them."""

    def get(self, app):
        return {"url_schemes": {"com.apple.Terminal": ["ssh", "x-man-page"], "com.apple.TextEdit": ["x-text"]}
                .get((app or {}).get("bundle_id"), [])}


def test_the_hosts_own_url_schemes_are_denied_and_another_apps_are_not(tmp_path):
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["schemes"]}}), helper=WithTerminal(), decider=ScriptedDecider([]))
    eng.cache["host.bundles"] = {"com.apple.Terminal"}
    eng.cache["appmodels"] = Schemes()
    obs = SimpleNamespace(affordances=[])
    PROVIDERS["schemes"](Ctx(eng.cfg, eng.helper, app=TERMINAL, cache=eng.cache), obs)
    assert [a.label for a in obs.affordances] == ["open a 「ssh:」 link with Terminal", "open a 「x-man-page:」 link with Terminal"]
    assert all(eng._denied(a, TERMINAL) for a in obs.affordances), "a link that runs a command in the host"
    assert eng.observe(app="Terminal")["affordances"] == []
    assert [a["label"] for a in eng.observe(app="TextEdit")["affordances"]] == ["open a 「x-text:」 link with TextEdit"]


class HostNamed(WithTerminal):
    """Terminal is running (pid 50) and installed, and TextEdit is in front."""

    def call(self, method, timeout=30.0, **p):
        if method == "apps.installed":
            return super().call(method, timeout, **p) + [{k: TERMINAL[k] for k in ("name", "path", "bundle_id")} | {"file": "Terminal"}]
        return super().call(method, timeout, **p)


def test_a_task_that_lands_in_the_host_works_in_no_app_and_never_opens_it(tmp_path):
    """A task whose working app is the host — the caller named it, or an open landed in it — works in no app: the
    host's window is not read into a look, where its text would go out in the state and in plans, and the host
    is not opened as the app to work in."""
    h = HostNamed()
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    eng.cache["host.bundles"] = {"com.apple.Terminal"}

    task = eng._new_task("list the files in my home folder", {}, "Terminal")
    ctx = eng._step_context(task)
    assert ctx.app is None, "the task works in the terminal running the engine"
    assert task.held is None and not task.pace.launched, "the host was opened as the app to work in"
    look = eng._look(task, ctx)
    assert look.state["app"] is None
    assert not [p for m, p in h.calls if m == "ax.snapshot" and p.get("pid") == TERMINAL["pid"]], "the host's window was read"

    landed = eng._new_task("show the log", {}, None)
    landed.target = dict(TERMINAL)                               # where the last step's open ended up
    assert eng._step_context(landed).app is None


# --- what a status item touches ------------------------------------------------------------------------

AGENT = {"pid": 90, "name": "Menu Clock", "bundle_id": "com.example.menuclock", "path": "/Applications/Menu Clock.app"}


class StatusItems(FakeHelper):
    """Two processes with an item in the menu bar, each with its menu down: TextEdit (pid 42) and an agent with no
    Dock icon (pid 90), which only the list of every process names."""

    menus = {42: ("TextEdit Status", "New Chat Window"), 90: ("Menu Clock", "Set an Alarm")}

    def call(self, method, timeout=30.0, **p):
        if method == "apps.running" and p.get("all"):
            return super().call(method, timeout, **p) + [AGENT]
        if method == "ax.snapshot" and p.get("scope") == "extras_menubar":
            self.calls.append((method, p))
            item, entry = self.menus[p["pid"]]
            ref = f"x{p['pid']}"
            return {"nodes": [{"ref": f"{ref}.0", "role": "AXMenuBar", "depth": 0},
                              {"ref": f"{ref}.1", "role": "AXMenuBarItem", "title": item, "actions": ["AXPress"], "parent": f"{ref}.0"},
                              {"ref": f"{ref}.2", "role": "AXMenuItem", "title": entry, "actions": ["AXPress"], "parent": f"{ref}.1"}]}
        return super().call(method, timeout, **p)


def _status_items(tmp_path, host, decider=None, **policy):
    c = cfg(tmp_path, config={"observe": {"providers": ["menubar_extras", "keys"]}}, **({"policy": policy} if policy else {}))
    h = StatusItems()
    eng = Engine(c, helper=h, decider=decider or ScriptedDecider([]))
    eng.cache["host.bundles"] = {host}
    eng.cache["menubar.owners"] = (time.monotonic(), [{"pid": 42, "name": "TextEdit", "items": 1},
                                                      {"pid": 90, "name": "Menu Clock", "items": 1}])
    return eng, h


def test_a_status_item_is_judged_by_the_process_that_owns_it(tmp_path):
    """The menu bar's extras are offered for their owner's process by its pid alone. With no app being worked in,
    an action that named a pid was let through as "judged by its own process", and nothing judged it: the host,
    deny.bundle_ids and allow.bundle_ids rules read a bundle the action names or the working app's, and there was
    neither. With the host in front, the host's own status menu was offered, and pressed."""
    eng, _ = _status_items(tmp_path, "com.apple.TextEdit")        # the app in front runs the engine
    seen = eng.observe()
    labels = [a["label"] for a in seen["affordances"]]
    assert seen["app"] is None
    assert not [x for x in labels if "TextEdit Status" in x or "New Chat Window" in x], labels
    assert [x for x in labels if "Set an Alarm" in x], "another process's status item is offered as its own"

    eng, _ = _status_items(tmp_path, "com.apple.TextEdit", deny={"bundle_ids": ["com.example.menuclock"]})
    labels = [a["label"] for a in eng.observe()["affordances"]]
    assert not [x for x in labels if "Menu Clock" in x or "Set an Alarm" in x], "an app the policy says never to touch"

    # …and while working in another app: a status item was charged to the app being worked in
    eng, _ = _status_items(tmp_path, "com.example.menuclock")     # a client with no Dock icon runs the engine
    seen = eng.observe()
    labels = [a["label"] for a in seen["affordances"]]
    assert seen["app"]["bundle_id"] == "com.apple.TextEdit"
    assert not [x for x in labels if "Set an Alarm" in x], "the host's status menu, charged to TextEdit"
    assert [x for x in labels if "New Chat Window" in x], "the working app's own status menu is still its own"

    def pick(state, questions):
        crit = questions["action"]["criteria"]
        return {"pick": next((k for k, v in crit.items() if "New Chat Window" in v), "done")}
    eng, h = _status_items(tmp_path, "com.apple.TextEdit", decider=ScriptedDecider([pick, {"pick": "done"}]))
    eng.do("tidy up")
    assert not [p for p in h.did("ax.perform") if str(p.get("ref", "")).startswith("x42")], "the host's status menu was pressed"


def test_with_no_app_an_action_is_judged_by_the_app_or_process_it_names(tmp_path):
    eng, _ = _status_items(tmp_path, "com.apple.TextEdit")

    def denied(**target):
        return eng._denied(Affordance("e1", "window", "press", "press it", {"ref": "r1", **target}), None)
    assert not denied(pid=7), "Finder's own control is Finder's"
    assert not denied(pid=90), "an agent's, found among every process"
    assert not denied(bundle_id="com.apple.finder")
    assert denied(pid=42), "the host's own, by its process"
    assert denied(bundle_id="com.apple.TextEdit")
    assert denied(pid=4242), "a process the Mac does not list is charged to nobody, and refused"
