"""Leaving the app a task is working in, and the apps a goal names.

Found in this Mac's audit log, not by reading the code. A cross-app task — look a word up in Dictionary, write
its meaning into a new TextEdit document — was run eight times over three days, and six of those runs went to the
person's own browser; one other went to the person's terminal. The planner had been shown the running apps (the person's browser and terminal among
them) and never told that the word the goal used for Dictionary is the name of an app on this Mac, so it read
the goal as "look it up on a dictionary website". The engine then left by two doors the question "does leaving
serve the goal" was never asked at: the Apple menu's Recent Items, which is a menu item, and a link the planner
suggested, which the file channel opens in whatever handles links. The planner also wrote routes through a
terminal — a chip model, a file count, a deletion — each of which the floor stops to ask the user about.
"""

import json

from macwork import act
from macwork.config import Config
from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import Ctx, apps_named, arrange, observe
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend

FILLERS = [{"name": f"Filler {i}", "file": f"Filler {i}", "path": f"/Applications/Filler {i}.app",
            "bundle_id": f"test.filler{i}"} for i in range(70)]
ELSEWHERE = [
    {"name": "Dictionary", "file": "Dictionary", "path": "/System/Applications/Dictionary.app", "bundle_id": "com.apple.Dictionary"},
    {"name": "Google Chrome", "file": "Google Chrome", "path": "/Applications/Google Chrome.app", "bundle_id": "com.google.Chrome"},
    {"name": "Terminal", "file": "Terminal", "path": "/System/Applications/Utilities/Terminal.app", "bundle_id": "com.apple.Terminal"},
]
# The Apple menu's Recent Items: every entry shares one identifier, so each is told apart by its label
RECENT = {"nodes": [
    {"ref": "r.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "r.1", "role": "AXMenuBarItem", "title": "Apple", "parent": "r.0"},
    {"ref": "r.2", "role": "AXMenu", "parent": "r.1"},
    {"ref": "r.3", "role": "AXMenuItem", "title": "Recent Items", "parent": "r.2"},
    {"ref": "r.4", "role": "AXMenu", "parent": "r.3"},
    {"ref": "r.5", "role": "AXMenuItem", "title": "Google Chrome", "ident": "_recentItemRequested:", "parent": "r.4"},
    {"ref": "r.6", "role": "AXMenuItem", "title": "Calculator", "ident": "_recentItemRequested:", "parent": "r.4"},
    {"ref": "r.7", "role": "AXMenuBarItem", "title": "File", "parent": "r.0"},
    {"ref": "r.8", "role": "AXMenu", "parent": "r.7"},
    {"ref": "r.9", "role": "AXMenuItem", "title": "New Document", "cmd": {"char": "N", "mods": 0}, "parent": "r.8"},
], "ms": 1}


class Mac(FakeHelper):
    """TextEdit in front, more apps installed than the decider is shown one by one, and a menu bar with the
    Apple menu's Recent Items in it."""

    def call(self, method, timeout=30.0, **p):
        if method == "apps.installed":
            self.calls.append((method, p))
            return FILLERS + ELSEWHERE
        if method == "ax.snapshot" and p.get("scope") == "menubar":
            self.calls.append((method, p))
            return RECENT
        return super().call(method, timeout, **p)


def test_a_name_counts_where_it_stands_as_a_name():
    apps = [{"name": n, "file": n, "path": f"/Applications/{n}.app"} for n in
            ("Maps", "News", "App", "App Store", "Dictionary", "TextEdit", "Safari")]

    def named(text):
        return [a["name"] for a in apps_named(text, apps)]

    assert named("look serendipity up in Dictionary and write it into a new TextEdit document") == ["Dictionary", "TextEdit"]
    assert named("open textedit") == ["TextEdit"], "a person does not capitalise app names"
    assert named("open the roadmaps folder") == [], "a name run into a longer word is that word"
    assert named("open ~/news.html in Safari") == ["Safari"], "a file's name is not the app of the same name"
    assert named("sign in to the App Store") == ["App Store"], "a name inside a longer one is the longer one"


def test_an_app_the_goal_names_is_offered_in_plain_sight_and_to_the_planner(tmp_path):
    eng = Engine(cfg(tmp_path), helper=Mac(), decider=ScriptedDecider([]))
    goal = "look serendipity up in Dictionary"
    ctx = eng._ctx(goal, {}, None, "t")
    obs = observe(ctx)

    flat, folded = arrange(obs.affordances, 199, set(), fold_over=60)
    assert "open app Dictionary" in [a.label for a in flat], "not one of 73 behind a group the decider has to open"
    assert any("open another app" in name for name, _members in folded.values())

    brief = eng._brief(eng._new_task(goal, {}, None), ctx, obs)
    assert brief["actions_available"][0] == "open app Dictionary", "the planner is never shown the other apps"
    assert not any(label.startswith("open app Filler") for label in brief["actions_available"])


def test_which_apps_the_goal_names_is_part_of_what_the_leaving_question_sees(tmp_path):
    eng = Engine(cfg(tmp_path), helper=Mac(), decider=ScriptedDecider([]))
    task = eng._new_task("look serendipity up in Dictionary, then write it into a TextEdit document", {}, None)
    state = eng._goal_state(task, eng._ctx(task.goal, {}, None, "t"))
    assert state["apps_the_goal_names"] == ["Dictionary", "TextEdit"]    # the goal against the Mac's list, not the screen


def test_what_leaves_the_app_is_judged_by_what_it_does(tmp_path):
    eng = Engine(cfg(tmp_path), helper=Mac(), decider=ScriptedDecider([]))
    ctx = eng._ctx("", {}, None, "t")                       # working in TextEdit
    leaves = lambda channel, verb, target: eng._leaves_for(ctx, Affordance("x", channel, verb, "x", target))   # noqa: E731

    assert leaves("app", "activate", {"name": "Finder", "pid": 7}) == "Finder"
    assert leaves("service", "perform", {"name": "Look Up in Dictionary"}) == "", "a Service hands content to its app"
    assert leaves("file", "open", {"path": "", "url": "https://dictionary.example/serendipity"}) == "", \
        "a link goes to whatever opens links"
    assert leaves("file", "open", {"path": "/tmp/a.txt", "bundle_id": "com.apple.TextEdit"}) is None, \
        "opened with the very app it is working in"
    assert leaves("menu", "press", {"title": "Google Chrome"}) == "Google Chrome", "a Recent Items entry"
    assert leaves("window", "press", {"title": "Dictionary"}) == "Dictionary", "an app's icon in a Finder window"
    assert leaves("menu", "press", {"title": "New Document"}) is None
    assert leaves("menu", "press", {"title": "TextEdit"}) is None, "the app it is in is not somewhere else"


def test_leaving_by_the_recent_items_menu_is_asked_first(tmp_path):
    d = ScriptedDecider([{"pick": "Recent Items ▸ Google Chrome"}, {"pick": "New Document"}, {"pick": "done"}])
    d.serves = 0.05
    res = Engine(cfg(tmp_path), helper=Mac(), decider=d).do("make a new document")

    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    asked = next(s for s, q in d.side if "serves" in q)
    assert "Recent Items ▸ Google Chrome" in asked["action"]
    again = d.seen[1][1]["action"]["criteria"]
    assert not any("Google Chrome" in v for v in again.values()), \
        "declined by what it is: every Recent Items entry has one identifier plus its label, not the label alone"
    assert "menu Apple ▸ Recent Items ▸ Google Chrome" in d.seen[1][0]["not_offered_because_the_goal_never_asked"]

    d2 = ScriptedDecider([{"pick": "Recent Items ▸ Google Chrome"}, {"pick": "done"}])
    res2 = Engine(cfg(tmp_path), helper=Mac(), decider=d2).do("open Google Chrome")
    assert res2["steps"] == ["menu Apple ▸ Recent Items ▸ Google Chrome"], "when the goal wants it, it is taken"


def test_a_link_the_planner_suggests_is_asked_about_like_any_other_way_out(tmp_path, monkeypatch):
    opened = []
    real = act.subprocess.run
    monkeypatch.setattr(act.subprocess, "run", lambda args, *a, **k: opened.append(args) or real(["true"], *a, **k))
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "open the link"},
                         {"pick": "New Document"}, {"pick": "done"}])
    d.serves = 0.05
    eng = Engine(cfg(tmp_path), helper=Mac(), decider=d)
    eng._planner = FakeBackend([{"steps": [{"goal": "look it up", "evidence": "the meaning"}], "inputs": {},
                                 "try": [{"open_url": "https://dictionary.example/serendipity"}], "blocked": ""},
                                {"done": True, "why": "a new document is open"}])
    res = eng.do("make a new document")

    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    assert not any("https://dictionary.example/serendipity" in " ".join(map(str, args)) for args in opened)
    asked = next(s for s, q in d.side if "serves" in q)
    assert "https://dictionary.example/serendipity" in asked["action"]


