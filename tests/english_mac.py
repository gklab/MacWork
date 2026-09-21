"""A small English fake Mac whose window text can be changed mid-task.

`tests/test_engine.py` holds the fake Mac most tests use. This one adds a knob — `text`, what the window says —
for tests about a screen that changes while a task runs. Both are in English: a test should read without
knowing another language unless the language is what it tests (see test_english_first.py).
"""

import copy

from tests.test_engine import FakeHelper

APP = {"pid": 42, "name": "TextEdit", "bundle_id": "com.apple.TextEdit", "path": "/System/Applications/TextEdit.app"}

MENUBAR = {"nodes": [
    {"ref": "g1.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "g1.1", "role": "AXMenuBarItem", "title": "Apple", "parent": "g1.0"},
    {"ref": "g1.2", "role": "AXMenu", "parent": "g1.1"},
    {"ref": "g1.3", "role": "AXMenuItem", "title": "Recent Items", "parent": "g1.2"},
    {"ref": "g1.4", "role": "AXMenuBarItem", "title": "File", "parent": "g1.0"},
    {"ref": "g1.5", "role": "AXMenu", "parent": "g1.4"},
    {"ref": "g1.6", "role": "AXMenuItem", "title": "New", "cmd": {"char": "N", "mods": 0}, "parent": "g1.5"},
    {"ref": "g1.7", "role": "AXMenuItem", "title": "Delete Document", "parent": "g1.5"},
    {"ref": "g1.8", "role": "AXMenuItem", "title": "Export", "parent": "g1.5"},
    {"ref": "g1.9", "role": "AXMenu", "parent": "g1.8"},
    {"ref": "g1.10", "role": "AXMenuItem", "title": "PDF", "cmd": {"char": "P", "mods": 1}, "parent": "g1.9"},
], "ms": 3}

WINDOW = {"nodes": [
    {"ref": "g2.0", "role": "AXWindow", "title": "Untitled", "depth": 0},
    {"ref": "g2.1", "role": "AXGroup", "title": "Toolbar", "parent": "g2.0"},
    {"ref": "g2.2", "role": "AXButton", "rdesc": "button", "title": "Refresh", "actions": ["AXPress"], "parent": "g2.1"},
    {"ref": "g2.3", "role": "AXTextField", "rdesc": "text field", "placeholder": "Search", "parent": "g2.0"},
    {"ref": "g2.4", "role": "AXStaticText", "value": "3 items", "parent": "g2.0"},
], "ms": 4}


class EnglishMac(FakeHelper):
    """`text` is what the window says; change it and the screen has changed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.focus_app = APP["name"]
        self.text = WINDOW["nodes"][4]["value"]

    def call(self, method, timeout=30.0, **p):
        if method in ("apps.running", "apps.frontmost", "ax.snapshot", "system.locale"):
            self.calls.append((method, p))
        if method == "apps.running":
            return [APP, {"pid": 7, "name": "Finder", "bundle_id": "com.apple.finder", "path": "/System/Library/CoreServices/Finder.app"}]
        if method == "apps.frontmost":
            return {"app": APP}
        if method == "system.locale":
            return {"locale": "en_US", "languages": ["en-US"], "ocr_languages": ["en-US"], "region": "US"}
        if method == "ax.snapshot":
            if p.get("scope") == "menubar":
                return MENUBAR
            window = copy.deepcopy(WINDOW)
            window["nodes"][4]["value"] = self.text
            return window
        return super().call(method, timeout, **p)
