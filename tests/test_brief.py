"""What the planner is told can be done: each place its own share of the brief.

It was the first 40 labels of the options, the window's first: after c632ed9 made it 40, not one of the 97 plan and
replan prompts in this Mac's audit held an item of the app's own menus, while 1,145 of their 3,742 labels were the
Apple menu's (965 its Recent Items), and 48 held no menu item at all. Now the apps the goal names, the app's own
window, one line per menu and one per other place each have a share, sized by planner.brief.
"""

import json
import time

from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import observe
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

def menubar():
    """An Apple menu with 30 Recent Items and a Log Out key, a File menu of 20 items of which two have a key
    equivalent, and a View menu with a checked item."""
    nodes = [{"ref": "b0", "role": "AXMenuBar"},
             {"ref": "a1", "role": "AXMenuBarItem", "title": "Apple", "parent": "b0"}, {"ref": "a2", "role": "AXMenu", "parent": "a1"},
             {"ref": "a3", "role": "AXMenuItem", "title": "About This Mac", "parent": "a2"},
             {"ref": "a4", "role": "AXMenuItem", "title": "Recent Items", "parent": "a2"}, {"ref": "a5", "role": "AXMenu", "parent": "a4"}]
    nodes += [{"ref": f"r{i}", "role": "AXMenuItem", "title": f"Document {i}", "ident": "_recentItemRequested:", "parent": "a5"}
              for i in range(30)]
    nodes += [{"ref": "a6", "role": "AXMenuItem", "title": "Log Out", "cmd": {"char": "Q", "mods": 1}, "parent": "a2"},
              {"ref": "f1", "role": "AXMenuBarItem", "title": "File", "parent": "b0"}, {"ref": "f2", "role": "AXMenu", "parent": "f1"}]
    nodes += [{"ref": f"f{i + 10}", "role": "AXMenuItem", "title": f"Plain Item {i}", "parent": "f2"} for i in range(18)]
    nodes += [{"ref": "f8", "role": "AXMenuItem", "title": "New Document", "cmd": {"char": "N", "mods": 0}, "parent": "f2"},
              {"ref": "f9", "role": "AXMenuItem", "title": "Save As...", "cmd": {"char": "S", "mods": 1}, "parent": "f2"},
              {"ref": "v1", "role": "AXMenuBarItem", "title": "View", "parent": "b0"}, {"ref": "v2", "role": "AXMenu", "parent": "v1"},
              {"ref": "v3", "role": "AXMenuItem", "title": "Basic", "parent": "v2"},
              {"ref": "v4", "role": "AXMenuItem", "title": "Scientific", "mark": "✓", "cmd": {"char": "2", "mods": 0}, "parent": "v2"}]
    return {"nodes": nodes, "ms": 1}