def test_the_planner_is_told_what_stops_to_ask_the_user(tmp_path):
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=Mac(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "try": [{"keys": "cmd+n"}], "blocked": ""},
                                {"done": True, "why": "a new document is open"}])
    eng.do("make a new document")

    prompt = eng._planner.prompts[0]
    line = next(x for x in prompt.splitlines() if x.startswith("Asks the user first: "))
    told = json.loads(line.split(": ", 1)[1])
    floor = Config.load().policy["confirm"]
    assert floor["categories"]["execute"]["what"] in told, "a command in a terminal is a stop to ask the user"
    assert len(told) == len(floor["categories"]) + 1, "every category, in policy's own words, and the catch-all"
    assert prompt.index("Asks the user first") < prompt.index("Goal:"), "the same on every call: before what is not"


def test_an_option_for_one_kind_of_link_opens_no_other(tmp_path, monkeypatch):
    """「open a things: link with Things」 was judged as what it says; handed an https: link, `open` would have
    brought up the browser, which nobody judged."""
    opened = []
    real = act.subprocess.run
    monkeypatch.setattr(act.subprocess, "run", lambda args, *a, **k: opened.append(args) or real(["true"], *a, **k))
    c = cfg(tmp_path, config={"engine": {"open_front_s": 0.05}})
    ctx = Ctx(c, FakeHelper(), app={"pid": 42, "name": "Things", "bundle_id": "com.culturedcode.ThingsMac"})
    link = Affordance("h0", "file", "open", "open a 「things:」 link with Things", {"path": "", "scheme": "things"})

    refused = act.CHANNELS["file"](ctx, link, {"url": "https://dictionary.example/serendipity"})
    assert not refused.ok and "things:" in refused.error and opened == []

    done = act.CHANNELS["file"](ctx, link, {"url": "THINGS:///add?title=milk"})
    assert done.ok and opened == [["open", "THINGS:///add?title=milk"]], "a scheme is the same scheme in any case"


