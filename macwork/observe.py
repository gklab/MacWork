"""Providers turn the current Mac into affordances. Each is a function ``(ctx, obs) -> None`` that appends to
the observation; register more with ``@provider("name")`` or the ``macwork.providers`` entry point and
list them in ``observe.providers``. Nothing here names an app — everything comes from what is installed,
running and on screen.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .helper import Helper, HelperError
from .appmodel import parse_services
from .model import Affordance, Observation, Slot

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    cfg: Config
    helper: Helper
    goal: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    app: dict[str, Any] | None = None          # the app being worked in
    running: list[dict[str, Any]] = field(default_factory=list)
    cache: dict[str, Any] = field(default_factory=dict)   # shared across observations of one engine
    gate: Any = None                            # privacy.Gate: the only way to ask the decider
    hands: Any = None                           # callable: may the engine use the keyboard and the front app now?
    redactor: Any = None                        # this task's pseudonym table


Provider = Callable[[Ctx, Observation], None]
PROVIDERS: dict[str, Provider] = {}


def provider(name: str) -> Callable[[Provider], Provider]:
    def reg(fn: Provider) -> Provider:
        PROVIDERS[name] = fn
        return fn
    return reg


def get_provider(name: str) -> Provider | None:
    if name in PROVIDERS:
        return PROVIDERS[name]
    for ep in entry_points(group="macwork.providers"):
        if ep.name == name:
            PROVIDERS[name] = ep.load()
            return PROVIDERS[name]
    return None


def _refresh_in_background(ctx: Ctx, key: str, ttl: float, make: Callable[[], Any]) -> Any:
    """What was last found, and a refresh behind it when that has gone stale.

    For surfaces whose enumeration is slow but changes rarely. Never blocks the look: the first one gets
    nothing, which is the honest cost of not making every step wait for it.
    """
    hit = ctx.cache.get(key)
    fresh = hit and time.monotonic() - hit[0] < ttl
    if not fresh and not ctx.cache.get(f"{key}.running"):
        ctx.cache[f"{key}.running"] = True

        def refresh() -> None:
            try:
                ctx.cache[key] = (time.monotonic(), make())
            except Exception as exc:  # noqa: BLE001  (a background scan must never take the task down)
                log.info("%s: background refresh failed (%s)", key, exc)
            finally:
                ctx.cache[f"{key}.running"] = False
        threading.Thread(target=refresh, name=f"refresh-{key}", daemon=True).start()
    return hit[1] if hit else None


def _cached(ctx: Ctx, key: str, ttl: float, make: Callable[[], Any]) -> Any:
    hit = ctx.cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = make()
    ctx.cache[key] = (time.monotonic(), value)
    return value


# ----------------------------------------------------------------------------- apps
@provider("apps")
def apps(ctx: Ctx, obs: Observation) -> None:
    here = (ctx.app or {}).get("pid")
    running_paths = set()
    for i, a in enumerate(ctx.running):
        running_paths.add(a.get("path"))
        if a.get("pid") == here or not a.get("name"):
            continue
        obs.affordances.append(Affordance(f"a{i}", "app", "activate", f"switch to app {a['name']}",
                                          {"pid": a["pid"], "bundle_id": a.get("bundle_id"), "name": a["name"]}))
    if not ctx.cfg.get("observe.apps.include_installed", True):
        return
    dirs = ctx.cfg.get("observe.apps.dirs") or []
    installed = _cached(ctx, "apps.installed", 600, lambda: ctx.helper.call("apps.installed", dirs=dirs))
    for i, a in enumerate(installed):
        if a.get("path") in running_paths:
            continue
        name = a["name"] if a["name"] == a.get("file") else f"{a['name']} ({a.get('file')})"
        obs.affordances.append(Affordance(f"i{i}", "app", "open", f"open app {name}",
                                          {"path": a["path"], "bundle_id": a.get("bundle_id"), "name": a["name"]}))


# ----------------------------------------------------------------------------- menu bar
_MODS = [(1, "⇧"), (2, "⌥"), (4, "⌃")]


def _shortcut(cmd: dict[str, Any] | None) -> str:
    if not cmd or not cmd.get("char"):
        return ""
    m = int(cmd.get("mods") or 0)
    keys = "".join(sym for bit, sym in _MODS if m & bit) + ("" if m & 8 else "⌘")
    return f" ({keys}{cmd['char']})"


_COMBO_MODS = [(8, None), (4, "ctrl"), (2, "alt"), (1, "shift")]


def _combo(cmd: dict[str, Any] | None) -> str | None:
    """The menu item's own key equivalent as a key combo (only printable keys; glyph keys are left to the press)."""
    ch = str((cmd or {}).get("char") or "")
    if len(ch) != 1 or not ch.isprintable() or ch.isspace():
        return None
    m = int((cmd or {}).get("mods") or 0)
    mods = ([] if m & 8 else ["cmd"]) + [name for bit, name in _COMBO_MODS if name and m & bit]
    return "+".join(mods + [ch.lower()]) if mods else None


