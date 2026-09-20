"""Capability surfaces the Mac describes for itself.

An app's URL schemes and its App Intents are the app saying what it can be asked to do without touching its
windows. Both were already being read — the schemes into the app model, nothing at all for intents — and
neither could ever become an action: the schemes were shown to the planner as prose, and the planner's reply
had no way to express "open this link".
"""

import json

from macwork.appmodel import parse_app_intents
from macwork.engine import Engine
from macwork.model import Observation
from macwork.observe import Ctx, schemes
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


class Models:
    """Stands in for AppModels: only what the provider reads."""

    def __init__(self, **model):
        self.model = model

    def get(self, app):
        return self.model


def test_an_apps_own_url_schemes_become_actions(tmp_path):
    obs = Observation(app={"pid": 1, "name": "Things"}, window=None, affordances=[])
    ctx = Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 1, "name": "Things"},
              cache={"appmodels": Models(url_schemes=["things", "x-things"], name="Things")})
    schemes(ctx, obs)

    assert [a.label for a in obs.affordances] == ["open a 「things:」 link with Things",
                                                  "open a 「x-things:」 link with Things"]
    assert list(obs.affordances[0].slots) == ["url"], "the link itself is text, so it comes from the caller"


def test_an_app_that_declares_no_scheme_offers_nothing(tmp_path):
    obs = Observation(app={"pid": 1, "name": "计算器"}, window=None, affordances=[])
    schemes(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 1, "name": "计算器"},
                cache={"appmodels": Models(url_schemes=[])}), obs)
    assert obs.affordances == []


def test_the_planner_can_propose_a_link_and_it_is_gated_by_what_the_link_is(tmp_path):
    """A link can be a mailto: as easily as an app's own "do this" URL. The suggestion carries the whole link
    in its label, so the floor judges it before it is ever chosen."""
    decider = ScriptedDecider([{"pick": "新建文稿", "move": "rethink"}, {"pick": "open the link"}])
    decider.what, decider.what_when = {"send": 0.95}, "mailto:"
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=decider)
    engine._planner = FakeBackend([{"steps": ["写封邮件"], "inputs": {},
                                    "try": [{"open_url": "mailto:someone@example.com?subject=hi"}]}])

    result = engine.do("给他发封邮件")
    assert result["status"] == "need_confirm"
    assert result["pending"]["because"] == ["send"]
    assert "mailto:someone@example.com" in result["pending"]["confirm"]["label"]


def test_a_link_the_caller_supplied_is_judged_with_the_link_in_it(tmp_path):
    """Here the label says only "open a things: link" — the link arrives as an input, and is judged then."""
    from macwork.appmodel import AppModels

    class Declares(AppModels):
        def get(self, app):
            return {**(super().get(app) or {}), "url_schemes": ["things"]}

    decider = ScriptedDecider([{"pick": "things:"}])
    decider.what, decider.what_when = {"delete": 0.95}, "things:///remove"
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["schemes", "menu"]}}),
                    helper=FakeHelper(), decider=decider)
    engine.models = engine.cache["appmodels"] = Declares(engine.cfg)

    result = engine.do("清一下待办", inputs={"url": "things:///remove?id=42"})
    assert result["status"] == "need_confirm"
    assert result["pending"]["because"] == ["delete"]
    assert "things:///remove" in result["pending"]["confirm"]["text"]


def test_app_intents_are_read_from_the_bundles_own_metadata(tmp_path):
    """Written against the real format; skipped where the bundle has none (older macOS)."""
    got = parse_app_intents("/System/Applications/Preview.app")
    if not got:
        return
    assert all({"id", "summary", "params", "opens_app"} <= set(a) for a in got)
    assert any(a["params"] for a in got), "at least one action takes a parameter"
    assert parse_app_intents("/nowhere.app") == []


def test_declared_actions_reach_the_planner(tmp_path):
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    app = {"bundle_id": "com.apple.TextEdit", "name": "文本编辑", "path": "/System/Applications/Preview.app"}
    brief = engine.models.brief(app)

    if parse_app_intents(app["path"]):
        assert brief["declared_actions"], "what the app says it can do belongs in the planner's brief"
        assert any("${" in a or a for a in brief["declared_actions"])