class Declares:
    """Stands in for AppModels: TextEdit declares a URL scheme of its own in its Info.plist."""

    def get(self, app):
        return {"url_schemes": ["x-text"] if (app or {}).get("bundle_id") == "com.apple.TextEdit" else []}


def test_the_working_apps_own_link_is_asked_about_like_any_other_way_out(tmp_path, monkeypatch):
    """「open a 「x-text:」 link with TextEdit」 is opened with a plain `open`, and LaunchServices hands a link to
    the scheme's handler, which need not be the app that declares it: a browser that is not the default one
    declares https: too. Naming the declaring app on the option, so that the host's own links are charged to
    the host, also made the link count as staying in the app, and whether leaving served the goal went unasked."""
    opened = []
    real = act.subprocess.run
    monkeypatch.setattr(act.subprocess, "run", lambda args, *a, **k: opened.append(args) or real(["true"], *a, **k))
    d = ScriptedDecider([{"pick": "x-text:"}, {"pick": "New Document"}, {"pick": "done"}])
    d.serves = 0.05
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["menu", "schemes"]}}), helper=Mac(), decider=d)
    eng.cache["appmodels"] = Declares()
    res = eng.do("make a new document")

    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"], res
    asked = next(s for s, q in d.side if "serves" in q)
    assert "open a 「x-text:」 link with TextEdit" in asked["action"]
    assert not [args for args in opened if args[0] == "open"], "the link was opened"