def _tree(nodes: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    by_ref = {n["ref"]: n for n in nodes}
    kids: dict[str, list[str]] = {}
    for n in nodes:
        if "parent" in n:
            kids.setdefault(n["parent"], []).append(n["ref"])
    return by_ref, kids


@provider("menu")
def menu(ctx: Ctx, obs: Observation) -> None:
    if not ctx.app:
        return
    ax = ctx.cfg.section("observe.ax")
    front = (ctx.helper.call("apps.frontmost").get("app") or {}) if ctx.cfg.get("observe.menu.cache_s", 0) else {}
    key = ("menu", ctx.app["pid"], front.get("pid"), front.get("window"), ctx.cache.get("actions_done", 0))
    hit = ctx.cache.get("menu.snap")
    if hit and hit[0] == key and time.monotonic() - hit[1] < float(ctx.cfg.get("observe.menu.cache_s", 0)):
        snap = hit[2]   # same app, same window, nothing done since: the menus have not changed
    else:
        snap = ctx.helper.call("ax.snapshot", pid=ctx.app["pid"], scope="menubar", max_nodes=ax.get("max_nodes", 3000),
                               max_depth=12, budget_ms=ax.get("budget_ms", 3000), visible_only=False, actions=False)
        ctx.cache["menu.snap"] = (key, time.monotonic(), snap)
    nodes = snap.get("nodes", [])
    by_ref, kids = _tree(nodes)
    tops = [r for r in kids.get(nodes[0]["ref"], [])] if nodes else []
    if ctx.cfg.get("observe.menu.skip_first_menu", True):
        tops = tops[1:]
    include_disabled = ctx.cfg.get("observe.menu.include_disabled", False)

    def walk(ref: str, path: list[str]) -> None:
        for c in kids.get(ref, []):
            n = by_ref[c]
            title = n.get("title", "")
            sub = kids.get(c, [])
            if n.get("role") == "AXMenuItem" and title:
                if sub:  # a submenu: its items are the targets
                    for s in sub:
                        walk(s, path + [title])
                elif include_disabled or n.get("enabled", True):
                    checked = f" {n['mark']}" if n.get("mark") else ""
                    obs.affordances.append(Affordance(f"m{len(obs.affordances)}", "menu", "press",
                                                      f"menu {' ▸ '.join(path + [title])}{checked}{_shortcut(n.get('cmd'))}",
                                                      {"ref": c, "pid": ctx.app["pid"], "combo": _combo(n.get("cmd"))},
                                                      context=' ▸ '.join(path[:1])))
                    if n.get("mark"):
                        obs.notes.setdefault("checked", []).append(' ▸ '.join(path + [title]))
            elif n.get("role") == "AXMenu":
                walk(c, path)

    for t in tops:
        walk(t, [by_ref[t].get("title", "")])
    for n in nodes:   # a search field inside a menu (the Help menu's) finds any command by name
        if n.get("role") in ("AXSearchField", "AXTextField"):
            top = n
            while top.get("parent") and by_ref.get(top["parent"], {}).get("role") != "AXMenuBar":
                top = by_ref[top["parent"]]
            obs.affordances.append(Affordance("q0", "menusearch", "search", "search this app's menu commands by name",
                                              {"menu_ref": top["ref"], "field_ref": n["ref"], "pid": ctx.app["pid"]},
                                              slots={"text": Slot("text", "words of the command to look for")}, context=ctx.app.get("name", "")))
            break
    obs.notes["menu_ms"] = snap.get("ms")


# ----------------------------------------------------------------------------- focused window
def _label(n: dict[str, Any]) -> str:
    for k in ("title", "desc", "value", "placeholder", "help"):
        v = n.get(k)
        if v and not str(v).startswith("_NS:"):
            return str(v)
    return ""


def _context_of(n: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> str:
    parts, p = [], n.get("parent")
    while p and len(parts) < 2:
        a = by_ref.get(p, {})
        t = a.get("title") or a.get("desc")
        if t and a.get("role") not in ("AXWindow", "AXApplication"):
            parts.append(str(t))
        p = a.get("parent")
    return " ▸ ".join(reversed(parts))


def element_affordances(ctx: Ctx, obs: Observation, nodes: list[dict[str, Any]], prefix: str, where: str = "") -> None:
    """Turn AX nodes into affordances: one per (element, offered action); text fields become typing targets;
    readable text feeds the screen text; actionable elements without any label are kept aside for the
    vision provider to name."""
    wcfg = ctx.cfg.section("observe.window")
    labels = wcfg.get("action_labels") or {}
    menu_roles = set(wcfg.get("context_menu_roles") or [])
    text_roles = set(wcfg.get("text_roles") or [])
    read_roles = set(wcfg.get("read_roles") or [])
    by_ref, kids = _tree(nodes)
    select_roles = set(wcfg.get("select_roles") or [])
    seen_text = set(obs.screen_text.splitlines())
    texts: list[str] = []

    def inner_text(ref: str, depth: int = 0) -> list[str]:   # a row is named by the text it shows
        out: list[str] = []
        for c in kids.get(ref, []):
            k = by_ref[c]
            t = (k.get("value") or k.get("title") or k.get("desc")) if k.get("role") in text_roles | read_roles else None
            if t and str(t).strip():
                out.append(str(t).strip())
            if depth < 3 and len(out) < 3:
                out += inner_text(c, depth + 1)
        return out[:3]

    in_row: set[str] = set()   # text inside a row is the row's content: it names the row, it is not a target
    for n in nodes:
        p = n.get("parent")
        if p and (p in in_row or by_ref.get(p, {}).get("role") in select_roles):
            in_row.add(n["ref"])

    for n in nodes:
        role = n.get("role", "")
        rd = n.get("rdesc") or role.removeprefix("AX").lower()
        if n.get("enabled", True) is False:
            continue
        if n["ref"] in in_row and (role in text_roles or role in read_roles or role in ("AXCell", "AXImage", "AXGroup")):
            continue
        ctx_text = _context_of(n, by_ref) or where
        if role in select_roles:   # rows are chosen by selecting them, not by an action
            name = _label(n) or " · ".join(dict.fromkeys(inner_text(n["ref"])))
            if name:
                state = " (selected)" if n.get("selected") else ""
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select", f"select {rd} 「{name[:80]}」{state}",
                                                  {"ref": n["ref"], "pid": ctx.app["pid"], "frame": n.get("frame")}, context=ctx_text))
        if role in text_roles and n.get("editable") is False:   # shows text but cannot be typed into: read it
            role = "AXStaticText"
        if role in text_roles:
            label = n.get("title") or n.get("desc") or n.get("placeholder") or ctx_text or rd
            current = f" (now: {str(n['value'])[:60]})" if n.get("value") and role != "AXSecureTextField" else ""
            target = {"ref": n["ref"], "pid": ctx.app["pid"], "secure": role == "AXSecureTextField", "frame": n.get("frame")}
            if n.get("value") and role != "AXSecureTextField":   # what fields hold is the best evidence that typing worked
                obs.notes.setdefault("fields", []).append(f"{label}: {str(n['value'])[: int(wcfg.get('field_chars', 300))]}")
            obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type", f"type into {rd} 「{label}」{current}",
                                              target, slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text))
            if role in set(wcfg.get("range_roles") or []) and n.get("value") and n.get("editable") is not False:
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select_text",
                                                  f"select part of the text in {rd} 「{label}」", dict(target),
                                                  slots={"selection": Slot("text", "the exact text to select, as it appears there")}, context=ctx_text))
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "cursor_end",
                                                  f"put the cursor at the end of the text in {rd} 「{label}」", dict(target), context=ctx_text))
            if role in set(wcfg.get("submit_roles") or []):
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type_submit",
                                                  f"type into {rd} 「{label}」 and press Return{current}", dict(target),
                                                  slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text))
            continue
        # a subrole (close button, sort button…) makes the role description itself a name
        label = _label(n) or (str(n["rdesc"]) if n.get("subrole") and n.get("rdesc") else "")
        offered = [a for a in n.get("actions", []) if a in labels and (a != "AXShowMenu" or role in menu_roles)]
        if offered and not label:
            obs.notes.setdefault("unlabeled", []).append({"ref": n["ref"], "role": role, "rdesc": rd, "frame": n.get("frame"),
                                                           "action": offered[0], "context": ctx_text, "pid": ctx.app["pid"]})
        elif offered:
            state = " (selected)" if n.get("selected") else ""
            for act in offered:
                verb = labels.get(act) or ""
                goes = f" → {n['url']}" if n.get("url") else ""   # a link's target, from the app itself
                text = f"{verb + ' ' if verb else ''}{rd} 「{label}」{goes}{state}"
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "press", text,
                                                  {"ref": n["ref"], "pid": ctx.app["pid"], "action": act, "frame": n.get("frame")}, context=ctx_text))
        if role in read_roles or (n.get("role") in text_roles and n.get("editable") is False):
            t = str(n.get("value") or n.get("title") or n.get("desc") or "").strip()
            if t and n.get("url"):   # where a link goes is the thing worth knowing about it
                t = f"{t} → {n['url']}"
            if t and t not in seen_text:
                seen_text.add(t)
                texts.append(t)
    if texts:
        obs.screen_text = "\n".join(filter(None, [obs.screen_text] + texts))[: int(wcfg.get("screen_text_chars", 1500))]


