"""What the harness finds on screen, and what it leaves there.

Found in the audit and the harness's own logs on this Mac:

* About This Mac is a window of a process with no Dock icon. The sweep walked the Dock apps only, so the
  window stayed up for a day and later tasks read the serial number off it;
* an input method's candidate panel, and windows that were on no screen at all, were reported as prompts
  "above every window" before 13 of the 14 tasks of one v2 run, and the final sweep pressed Escape at one;
* Escape was pressed at a prompt without anyone checking where the keyboard was;
* every app the run launched was force-quit without ever being asked to quit (3 of 3 in the logs), because
  the polite quit went through a check that needs a bundle id the sweep never had.
"""

from macwork import evals
from macwork.config import Config
from macwork.engine import Engine
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Mac:
    """The window server, the process list and the focus, as the helper reports them."""

    def __init__(self, windows, apps, nodes=None, front=1, focused=1):
        self.windows, self.apps, self.nodes = list(windows), list(apps), dict(nodes or {})
        self.front, self.focused = front, focused
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "screen.windows":
            return [w for w in self.windows if p.get("all") or w.get("on_screen", True)]
        if method == "apps.running":
            return [a for a in self.apps if p.get("all") or a.get("regular", True)]
        if method == "apps.frontmost":
            return {"app": {"pid": self.front}}
        if method == "ax.focused":
            return {"focused": {"role": "AXButton"}, "pid": self.focused}
        if method == "ax.snapshot":
            return {"nodes": self.nodes.get(p.get("pid"), [])}
        if method == "ax.perform":            # a close button pressed closes its window
            closes = {n["ref"]: n.get("closes") for ns in self.nodes.values() for n in ns if n.get("closes")}
            self.windows = [w for w in self.windows if w["id"] != closes.get(p.get("ref"))]
        return {"ok": True}

    def keys(self):
        return [p["combo"] for m, p in self.calls if m == "input.key"]

    def pressed(self):
        return [p["ref"] for m, p in self.calls if m == "ax.perform"]


class Eng:
    def __init__(self, mac):
        self.helper = mac
        self.cfg = Config.load(overrides={"config": {"engine": {"activate_wait_s": 0.05}}})
        self.tasks = {}

    def _host_bundles(self):
        return set()


TEXTEDIT = {"pid": 1, "name": "TextEdit", "bundle_id": "com.apple.TextEdit", "regular": True}
SYSINFO = {"pid": 88, "name": "System Information", "bundle_id": "com.apple.SystemProfiler", "regular": False}
AUTOFILL = {"pid": 90, "name": "AutoFill", "bundle_id": "com.apple.SafariPlatformSupport.Helper", "regular": False}
NOTIFIER = {"pid": 77, "name": "UserNotificationCenter", "bundle_id": "com.apple.UserNotificationCenter", "regular": False}
PINYIN = {"pid": 806, "name": "Pinyin Input", "bundle_id": "com.example.inputmethod.pinyin", "regular": False}

DOC = {"pid": 1, "id": 10, "owner": "TextEdit", "layer": 0, "alpha": 1, "frame": [0, 0, 800, 600], "on_screen": True}
ABOUT = {"pid": 88, "id": 70, "owner": "System Information", "layer": 0, "alpha": 1, "frame": [300, 200, 560, 420],
         "on_screen": True, "regular": False}
PROMPT = {"pid": 77, "id": 90, "owner": "UserNotificationCenter", "layer": 8, "alpha": 1, "frame": [400, 300, 420, 200],
          "on_screen": True, "title": "Allow the app to find devices on local networks?"}
ABOUT_NODES = [{"ref": "a1", "role": "AXWindow", "title": "About This Mac", "frame": [300, 200, 560, 420]},
               {"ref": "a2", "role": "AXButton", "subrole": "AXCloseButton", "parent": "a1", "closes": 70}]


def test_a_window_whose_owner_has_no_dock_icon_is_a_leftover():
    eng = Eng(Mac([DOC, ABOUT], [TEXTEDIT, SYSINFO]))
    assert evals._leftovers(eng, ({1: {10}}, {1})) == [
        {"app": "System Information", "pid": 88, "bundle_id": "com.apple.SystemProfiler", "new_app": False, "windows": 1,
         "ids": [70], "dockless": True}]


def test_the_sweep_closes_that_window_by_its_own_close_button(monkeypatch):
    mac = Mac([DOC, ABOUT], [TEXTEDIT, SYSINFO], nodes={88: ABOUT_NODES})
    asked = []
    monkeypatch.setattr(evals, "_discard_prompt", lambda engine, pid: asked.append(pid) or False)
    left = evals.sweep(Eng(mac), ({1: {10}}, {1}))
    assert mac.pressed() == ["a2"] and left == []
    assert mac.keys() == [], "a system agent's window is closed by its own button or not at all: never a key"
    assert 88 not in asked, "no button the decider picks is pressed in a system agent's window"


def test_an_off_screen_window_of_a_process_with_no_dock_icon_is_not_a_leftover():
    """This Mac keeps such windows right now: AutoFill's and loginwindow's, at the ordinary level, on no screen."""
    kept = {"pid": 90, "id": 71, "owner": "AutoFill", "layer": 0, "alpha": 1, "frame": [0, 0, 312, 237],
            "on_screen": False, "regular": False}
    eng = Eng(Mac([DOC, ABOUT, kept], [TEXTEDIT, SYSINFO, AUTOFILL]))
    assert evals._real_windows(eng) == {1: {10}, 88: {70}}
    assert [x["pid"] for x in evals._leftovers(eng, ({1: {10}}, {1}))] == [88]