# --- the menu bar's right-hand end, and the clipboard ------------------------------------------------------

EXTRAS = {"nodes": [
    {"ref": "x.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "x.1", "role": "AXMenuBarItem", "title": "Wi‑Fi, connected, 3 bars", "actions": ["AXPress"], "parent": "x.0"},
    {"ref": "x.2", "role": "AXMenuBarItem", "title": "Clock", "actions": ["AXPress"], "parent": "x.0"},
], "ms": 2}


class HasStatusItems(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "ax.extras_owners":
            self.calls.append((method, p))
            return [{"pid": 900, "name": "Control Center", "items": 2}]
        if method == "ax.snapshot" and p.get("scope") == "extras_menubar":
            self.calls.append((method, p))
            return EXTRAS
        if method == "clipboard.read":
            self.calls.append((method, p))
            return {"change_count": 7, "types": ["public.utf8-plain-text"], "chars": 12}
        return super().call(method, timeout, **p)


def _observed(engine, tries=6):
    """The owners scan runs behind the look, so the first one legitimately comes up empty."""
    import time as _t
    for _ in range(tries):
        res = engine.observe()
        if res["notes"].get("menubar_extras_n"):
            return res
        _t.sleep(0.05)
    return res


def test_the_menu_bars_status_items_become_actions(tmp_path):
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["menubar_extras"]}}),
                    helper=HasStatusItems(), decider=ScriptedDecider([]))
    res = _observed(engine)

    labels = [a["label"] for a in res["affordances"]]
    assert any("Wi‑Fi" in label for label in labels), labels
    assert any("Clock" in label for label in labels)
    assert all("菜单栏" in a["context"] or "menu bar" in a["context"] for a in res["affordances"])


def test_the_owners_scan_never_holds_up_a_look(tmp_path):
    """It has to ask every process on the Mac, which costs about a second. It runs behind the look instead."""
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["menubar_extras"]}}),
                    helper=HasStatusItems(), decider=ScriptedDecider([]))
    first = engine.observe()
    assert first["notes"]["menubar_extras_n"] == 0
    assert first["notes"]["menubar_extras_ms"] <= 2


def test_the_clipboard_is_reported_by_shape_not_by_contents(tmp_path):
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["clipboard"]}}),
                    helper=HasStatusItems(), decider=ScriptedDecider([]))
    res = engine.observe()

    assert res["notes"]["clipboard"] == {"types": ["public.utf8-plain-text"], "chars": 12}
    asked = next(p for m, p in engine.helper.calls if m == "clipboard.read")
    assert asked["preview_chars"] == 0, "a password manager puts real secrets there"


def test_putting_text_on_the_clipboard_is_offered_only_when_there_is_text(tmp_path):
    conf = {"observe": {"providers": ["clipboard"]}}
    engine = Engine(cfg(tmp_path, config=conf), helper=HasStatusItems(), decider=ScriptedDecider([]))
    assert engine.observe()["affordances"] == []
    assert engine.observe(inputs={"text": "hello"})["affordances"][0]["channel"] == "clipboard"


# --- services, dragging, reading a file, and the UI that has no Dock icon ----------------------------------

from macwork.act import file_channel, get_channel          # noqa: E402
from macwork.appmodel import parse_services                # noqa: E402
from macwork.model import Affordance, Slot                 # noqa: E402
from macwork.observe import drag                           # noqa: E402


class HasServices(FakeHelper):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.performed = []

    def call(self, method, timeout=30.0, **p):
        if method == "apps.installed":
            self.calls.append((method, p))
            return [{"name": "文本编辑", "path": "/System/Applications/TextEdit.app", "bundle_id": "com.apple.TextEdit"}]
        if method == "services.perform":
            self.calls.append((method, p))
            self.performed.append(p)
            return {"ok": True}
        if method == "file.read_text":
            self.calls.append((method, p))
            return {"text": "会议定在周四 15:00", "kind": "public.plain-text"}
        return super().call(method, timeout, **p)