def _snap(ctx: Ctx, scope: str, manual: bool = False) -> dict[str, Any]:
    ax = ctx.cfg.section("observe.ax")
    return ctx.helper.call("ax.snapshot", pid=ctx.app["pid"], scope=scope, max_nodes=ax.get("max_nodes", 3000),
                           max_depth=ax.get("max_depth", 40), max_children=ax.get("max_children", 300),
                           max_text=ax.get("max_text", 200), budget_ms=ax.get("budget_ms", 3000),
                           visible_only=ax.get("visible_only", True) and scope != "open_menus", manual_accessibility=manual,
                           keep_offscreen_roles=ctx.cfg.get("observe.window.select_roles") or [],
                           settable_roles=ctx.cfg.get("observe.window.text_roles") or [],
                           skip_roles=(ax.get("window_skip_roles") or []) if scope == "focused_window" else [])


@provider("window")
def window(ctx: Ctx, obs: Observation) -> None:
    if not ctx.app:
        return
    ax = ctx.cfg.section("observe.ax")
    actions = set((ctx.cfg.get("observe.window.action_labels") or {}).keys())
    text_roles = set(ctx.cfg.get("observe.window.text_roles") or [])
    count = lambda nodes: sum(1 for n in nodes if actions & set(n.get("actions", [])) or n.get("role") in text_roles)  # noqa: E731
    mode = ax.get("manual_accessibility", "auto")
    s = _snap(ctx, "focused_window", mode == "always")
    if mode == "auto" and count(s.get("nodes", [])) < int(ax.get("sparse_tree_below", 8)):
        s2 = _snap(ctx, "focused_window", True)  # Electron/Chromium apps only expose their tree when asked
        if count(s2.get("nodes", [])) > count(s.get("nodes", [])):
            s = s2
            obs.notes["manual_accessibility"] = True
    nodes = s.get("nodes", [])
    if nodes and nodes[0].get("role") == "AXWindow":
        obs.window = nodes[0].get("title") or obs.window
        obs.notes["window_frame"] = nodes[0].get("frame")
    element_affordances(ctx, obs, nodes, "w")
    obs.notes["window_actionable"] = count(nodes)
    obs.notes["window_ms"] = s.get("ms")
    obs.notes["window_truncated"] = s.get("truncated")