def test_a_window_that_is_not_on_screen_is_not_a_prompt():
    hidden = {"pid": 91, "id": 92, "owner": "Launcher", "layer": 3, "alpha": 1, "frame": [0, 900, 420, 240], "on_screen": False}
    eng = Eng(Mac([DOC, PROMPT, hidden], [TEXTEDIT, NOTIFIER]))
    assert [d["ids"] for d in evals._dialogs_above(eng, {})] == [[90]]


def test_an_input_methods_own_panel_is_neither_a_prompt_nor_a_leftover():
    panel = {"pid": 806, "id": 95, "owner": "Pinyin Input", "layer": 3, "alpha": 1, "frame": [0, 900, 420, 240],
             "on_screen": True, "input_method": True}
    settings = {"pid": 806, "id": 96, "owner": "Pinyin Input", "layer": 0, "alpha": 1, "frame": [200, 200, 600, 400],
                "on_screen": True, "input_method": True}
    eng = Eng(Mac([DOC, panel, settings], [TEXTEDIT, PINYIN]))
    assert evals._dialogs_above(eng, {}) == [], "an input method's candidates are no prompt"
    left = evals._leftovers(eng, ({1: {10}}, {1}))
    assert [(x["pid"], x.get("ids"), x.get("dialog")) for x in left] == [(806, [96], None)], \
        "its settings window is a window like any other"


def test_a_regular_apps_floating_panel_is_still_closed_by_its_close_button(monkeypatch):
    """A regular app's floating panel goes with that app's windows and is closed by its own button, even when
    the app also left a window at the ordinary level; one of its panels that is on no screen is not reached
    for at all."""
    doc2 = {"pid": 1, "id": 11, "owner": "TextEdit", "layer": 0, "alpha": 1, "frame": [100, 100, 600, 500], "on_screen": True}
    fonts = {"pid": 1, "id": 12, "owner": "TextEdit", "layer": 3, "alpha": 1, "frame": [900, 100, 300, 400], "on_screen": True}
    gone = {"pid": 1, "id": 13, "owner": "TextEdit", "layer": 3, "alpha": 1, "frame": [900, 600, 300, 400], "on_screen": False}
    nodes = {1: [{"ref": "d1", "role": "AXWindow", "frame": doc2["frame"]},
                 {"ref": "d2", "role": "AXButton", "subrole": "AXCloseButton", "parent": "d1", "closes": 11},
                 {"ref": "f1", "role": "AXWindow", "subrole": "AXFloatingWindow", "frame": fonts["frame"]},
                 {"ref": "f2", "role": "AXButton", "subrole": "AXCloseButton", "parent": "f1", "closes": 12}]}
    mac = Mac([DOC, doc2, fonts, gone], [TEXTEDIT], nodes=nodes)
    monkeypatch.setattr(evals, "_discard_prompt", lambda *a, **k: False)
    left = evals.sweep(Eng(mac), ({1: {10}}, {1}))
    assert sorted(mac.pressed()) == ["d2", "f2"] and left == []
    assert mac.keys() == [], "a panel on no screen was reached for with a key"


def test_escape_goes_to_a_prompt_only_once_the_keyboard_is_there(monkeypatch):
    monkeypatch.setattr(evals, "_discard_prompt", lambda *a, **k: False)
    mac = Mac([DOC, PROMPT], [TEXTEDIT, NOTIFIER], front=1, focused=1)       # the keyboard stays in TextEdit
    left = evals.sweep(Eng(mac), ({1: {10}}, {1}))
    assert mac.keys() == [], "Escape was pressed at whatever had the keyboard"
    assert [x["pid"] for x in left] == [77], "a prompt that could not be stepped back from is reported"

    mac = Mac([DOC, PROMPT], [TEXTEDIT, NOTIFIER], front=1, focused=77)      # the prompt has taken the focus
    evals.sweep(Eng(mac), ({1: {10}}, {1}))
    assert mac.keys() == ["escape"]
    assert not any(m == "ax.perform" for m, p in mac.calls), "the harness pressed a button on a system prompt"


class Launched(FakeHelper):
    """Two apps the run launched: one quits when asked, one only when forced."""

    def __init__(self):
        super().__init__()
        self.alive = {98: "x.polite", 99: "x.stubborn"}

    def call(self, method, timeout=30.0, **p):
        if method == "apps.running":
            return super().call(method, timeout, **p) + [{"pid": pid, "name": b.split(".")[-1].title(), "bundle_id": b}
                                                         for pid, b in self.alive.items()]
        if method == "apps.quit":
            self.calls.append((method, p))
            if p.get("force") or p["pid"] == 98:
                self.alive.pop(p["pid"], None)
            return {"terminated": p["pid"] not in self.alive, "asked": True}
        return super().call(method, timeout, **p)


def test_an_app_the_run_launched_is_asked_to_quit_before_it_is_forced(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "_discard_prompt", lambda engine, pid: False)
    h = Launched()
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    left = evals.sweep(eng, ({}, {42, 7}))          # 98 and 99 were not running when the run began
    quits = [(p["pid"], bool(p.get("force"))) for m, p in h.calls if m == "apps.quit"]
    assert quits == [(98, False), (99, False), (99, True)], quits
    assert left == []

    h = Launched()
    forced: list[str] = []
    evals.sweep(Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([])), ({}, {42, 7}), forced)
    assert forced == ["x.stubborn"], "a forced quit is counted, by bundle id"