class Windowless(Mac):
    """TextEdit in front with no window open: its windows are listed as none, and its focused window is empty."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") in ("windows", "focused_window"):
            self.calls.append((method, p))
            return {"nodes": [], "ms": 1, "truncated": False}
        return super().call(method, timeout, **p)


def test_bringing_back_the_window_of_the_app_in_front_is_not_leaving(tmp_path, monkeypatch):
    """34 of the 125 serves_goal questions in this Mac's audit were about bringing back the main window of the
    app already in front, and one was declined (0.22): Finder, with no window open, was not given one back."""
    opened = []
    real = act.subprocess.run
    monkeypatch.setattr(act.subprocess, "run", lambda args, *a, **k: opened.append(args) or real(["true"], *a, **k))
    c = cfg(tmp_path, config={"observe": {"providers": ["windows", "apps", "keys"]}})
    eng = Engine(c, helper=Windowless(), decider=ScriptedDecider([]))
    ctx = eng._ctx("", {}, None, "t")
    obs = observe(ctx)
    reopen = next(a for a in obs.affordances if a.verb == "reopen")
    assert reopen.label == "bring back the main window of TextEdit"
    assert eng._leaves_for(ctx, reopen) is None
    assert eng._leaves_for(ctx, next(a for a in obs.affordances if a.label == "switch to app Finder")) == "Finder"

    d = ScriptedDecider([{"pick": "bring back the main window"}, {"pick": "done"}])
    d.serves = 0.05
    res = Engine(c, helper=Windowless(), decider=d).do("write a note")
    assert res["steps"] == ["bring back the main window of TextEdit"], res
    assert not [q for _, q in d.side if "serves" in q], "asked whether staying in the app serves the goal"
    assert [args for args in opened if args[0] == "open"] == [["open", "-a", "/System/Applications/TextEdit.app"]]

    # An app in front with no bundle id: a Shortcut has none either, and neither need the app it switches to
    nameless = Ctx(eng.cfg, eng.helper, app={"pid": 9, "name": "a tool with no bundle id"})
    assert eng._leaves_for(nameless, Affordance("s0", "shortcut", "run", "run shortcut 「Make PDF」", {"name": "Make PDF"})) == "Make PDF"
    assert eng._leaves_for(nameless, Affordance("a1", "app", "activate", "switch to app Finder", {"pid": 7, "name": "Finder"})) == "Finder"


def test_an_ellipsis_does_not_make_a_command_an_app(tmp_path):
    """Guard for a proposal left out: dropping a trailing ellipsis before matching app names would read Edit ▸
    Find… as an app called Find (on this Mac, the Find My app's Chinese name is the Find command's without its
    ellipsis, and that command was chosen 11 times)."""
    class WithFind(Mac):
        def call(self, method, timeout=30.0, **p):
            if method == "apps.installed":
                self.calls.append((method, p))
                return FILLERS + ELSEWHERE + [{"name": "Find", "file": "Find", "path": "/Applications/Find.app", "bundle_id": "test.find"}]
            return super().call(method, timeout, **p)

    eng = Engine(cfg(tmp_path), helper=WithFind(), decider=ScriptedDecider([]))
    ctx = eng._ctx("", {}, None, "t")
    assert eng._leaves_for(ctx, Affordance("m1", "menu", "press", "menu Edit ▸ Find…", {"title": "Find…"})) is None
    assert eng._leaves_for(ctx, Affordance("m2", "menu", "press", "menu Window ▸ Find", {"title": "Find"})) == "Find"