@provider("windows")
def windows(ctx: Ctx, obs: Observation) -> None:
    """Every window of the app, not just the focused one: the thing needed is often in another window."""
    if not ctx.app:
        return
    s = ctx.helper.call("ax.snapshot", pid=ctx.app["pid"], scope="windows", max_depth=0, max_nodes=50, actions=False)
    titles = []
    for n in s.get("nodes", []):
        t = n.get("title") or ""
        titles.append(t or "(untitled)")
        if t and t != obs.window:
            obs.affordances.append(Affordance(f"x{len(obs.affordances)}", "window", "raise", f"switch to the window 「{t}」",
                                              {"ref": n["ref"], "pid": ctx.app["pid"], "action": "AXRaise"}, context=ctx.app.get("name", "")))
    obs.notes["open_windows"] = titles
    if not titles and ctx.app.get("path"):   # running without a window: macOS "reopen" brings its main window back
        obs.affordances.append(Affordance(f"x{len(obs.affordances)}", "app", "reopen", f"bring back the main window of {ctx.app.get('name', 'the app')}",
                                          {"pid": ctx.app["pid"], "path": ctx.app["path"], "bundle_id": ctx.app.get("bundle_id"),
                                           "name": ctx.app.get("name")}, context=ctx.app.get("name", "")))