def test_services_are_read_from_the_publishing_bundle(tmp_path):
    declared = parse_services("/System/Applications/TextEdit.app")
    if not declared:
        return                                   # no Services on this macOS build
    assert all({"name", "sends", "returns"} <= set(s) for s in declared)
    assert any("TextEdit" in s["name"] for s in declared)


def test_a_service_becomes_an_action_and_runs_on_its_own_pasteboard(tmp_path):
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["services"]}}),
                    helper=HasServices(), decider=ScriptedDecider([]))
    res = _observed(engine)
    offered = [a for a in res["affordances"] if a["channel"] == "service"]
    if not offered:
        return
    assert "文本编辑" in offered[0]["label"]

    from macwork.observe import Ctx
    ctx = Ctx(engine.cfg, engine.helper, app={"pid": 42})
    action = Affordance("v0", "service", "perform", offered[0]["label"], {"name": "Open Selected File in TextEdit"},
                        slots={"text": Slot("text", "x")})
    out = get_channel("service")(ctx, action, {"text": "/tmp/notes.txt"})
    assert out.ok and engine.helper.performed[0]["name"] == "Open Selected File in TextEdit"


def test_dragging_is_one_action_that_takes_both_ends_by_name(tmp_path):
    """macOS never says which elements can be dragged, so offering a drag per element would be a guess."""
    obs = Observation(app={"pid": 42, "name": "文本编辑"}, window=None, affordances=[
        Affordance("w0", "window", "press", "文件 A", {"ref": "r0", "pid": 42, "frame": [10, 10, 100, 20]}),
        Affordance("w1", "window", "press", "文件夹 B", {"ref": "r1", "pid": 42, "frame": [10, 200, 100, 20]}),
    ])
    ctx = Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "文本编辑"})
    drag(ctx, obs)

    dragger = obs.affordances[-1]
    assert dragger.verb == "drag_named" and set(dragger.slots) == {"from", "onto"}

    out = get_channel("pointer")(ctx, dragger, {"from": "文件 A", "onto": "文件夹 B"})
    assert out.ok
    asked = ctx.helper.did("input.drag")[0]
    assert (asked["x1"], asked["y1"]) == (60, 20) and (asked["x2"], asked["y2"]) == (60, 210)

    missed = get_channel("pointer")(ctx, dragger, {"from": "文件 A", "onto": "不在屏幕上"})
    assert not missed.ok and "不在屏幕上" in missed.error


def test_reading_a_file_gives_the_task_something_it_may_write(tmp_path):
    """Until now a fact could only come from a screen, so a file had to be opened in an app and read off it."""
    from macwork.facts import Facts
    from macwork.observe import Ctx

    ctx = Ctx(cfg(tmp_path), HasServices(), app={"pid": 42})
    out = file_channel(ctx, Affordance("R0", "file", "read", "read the text of 「notes.txt」", {"path": "/tmp/notes.txt"}), {})
    assert out.ok and out.output["file_read"]["text"].startswith("会议")

    facts = Facts(inputs={}, goal="会议什么时候")
    assert facts.source_of("周四 15:00") is None
    read = out.output["file_read"]
    facts.record(read["path"].rsplit("/", 1)[-1], read["kind"], read["text"], 0)
    assert facts.source_of("周四 15:00") is not None


def test_the_ui_without_a_dock_icon_can_be_worked_in(tmp_path):
    """The Dock, Control Center and the input-method agent are ordinary processes with an Accessibility tree."""
    class WithAgents(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "apps.running" and p.get("all"):
                self.calls.append((method, p))
                return super().call("apps.running") + [
                    {"pid": 501, "name": "Dock", "bundle_id": "com.apple.dock", "path": "/System/Library/CoreServices/Dock.app"}]
            return super().call(method, timeout, **p)

    engine = Engine(cfg(tmp_path), helper=WithAgents(), decider=ScriptedDecider([]))
    running = engine.helper.call("apps.running")
    assert engine._resolve_app("Dock", running) == {"pid": 501, "name": "Dock", "bundle_id": "com.apple.dock",
                                                    "path": "/System/Library/CoreServices/Dock.app"}
    assert engine._resolve_app("nothing called this", running) is None