class Menus(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "menubar":
            self.calls.append((method, p))
            return menubar()
        return super().call(method, timeout, **p)


class Window(FakeHelper):
    """A window of twelve rows of one list and a keypad of ten buttons."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "focused_window":
            self.calls.append((method, p))
            nodes = [{"ref": "w0", "role": "AXWindow", "title": "Files"}, {"ref": "l0", "role": "AXOutline", "desc": "files", "parent": "w0"}]
            nodes += [{"ref": f"row{i}", "role": "AXRow", "rdesc": "row", "title": f"report {i}.txt", "parent": "l0"} for i in range(12)]
            nodes += [{"ref": f"k{i}", "role": "AXButton", "rdesc": "button", "title": str(i), "actions": ["AXPress"], "parent": "w0"}
                      for i in range(10)]
            return {"nodes": nodes, "ms": 1}
        return super().call(method, timeout, **p)


def brief_of(tmp_path, helper, goal="make a new document", affs=None):
    eng = Engine(cfg(tmp_path), helper=helper, decider=ScriptedDecider([]))
    task = eng._new_task(goal, {}, None)
    ctx = eng._step_context(task)
    obs = observe(ctx)
    return eng._brief(task, ctx, obs, affs)


def test_each_menu_has_its_share_with_keyed_items_first_and_a_long_submenu_is_one_entry(tmp_path):
    brief = brief_of(tmp_path, Menus())
    assert "menus" in brief, "the planner is told no menu of the app"
    menus = brief["menus"]
    assert list(menus) == ["Apple", "File", "View"], "one line per menu, in the menu bar's order"
    assert menus["Apple"] == "Log Out (⇧⌘Q) · About This Mac · Recent Items ▸ … (30: Document 0, Document 1, …)", menus["Apple"]
    file = menus["File"].split(" · ")
    assert file[:2] == ["New Document (⌘N)", "Save As... (⇧⌘S)"], "the items with a key equivalent come first"
    assert len(file) == 17 + 1 and file[-1] == "(+3 more)", file
    assert menus["View"] == "Scientific ✓ (⌘2) · Basic"
    assert "actions_available" not in brief


def test_a_run_of_content_rows_is_one_entry_but_a_keypad_is_listed(tmp_path):
    """A list's rows are its content, and twelve of them are one entry that says how many; a keypad's ten buttons
    are the app's own controls, each of them one. A move naming the entry as the brief wrote it names the row."""
    from macwork.consult import find_named

    eng = Engine(cfg(tmp_path), helper=Window(), decider=ScriptedDecider([]))
    task = eng._new_task("open the first report", {}, None)
    ctx = eng._step_context(task)
    obs = observe(ctx)
    shown = eng._brief(task, ctx, obs).get("on_screen") or []
    assert "select row 「report 0.txt」 (and 11 more like it)" in shown, shown
    assert not [x for x in shown if "report 1.txt" in x]
    assert [x for x in shown if x.startswith("button")] == [f"button 「{i}」" for i in range(10)], shown
    assert [a.label for a in find_named(obs.affordances, "select row 「report 0.txt」 (and 11 more like it)")] == ["select row 「report 0.txt」"]


def test_a_window_read_only_by_sight_still_has_on_screen_entries(tmp_path):
    """What is read off the screen names no process — vision's click targets, the wheel, dragging by name — and in
    a canvas or a Qt window it is all there is. A prompt from another process is not the app's window, and the
    planner's own moves are its own already."""
    affs = [Affordance("o1", "pointer", "click", "click the text 「Total」 at bottom", {"x": 10, "y": 10, "frame": [0, 0, 20, 20]}),
            Affordance("o2", "pointer", "click", "click the empty place in the row of 「Total」, in line with 「Amount」", {"x": 30, "y": 10}),
            Affordance("wdown", "pointer", "scroll", "scroll down in this window with the wheel", {"window_frame": [0, 0, 800, 600], "dy": -5}),
            Affordance("c1", "window", "press", "button 「Allow」", {"ref": "x", "pid": 99}, context="a prompt from Agent in front of the app"),
            Affordance("try0", "pointer", "drag", "drag 「a」 onto 「b」 (suggested by the planner)", {"x1": 1, "y1": 1, "x2": 9, "y2": 9, "try": 0})]
    brief = brief_of(tmp_path, FakeHelper(), affs=affs)
    assert brief.get("on_screen") == ["click the text 「Total」 at bottom", "click the empty place in the row of 「Total」, in line with 「Amount」",
                                  "scroll down in this window with the wheel"], brief.get("on_screen")
    assert brief["elsewhere"] == ["a prompt from Agent in front of the app: button 「Allow」"], brief.get("elsewhere")


class Installed(FakeHelper):
    """A Mac with Dictionary installed."""

    def call(self, method, timeout=30.0, **p):
        if method == "apps.installed":
            self.calls.append((method, p))
            return [{"name": "Dictionary", "file": "Dictionary", "path": "/System/Applications/Dictionary.app", "bundle_id": "com.apple.Dictionary"}]
        return super().call(method, timeout, **p)


def test_other_processes_and_services_are_a_line_each(tmp_path):
    """Other processes with controls on screen are a line each and forty Services one line, which names the Service
    of the app the goal names: planner.brief.elsewhere lines in all (3), so two of the three processes, and the
    third counted."""
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["apps", "menu", "window", "keys", "services"]}}), helper=Installed(),
                 decider=ScriptedDecider([]))
    eng.cache["services.all"] = (time.monotonic(), [{"name": f"Service {i}", "app": f"App {i}", "sends": []} for i in range(39)]
                                 + [{"name": "Look Up in Dictionary", "app": "Dictionary", "sends": ["NSStringPboardType"]}])
    task = eng._new_task("look serendipity up in Dictionary", {}, None)
    ctx = eng._step_context(task)
    ctx.running = ctx.running + [{"pid": 100, "name": "Wi-Fi Menu"}, {"pid": 200, "name": "Agent"}, {"pid": 300, "name": "Clock"}]
    obs = observe(ctx)
    elsewhere = [Affordance(f"e{i}", "window", "press", f"menu item 「Network {i}」", {"pid": 100}) for i in range(6)]
    elsewhere += [Affordance("c0", "window", "press", "button 「Allow」", {"pid": 200}), Affordance("t0", "window", "press", "button 「Now」", {"pid": 300})]
    brief = eng._brief(task, ctx, obs, obs.affordances + elsewhere)
    assert brief.get("apps_the_goal_names") == ["open app Dictionary"]
    assert list(brief).index("apps_the_goal_names") < list(brief).index("on_screen"), "the apps the goal names come first"
    assert brief["elsewhere"] == ["Wi-Fi Menu (6: menu item 「Network 0」, menu item 「Network 1」, …)",
                                  "Agent: button 「Allow」 (+1 more processes)",
                                  "Services on this Mac: 40; of the apps the goal names: 「Look Up in Dictionary」 — a Service of Dictionary on this Mac"], \
        brief["elsewhere"]
    assert "Service 3" not in json.dumps(brief)