def _overlap(a: list[int], b: list[int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@provider("overlays")
def overlays(ctx: Ctx, obs: Observation) -> None:
    """What covers the app from another process — a permission prompt, a system alert: windows above the app's
    own, overlapping it, owned by a process without a Dock presence (not another ordinary app). Their text is
    evidence for the decider and their controls are options like any other, so an unexpected prompt can be
    answered; the policy floor makes granting anything ("Allow", "Authorize") the user's call."""
    if not ctx.app:
        return
    wins = ctx.helper.call("screen.windows")
    mine = [i for i, w in enumerate(wins) if w.get("pid") == ctx.app["pid"]]
    if not mine:
        return
    frames = [wins[i]["frame"] for i in mine]
    found: list[dict[str, Any]] = []
    for w in wins[: mine[0]]:   # front to back: only what is in front of the app's frontmost window
        if w.get("regular") or not w.get("alpha") or not any(_overlap(w["frame"], f) for f in frames):
            continue
        if any(o["pid"] == w["pid"] for o in found):
            continue
        try:
            nodes = ctx.helper.call("ax.snapshot", pid=w["pid"], scope="windows", max_nodes=300, max_depth=15).get("nodes", [])
        except HelperError:
            nodes = []
        text = " / ".join(dict.fromkeys(str(n.get(k)) for n in nodes for k in ("title", "value", "desc") if n.get(k)))[:400]
        if not text:   # nothing readable (the Dock's transparent layer, effects): nothing to tell the decider
            continue
        found.append({"pid": w["pid"], "from": w.get("owner", ""), "text": text})
        # its controls: pressed through Accessibility like the app's own; the effect is watched on the app
        sub = Ctx(ctx.cfg, ctx.helper, ctx.goal, ctx.inputs, {"pid": w["pid"], "name": w.get("owner", "")}, ctx.running, ctx.cache)
        n0 = len(obs.affordances)
        element_affordances(sub, obs, nodes, "c", where=f"a prompt from {w.get('owner', '')} in front of the app")
        for a in obs.affordances[n0:]:
            a.target["watch"] = ctx.app["pid"]
    if found:
        obs.notes["covered_by"] = [{"from": o["from"], "text": o["text"]} for o in found]
        lines = [f"[in front of the app, from {o['from']}: {o['text']}]" for o in found]
        obs.screen_text = "\n".join(lines + ([obs.screen_text] if obs.screen_text else []))


@provider("menubar_extras")
def menubar_extras(ctx: Ctx, obs: Observation) -> None:
    """The right-hand end of the menu bar: Wi-Fi, Bluetooth, volume, the input method, and every third-party
    status item.

    A whole surface the engine could not see at all — and one nothing here has to know about, since each item
    belongs to whichever process owns it and macOS hands them over through an ordinary Accessibility attribute.
    Status items are owned mostly by background agents, so the scan covers every running process, not just the
    ones with a Dock icon; which processes have any is remembered, because that changes rarely and asking them
    all is the expensive part.
    """
    mc = ctx.cfg.section("observe.menubar_extras")
    if not mc.get("enabled", True):
        return
    depth, nodes_max = int(mc.get("max_depth", 4)), int(mc.get("max_nodes", 60))

    def snapshot(pid: int) -> list[dict[str, Any]]:
        try:
            return ctx.helper.call("ax.snapshot", pid=pid, scope="extras_menubar",
                                   max_nodes=nodes_max, max_depth=depth).get("nodes", [])
        except HelperError:
            return []

    # Who owns one is asked of the helper in a single call and remembered for a long time: status items come
    # and go with apps, not with screens. Even so the scan costs a second or two — every process has to be
    # asked — so it never runs inside a look: the first look goes without, and a refresh runs behind it.
    who = _refresh_in_background(ctx, "menubar.owners", float(mc.get("owners_cache_s", 300)),
                                 lambda: ctx.helper.call("ax.extras_owners")) or []
    who = who[: int(mc.get("max_apps", 20))]
    # like the menu tree: reused until something is acted on, because reading nine processes' trees on every
    # look is most of what this surface costs
    key = (tuple(sorted(a["pid"] for a in who)), ctx.cache.get("actions_done", 0))   # sorted: the scan's order varies
    hit = ctx.cache.get("menubar.snap")
    if hit and hit[0] == key and time.monotonic() - hit[1] < float(mc.get("cache_s", 4)):
        trees = hit[2]
    else:
        trees = {a["pid"]: snapshot(a["pid"]) for a in who}
        ctx.cache["menubar.snap"] = (key, time.monotonic(), trees)
    for app in who:
        nodes = trees.get(app["pid"]) or []
        if not nodes:
            continue
        sub = Ctx(ctx.cfg, ctx.helper, ctx.goal, ctx.inputs, {"pid": app["pid"], "name": app["name"]}, ctx.running, ctx.cache)
        element_affordances(sub, obs, nodes, "e", where=f"the menu bar ▸ {app['name']}")


@provider("clipboard")
def clipboard(ctx: Ctx, obs: Observation) -> None:
    """What is on the clipboard — its shape, not its contents.

    The clipboard is how apps that share nothing else pass data, so whether something is on it changes what a
    "paste" means. Its contents are another matter: a password manager puts real secrets there and this runs on
    every look, so only the types and the size are reported unless ``preview_chars`` says otherwise.
    """
    cc = ctx.cfg.section("observe.clipboard")
    if not cc.get("enabled", True):
        return
    try:
        seen = ctx.helper.call("clipboard.read", preview_chars=int(cc.get("preview_chars", 0)))
    except HelperError:
        return
    if seen.get("types"):
        obs.notes["clipboard"] = {k: seen[k] for k in ("types", "chars", "text") if seen.get(k)}
    if ctx.inputs.get(str(cc.get("requires_input", "text"))):
        obs.affordances.append(Affordance(f"b{len(obs.affordances)}", "clipboard", "put",
                                          str(cc.get("label", "put the given text on the clipboard")), {},
                                          slots={"text": Slot("text", "what to put on the clipboard")}))


@provider("popups")
def popups(ctx: Ctx, obs: Observation) -> None:
    """Context and pop-up menus that are open right now (they belong to the app, not to any window)."""
    if not ctx.app:
        return
    s = _snap(ctx, "open_menus")
    if s.get("nodes"):
        element_affordances(ctx, obs, s["nodes"], "p", where="open menu")
        obs.notes["popup_open"] = True


# ----------------------------------------------------------------------------- vision fallback
def _inside(inner: list[int], outer: list[int], slack: int = 2) -> bool:
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack and
            inner[0] + inner[2] <= outer[0] + outer[2] + slack and inner[1] + inner[3] <= outer[1] + outer[3] + slack)


def _center(f: list[int]) -> tuple[float, float]:
    return f[0] + f[2] / 2, f[1] + f[3] / 2


def _where_in(f: list[int], win: list[int] | None, words: list[str]) -> str:
    """Coarse position of a box inside the window, from a 3x3 grid of configurable words."""
    if not win or len(words) != 9:
        return ""
    cx, cy = _center(f)
    col = min(2, max(0, int(3 * (cx - win[0]) / max(1, win[2]))))
    row = min(2, max(0, int(3 * (cy - win[1]) / max(1, win[3]))))
    return words[row * 3 + col]


def _ocr(ctx: Ctx, obs: Observation, vc: dict[str, Any]) -> dict[str, Any] | None:
    """Read the window on-device; the same window content is read once (keyed by its Accessibility fingerprint)."""
    cache: dict[Any, Any] = ctx.cache.setdefault("vision.ocr", {})
    try:
        fp = ctx.helper.call("ax.fingerprint", pid=ctx.app["pid"], poll_nodes=int(vc.get("fingerprint_nodes", 400))).get("fingerprint")
    except HelperError:
        fp = None
    hit = cache.get((ctx.app["pid"], fp)) if fp is not None else None
    if hit is not None:
        obs.notes["vision_cached"] = True
        return hit
    try:
        res = ctx.helper.call("screen.ocr", pid=ctx.app["pid"], near=obs.notes.get("window_frame"), languages=vc.get("languages") or ["en-US"],
                              fast=bool(vc.get("fast", False)), min_conf=float(vc.get("min_conf", 0.3)), timeout=15)
    except HelperError as exc:
        obs.notes["vision_error"] = str(exc)[:200]
        return None
    if fp is not None:
        if len(cache) > 32:
            cache.clear()
        cache[(ctx.app["pid"], fp)] = res
    return res


@provider("vision")
def vision(ctx: Ctx, obs: Observation) -> None:
    """For what the Accessibility tree cannot say: read the window on-device (OCR) to name unlabeled controls,
    and, when the tree is sparse (canvas/custom-drawn UIs), offer the text on screen as click targets."""
    vc = ctx.cfg.section("observe.vision")
    mode = vc.get("mode", "auto")
    if not ctx.app or mode == "never":
        return
    unlabeled = obs.notes.get("unlabeled", [])
    sparse = int(obs.notes.get("window_actionable", 0)) < int(vc.get("sparse_below", 8))
    if mode == "auto" and not sparse and not unlabeled:
        return
    key = f"{ctx.app['pid']}|{obs.window}"
    if mode == "auto" and not sparse and vc.get("on_demand", True) and key not in ctx.cache.setdefault("vision.wanted", set()):
        # the tree names most things: reading the screen for the rest costs ~0.3 s, so it happens when asked for
        obs.affordances.append(Affordance("vr", "vision", "reveal", f"read the {len(unlabeled)} controls without a label in this window "
                                          "from the screen (to see what they are)", {"key": key}, context=ctx.app.get("name", "")))
        return
    res = _ocr(ctx, obs, vc)
    if res is None:
        return
    boxes = res.get("boxes", [])
    win = res.get("frame")
    grid = vc.get("position_words") or []
    radius = float(vc.get("near_radius", 60))
    for i, u in enumerate(unlabeled[: int(vc.get("max_unlabeled", 40))]):
        f = u.get("frame")
        if not f:
            continue
        inside_boxes = [b for b in boxes if _inside(b["frame"], f)]
        if len(inside_boxes) >= int(vc.get("container_min_texts", 2)):
            # an unlabeled container (gallery, template picker, custom list): each text in it is its own target
            where_c = f" in {u.get('rdesc') or 'area'}" + (f" 「{u['context']}」" if u.get("context") else "")
            for b in inside_boxes[: int(vc.get("max_container_targets", 40))]:
                x, y = _center(b["frame"])
                spot = (b["text"], round(x / 8), round(y / 8))
                if spot in obs.notes.setdefault("_vision_spots", set()):
                    continue          # nested unlabeled containers see the same text: offer it once
                obs.notes["_vision_spots"].add(spot)
                obs.affordances.append(Affordance(f"o{len(obs.affordances)}", "pointer", "click", f"click 「{b['text']}」{where_c}",
                                                  {"x": x, "y": y, "frame": b["frame"]}, context=u.get("context", "")))
            continue
        inside = [b["text"] for b in inside_boxes]
        name = " ".join(inside)
        if not name:
            cx, cy = _center(f)
            near = sorted(((abs(_center(b["frame"])[0] - cx) + abs(_center(b["frame"])[1] - cy), b["text"]) for b in boxes))
            name = f"near 「{near[0][1]}」" if near and near[0][0] <= radius else ""
        else:
            name = f"「{name}」"
        label = (u.get("rdesc") or "control") + " (no label) " + (name or f"#{i + 1}") + (f" at {_where_in(f, win, grid)}" if grid else "")
        describe = vc.get("describer_cmd")
        if describe and not inside:   # a local image describer names icons (e.g. a VLM on this Mac)
            try:
                path = f"/tmp/macwork-icon-{ctx.app['pid']}-{i}.png"
                ctx.helper.call("screen.capture", pid=ctx.app["pid"], near=obs.notes.get("window_frame"), crop=f, path=path, timeout=15)
                out = subprocess.run([*describe, path], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
                if out:
                    label += f" looks like: {out[:80]}"
            except (HelperError, OSError, subprocess.SubprocessError) as exc:
                obs.notes["describer_error"] = str(exc)[:120]
        obs.affordances.append(Affordance(f"v{len(obs.affordances)}", "window", "press", label,
                                          {"ref": u["ref"], "pid": u["pid"], "action": u.get("action"), "frame": f}, context=u.get("context", "")))
    if sparse:
        taken = [a.target["frame"] for a in obs.affordances if a.channel == "window" and a.target.get("frame")]
        for b in boxes[: int(vc.get("max_text_targets", 80))]:
            if any(_inside(b["frame"], t) for t in taken):
                continue   # already reachable through the Accessibility tree
            x, y = _center(b["frame"])
            obs.affordances.append(Affordance(f"o{len(obs.affordances)}", "pointer", "click", f"click the text 「{b['text']}」" + (f" at {_where_in(b['frame'], win, grid)}" if grid else ""),
                                              {"x": x, "y": y, "frame": b["frame"]}))
        lines = [b["text"] for b in sorted(boxes, key=lambda b: (b["frame"][1] // 12, b["frame"][0]))]
        obs.screen_text = "\n".join(filter(None, [obs.screen_text] + lines))[: int(ctx.cfg.get("observe.window.screen_text_chars", 1500))]
    obs.notes.pop("_vision_spots", None)   # internal: notes go back to MCP clients as JSON
    obs.notes["vision_ms"] = res.get("ms")
    obs.notes["vision_boxes"] = len(boxes)


# ----------------------------------------------------------------------------- scripting dictionary
@provider("sdef")
def sdef(ctx: Ctx, obs: Observation) -> None:
    """Commands the app declares for scripting: things it can do without touching its UI."""
    if not ctx.app or not ctx.app.get("path"):
        return
    models = ctx.cache.get("appmodels")
    model = models.get(ctx.app) if models else None
    if not model:
        return
    sc = ctx.cfg.section("observe.sdef")
    skip_suites = set(sc.get("skip_suites") or [])
    skip = set(sc.get("skip_commands") or [])
    simple = set(sc.get("simple_types") or [])
    for i, c in enumerate(model["sdef"]["commands"]):
        if c["suite"] in skip_suites or c["name"] in skip:
            continue
        slots: dict[str, Slot] = {}
        ok = True
        d = c.get("direct")
        if d and not d.get("optional"):
            if d["type"] not in simple:
                continue
            slots["direct"] = Slot("text", d.get("desc") or f"{c['name']} what ({d['type']})")
        for prm in c.get("params", []):
            if prm.get("optional"):
                continue
            if prm["type"] not in simple:
                ok = False
                break
            slots[re.sub(r"\W+", "_", prm["name"])] = Slot("text", prm.get("desc") or f"{prm['name']} ({prm['type']})")
        if not ok:
            continue
        obs.affordances.append(Affordance(f"d{i}", "script_cmd", "run", f"scripting command 「{c['name']}」" + (f": {c['desc'][:100]}" if c.get("desc") else ""),
                                          {"bundle_id": ctx.app.get("bundle_id"), "command": c["name"], "direct": d,
                                           "params": [p for p in c.get("params", []) if not p.get("optional")]},
                                          slots=slots, context=ctx.app.get("name", "")))


# ----------------------------------------------------------------------------- keys
@provider("keys")
def keys(ctx: Ctx, obs: Observation) -> None:
    """Physical keys (Return, Escape, Tab, paging) — the keyboard itself, not any app's commands: those come from
    the app's own menus (with their shortcuts) or are proposed by the planner for this app."""
    for i, combo in enumerate(ctx.cfg.get("observe.keys") or []):
        obs.affordances.append(Affordance(f"k{i}", "keys", "key", f"press the {combo} key", {"combo": combo}))


@provider("typing")
def typing(ctx: Ctx, obs: Observation) -> None:
    """Type at the cursor: for editors and canvases that expose no text field (the text comes from the caller)."""
    tc = ctx.cfg.section("observe.typing")
    needs = tc.get("requires_input")   # only when the caller gave text: otherwise it lures the decider into typing junk
    if ctx.app and tc.get("enabled", True) and (not needs or ctx.inputs.get(needs)):
        obs.affordances.append(Affordance("y0", "keys", "type", str(tc.get("label") or "type the given text at the cursor"), {},
                                          slots={"text": Slot("text", "the text to type at the cursor")}, context=ctx.app.get("name", "")))
        obs.affordances.append(Affordance("y1", "keys", "type_submit", str(tc.get("submit_label") or "type the given text at the cursor and press Return"),
                                          {}, slots={"text": Slot("text", "the text to type at the cursor")}, context=ctx.app.get("name", "")))


# ----------------------------------------------------------------------------- shortcuts
@provider("shortcuts")
def shortcuts(ctx: Ctx, obs: Observation) -> None:
    def load() -> list[str]:
        r = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=10, check=False)
        return [s for s in r.stdout.splitlines() if s.strip()]
    names = _cached(ctx, "shortcuts", float(ctx.cfg.get("observe.shortcuts.cache_s", 300)), load)
    for i, name in enumerate(names):
        obs.affordances.append(Affordance(f"s{i}", "shortcut", "run", f"run shortcut 「{name}」", {"name": name},
                                          slots={"input": Slot("text", "input for the shortcut", required=False)}))


# ----------------------------------------------------------------------------- files / urls named by the caller
@provider("schemes")
def schemes(ctx: Ctx, obs: Observation) -> None:
    """URL schemes the app being worked in declares for itself.

    A scheme is an app saying "you can ask me to do this without touching my windows" — it is in the bundle's
    own Info.plist, so nothing here knows any app. The link itself is text, so it comes from the caller or the
    planner like any other text, and it is judged by the safety floor with the link in it.
    """
    models = ctx.cache.get("appmodels")
    if not ctx.app or models is None:
        return
    model = models.get(ctx.app) or {}
    name = ctx.app.get("name") or model.get("name") or ""
    for scheme in (model.get("url_schemes") or [])[: int(ctx.cfg.get("observe.schemes.limit", 8))]:
        obs.affordances.append(Affordance(
            f"h{len(obs.affordances)}", "file", "open", f"open a 「{scheme}:」 link with {name}",
            {"path": "", "scheme": scheme},
            slots={"url": Slot("text", f"the whole link, starting with {scheme}:")}, context=name))


@provider("services")
def services(ctx: Ctx, obs: Observation) -> None:
    """Services other apps publish: "hand me this kind of content and I will do something with it".

    Look a word up, start an email from a selection, open a folder in a terminal — a system-wide bus between
    apps that share nothing else, which the engine could otherwise only reach by walking into the right app's
    menu. Each one is declared by its own bundle, so nothing here knows any app. Reading every bundle takes a
    moment, so it happens behind the look and is remembered.
    """
    sc = ctx.cfg.section("observe.services")
    if not sc.get("enabled", True):
        return

    def scan() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for app in ctx.helper.call("apps.installed", dirs=ctx.cfg.get("observe.apps.dirs") or []):
            for s in parse_services(app.get("path") or ""):
                out.append({**s, "app": app.get("name") or ""})
        return out

    found = _refresh_in_background(ctx, "services.all", float(sc.get("cache_s", 900)), scan) or []
    file_types = set(sc.get("file_types") or [])
    for s in found[: int(sc.get("limit", 80))]:
        wants_file = bool(set(s.get("sends") or []) & file_types)
        slot = ("file", "the path of the file to hand over") if wants_file else ("text", "the text to hand over")
        obs.affordances.append(Affordance(
            f"v{len(obs.affordances)}", "service", "perform", f"hand content to {s['app']}: 「{s['name']}」",
            {"name": s["name"]}, slots={slot[0]: Slot("text", slot[1])}, context=f"{s['app']} (a system service)"))


@provider("drag")
def drag(ctx: Ctx, obs: Observation) -> None:
    """Dragging one thing onto another.

    macOS does not say which elements can be dragged — there is no such attribute — so offering a drag per
    element would mean guessing, and guessing is what this engine does not do. Instead there is one action
    that takes both ends by name; they are looked up among what is on screen when it runs.
    """
    if not ctx.app or not ctx.cfg.get("observe.drag.enabled", True):
        return
    spots = {a.label: a.target["frame"] for a in obs.affordances if a.target.get("frame")}
    if len(spots) < 2:
        return
    obs.affordances.append(Affordance(
        f"n{len(obs.affordances)}", "pointer", "drag_named",
        str(ctx.cfg.get("observe.drag.label", "drag one thing on screen onto another")),
        {"spots": dict(list(spots.items())[: int(ctx.cfg.get("observe.drag.max_spots", 300))])},
        slots={"from": Slot("text", "the label of what to drag, as it appears on screen"),
               "onto": Slot("text", "the label of what to drop it onto")}))


@provider("files")
def files(ctx: Ctx, obs: Observation) -> None:
    fc = ctx.cfg.section("observe.files")
    if ctx.inputs.get("url"):
        obs.affordances.append(Affordance("u0", "file", "open", f"open {ctx.inputs['url']} in the default app/browser", {"path": str(ctx.inputs["url"])}))
    query = next((str(ctx.inputs[k]) for k in fc.get("input_keys", []) if ctx.inputs.get(k)), "")
    if not query:
        return
    roots = [str(Path(r).expanduser()) for r in fc.get("roots", ["~"])]
    limit = int(fc.get("limit", 15))

    def mdfind(*args: str) -> list[str]:
        out: list[str] = []
        for root in roots:
            r = subprocess.run(["mdfind", "-onlyin", root, *args], capture_output=True, text=True, timeout=10, check=False)
            out += [p for p in r.stdout.splitlines() if p]
        return out

    paths = mdfind("-name", query) or mdfind(query)
    stat = []
    for p in dict.fromkeys(paths):
        try:
            stat.append((Path(p).stat().st_mtime, p))
        except OSError:
            pass
    for i, (mtime, p) in enumerate(sorted(stat, reverse=True)[:limit]):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        pp = Path(p)
        obs.affordances.append(Affordance(f"f{i}", "file", "open", f"open file 「{pp.name}」 in {pp.parent} (modified {when})", {"path": p}))
        obs.affordances.append(Affordance(f"F{i}", "file", "reveal", f"show 「{pp.name}」 in Finder", {"path": p}))
        # what a file says can be read without opening it in anything, and becomes a fact of the task
        obs.affordances.append(Affordance(f"R{i}", "file", "read", f"read the text of 「{pp.name}」", {"path": p}))


# ----------------------------------------------------------------------------- learned routines
@provider("skills")
def skills(ctx: Ctx, obs: Observation) -> None:
    store = ctx.cache.get("skills")
    if store is None:
        return
    for sk in store.for_app((ctx.app or {}).get("bundle_id")):
        obs.affordances.append(Affordance(f"k_{sk['id']}", "skill", "replay", store.describe(sk), {"skill": sk}))


# ----------------------------------------------------------------------------- web research
@provider("web")
def web(ctx: Ctx, obs: Observation) -> None:
    if not ctx.cfg.get("observe.web.enabled", True):
        return
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    obs.affordances.append(Affordance("r0", "web", "research", str(ctx.cfg.get("observe.labels.web_research") or "research this on the web"), {}))


# ----------------------------------------------------------------------------- grouping
def group_of(a: Affordance) -> tuple[str, str]:
    """(key, name) of the group an affordance belongs to: where it lives, not what it might be good for."""
    if a.channel == "menu":
        return f"menu:{a.context}", f"the 「{a.context}」 menu"
    if a.channel == "app":
        return f"app:{a.verb}", "open another app" if a.verb == "open" else "switch to another running app"
    if a.verb == "select":   # rows of one list together, apart from the window's buttons
        where = a.context.split(" ▸ ")[0] if a.context else ""
        return f"list:{where}", f"the list {'「' + where + '」 ' if where else ''}in the window (select a row)"
    if a.channel in ("window", "pointer") and a.verb != "raise":
        where = a.context.split(" ▸ ")[0] if a.context else ""
        return f"area:{where}", f"the area 「{where}」 of the window" if where else "the window"
    names = {"script_cmd": "the app's scripting commands", "shortcut": "the user's Shortcuts", "file": "files found for the inputs",
             "skill": "learned routines", "keys": "keys to press and typing", "menusearch": "searching the menus",
             "web": "research on the web", "vision": "reading the screen"}
    return f"ch:{a.channel}", names.get(a.channel, "general")


def arrange(affs: list[Affordance], budget: int, expanded: set[str], sample: int = 12,
            fold_over: int = 0) -> tuple[list[Affordance], dict[str, tuple[str, list[Affordance]]]]:
    """Fit what can be done into one choice of at most ``budget`` options without guessing relevance: everything
    when it fits; otherwise the smallest groups stay as they are and the largest are offered as one entry each
    ("look into the File menu: New, Open, …"), which the decider can open like a person opens a menu. Groups
    already opened in this task are always shown in full."""
    groups: dict[str, list[Affordance]] = {}
    names: dict[str, str] = {}
    for a in affs:
        k, n = group_of(a)
        groups.setdefault(k, []).append(a)
        names[k] = n
    huge = {k for k in groups if fold_over and len(groups[k]) > fold_over and k not in expanded}   # e.g. every installed app
    if len(affs) <= budget and not huge:
        return affs, {}
    shown = {k for k in groups if (k in expanded or len(groups[k]) == 1) and k not in huge}   # folding one option saves nothing
    used = sum(len(groups[k]) for k in shown) + (len(groups) - len(shown))
    for k in sorted((k for k in groups if k not in shown and k not in huge), key=lambda k: len(groups[k])):
        if used - 1 + len(groups[k]) > budget:
            break
        shown.add(k)
        used += len(groups[k]) - 1
    folded: dict[str, tuple[str, list[Affordance]]] = {}
    for k, members in groups.items():
        if k not in shown:
            names_ = ", ".join(m.label.split(" ▸ ")[-1][:40] for m in members[:sample])
            folded[k] = (f"look into {names[k]} ({len(members)} options: {names_}{', …' if len(members) > sample else ''})", members)
    opened = [a for a in affs if group_of(a)[0] in expanded]   # what the decider asked to see comes first if space runs out
    flat = opened + [a for a in affs if group_of(a)[0] in shown and group_of(a)[0] not in expanded]
    return flat[: max(0, budget - len(folded))], folded


def observe(ctx: Ctx) -> Observation:
    t0 = time.monotonic()
    obs = Observation(app=ctx.app, window=None, affordances=[])
    for name in ctx.cfg.get("observe.providers") or []:
        fn = get_provider(name)
        if fn is None:
            log.warning("unknown provider %s", name)
            continue
        t, n0 = time.monotonic(), len(obs.affordances)
        try:
            fn(ctx, obs)
        except (HelperError, OSError, subprocess.SubprocessError) as exc:
            obs.notes[f"{name}_error"] = str(exc)[:200]
            log.info("provider %s failed: %s", name, exc)
        obs.notes[f"{name}_n"] = len(obs.affordances) - n0
        obs.notes[f"{name}_ms"] = round((time.monotonic() - t) * 1000)
    ids = [a.id for a in obs.affordances]
    if len(ids) != len(set(ids)):
        for i, a in enumerate(obs.affordances):   # providers are independent; make ids unique afterwards
            a.id = f"{a.id}_{i}"
    obs.notes["ms"] = round((time.monotonic() - t0) * 1000)
    return obs
