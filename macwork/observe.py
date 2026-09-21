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
    task: str = ""                              # whose look this is: for what is remembered per task, not per Mac


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


def _refresh_in_background(ctx: Ctx, key: str, ttl: float, make: Callable[[Any], Any]) -> Any:
    """What was last found, and a refresh behind it when that has gone stale.

    For surfaces whose enumeration is slow but changes rarely. Never blocks the look: the first one gets
    nothing, which is the honest cost of not making every step wait for it.

    ``make`` is handed the helper to use, and it is *not* the one the look is using: one connection serves one
    call at a time, so a refresh on the shared one blocks exactly what this is meant to keep moving.
    """
    hit = ctx.cache.get(key)
    fresh = hit and time.monotonic() - hit[0] < ttl
    if not fresh and not ctx.cache.get(f"{key}.running"):
        ctx.cache[f"{key}.running"] = True
        helper = ctx.helper.background() if hasattr(ctx.helper, "background") else ctx.helper

        def refresh() -> None:
            try:
                ctx.cache[key] = (time.monotonic(), make(helper))
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


def installed_apps(cfg: Any, helper: Any) -> list[dict[str, Any]]:
    """Every app this Mac would open, asked of the Mac rather than of a list of folders.

    ``observe.apps.source: dirs`` goes back to scanning the configured folders, which on this Mac finds 146
    apps against 405 — Finder and Safari among the missing, because macOS keeps them outside them.
    """
    return helper.call("apps.installed", dirs=cfg.get("observe.apps.dirs") or [],
                       source=str(cfg.get("observe.apps.source", "launchservices")),
                       timeout=float(cfg.get("observe.apps.timeout_s", 15)) + 5)


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
    installed = _cached(ctx, "apps.installed", 600, lambda: installed_apps(ctx.cfg, ctx.helper))
    for i, a in enumerate(installed):
        if a.get("path") in running_paths:
            continue
        name = a["name"] if a["name"] == a.get("file") else f"{a['name']} ({a.get('file')})"
        # the app's own declaration that it runs without a window: not hidden, but the decider should not
        # expect a window to appear (its menu bar item is reachable through the menubar_extras provider)
        kind = " (a background app: no window, it puts an item in the menu bar)" if a.get("background") else ""
        obs.affordances.append(Affordance(f"i{i}", "app", "open", f"open app {name}{kind}",
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


def _screen_mark(ctx: Ctx) -> Any:
    """What this app's window looked like when the engine last checked — the number it already takes after
    every step, reused here rather than asked for again. When there is none (no window, or speculation off),
    the count of actions taken stands in, which is what the menus used to be keyed on all the time."""
    pid, fp = ctx.cache.get("screen.fp") or (None, None)
    return fp if fp is not None and pid == (ctx.app or {}).get("pid") else ctx.cache.get("actions_done", 0)


@provider("menu")
def menu(ctx: Ctx, obs: Observation) -> None:
    if not ctx.app:
        return
    ax = ctx.cfg.section("observe.ax")
    front = (ctx.helper.call("apps.frontmost").get("app") or {}) if ctx.cfg.get("observe.menu.cache_s", 0) else {}
    # What makes a menu change is the window changing under it — a document opened, a selection made, a mode
    # toggled. Throwing the menus away after *any* action meant re-reading the whole menu bar every step, and
    # a menu bar is not cheap: measured on this Mac, 350–870 ms for TextEdit's 340 items against 0–18 ms to
    # ask what the window now looks like. So the window's own fingerprint decides, and the TTL still bounds
    # how stale an unchanged-looking window may leave it.
    key = ("menu", ctx.app["pid"], front.get("pid"), front.get("window"), _screen_mark(ctx))
    hit = ctx.cache.get("menu.snap")
    if hit and hit[0] == key and time.monotonic() - hit[1] < float(ctx.cfg.get("observe.menu.cache_s", 0)):
        snap = hit[2]   # same app, same window, nothing on screen has moved: the menus have not changed
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
                    ident = n.get("ident") or ""
                    obs.affordances.append(Affordance(f"m{len(obs.affordances)}", "menu", "press",
                                                      f"menu {' ▸ '.join(path + [title])}{checked}{_shortcut(n.get('cmd'))}",
                                                      {"ref": c, "pid": ctx.app["pid"], "combo": _combo(n.get("cmd"))},
                                                      context=' ▸ '.join(path[:1]),
                                                      key=_identity(ident, n.get("role"), n.get("subrole"), title)))
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
def _identity(ident: str, role: str | None, subrole: str | None, label: str = "") -> str:
    """A key that does not change with the interface language, or "" when the Mac offers nothing to build one.

    An Accessibility identifier is the developer's own name for the control — measured on a real Mac, 93-97%
    of menu items have one and it is the selector behind the command, identical in every language and
    unchanged across launches. The label is deliberately *not* part of it: putting it in would make every key
    language-bound again, which is the whole thing being fixed. Identifiers are not always unique (every
    "Recent Items" entry shares `_recentItemRequested:`), and `observe` adds the label to just those, where
    there is nothing else to tell siblings apart.
    """
    return "|".join(x for x in ("ax", role or "", subrole or "", ident) if x) if ident else ""


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
    # Two sources, taken together. The role lists below are no longer the definition of what can be done —
    # anything with an unusual role (a web input, a contenteditable group, a custom control) was simply
    # invisible — but they are not dropped either: the Mac answers "can this attribute be set", and the
    # engine has ways round a "no" (click the row, focus the field and type), so a "no" is not the last word.
    by_capability = bool(wcfg.get("by_capability", True))

    def typeable(n: dict[str, Any], role: str) -> bool:
        return (by_capability and n.get("editable") is True) or (role in text_roles and n.get("editable") is not False)

    def selectable(n: dict[str, Any], role: str) -> bool:
        return (by_capability and n.get("selectable") is True) or role in select_roles

    def has_range(n: dict[str, Any], role: str) -> bool:
        return (by_capability and n.get("text_range") is True) or role in range_roles
    menu_roles = set(wcfg.get("context_menu_roles") or [])
    text_roles = set(wcfg.get("text_roles") or [])
    read_roles = set(wcfg.get("read_roles") or [])
    select_roles = set(wcfg.get("select_roles") or [])
    range_roles = set(wcfg.get("range_roles") or [])
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
        ikey = _identity(n.get("ident") or "", role, n.get("subrole"), _label(n))   # "" when the app gives none
        if n["ref"] in in_row and (role in text_roles or role in read_roles or role in ("AXCell", "AXImage", "AXGroup")):
            continue
        ctx_text = _context_of(n, by_ref) or where
        if selectable(n, role):   # rows are chosen by selecting them, not by an action
            name = _label(n) or " · ".join(dict.fromkeys(inner_text(n["ref"])))
            if name:
                state = " (selected)" if n.get("selected") else ""
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select", f"select {rd} 「{name[:80]}」{state}",
                                                  {"ref": n["ref"], "pid": ctx.app["pid"], "frame": n.get("frame")}, context=ctx_text, key=ikey))
        if role in text_roles and n.get("editable") is False and not by_capability:   # shows text, cannot be typed into
            role = "AXStaticText"
        if typeable(n, role):
            label = n.get("title") or n.get("desc") or n.get("placeholder") or ctx_text or rd
            current = f" (now: {str(n['value'])[:60]})" if n.get("value") and role != "AXSecureTextField" else ""
            target = {"ref": n["ref"], "pid": ctx.app["pid"], "secure": role == "AXSecureTextField", "frame": n.get("frame")}
            if n.get("value") and role != "AXSecureTextField":   # what fields hold is the best evidence that typing worked
                obs.notes.setdefault("fields", []).append(f"{label}: {str(n['value'])[: int(wcfg.get('field_chars', 300))]}")
            obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type", f"type into {rd} 「{label}」{current}",
                                              target, slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text, key=ikey))
            if has_range(n, role) and n.get("value") and n.get("editable") is not False:
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select_text",
                                                  f"select part of the text in {rd} 「{label}」", dict(target),
                                                  slots={"selection": Slot("text", "the exact text to select, as it appears there")}, context=ctx_text, key=ikey))
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "cursor_end",
                                                  f"put the cursor at the end of the text in {rd} 「{label}」", dict(target), context=ctx_text, key=ikey))
            if role in set(wcfg.get("submit_roles") or []):
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type_submit",
                                                  f"type into {rd} 「{label}」 and press Return{current}", dict(target),
                                                  slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text, key=ikey))
            # An element can be both: a table cell takes text *and* has a context menu. Probing capabilities
            # finds many more typing targets than the role list did, so swallowing their actions here would
            # quietly take away what they could already do.
            if not (n.get("actions") and by_capability):
                continue
        # a subrole (close button, sort button…) makes the role description itself a name
        label = _label(n) or (str(n["rdesc"]) if n.get("subrole") and n.get("rdesc") else "")
        named = [a for a in n.get("actions", []) if a in labels and (a != "AXShowMenu" or role in menu_roles)]
        # An action outside the naming table used to be discarded, which made AXRaise, AXCancel, AXDelete and
        # every app's own actions invisible. They are offered now — but only where nothing named already
        # reaches this element: measured on a real screen, AXScrollToVisible sat on 428 of 441 elements that
        # all had AXPress too, and offering it 428 times is noise, not capability.
        extra = [a for a in n.get("actions", []) if a not in labels] if (by_capability and not named) else []
        offered = named + extra
        if offered and not label:
            obs.notes.setdefault("unlabeled", []).append({"ref": n["ref"], "role": role, "rdesc": rd, "frame": n.get("frame"),
                                                           "action": offered[0], "context": ctx_text, "pid": ctx.app["pid"]})
        elif offered:
            state = " (selected)" if n.get("selected") else ""
            for act in offered:
                verb = labels.get(act) or str((n.get("action_desc") or {}).get(act) or "")   # the app's own word
                goes = f" → {n['url']}" if n.get("url") else ""   # a link's target, from the app itself
                text = f"{verb + ' ' if verb else ''}{rd} 「{label}」{goes}{state}"
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "press", text,
                                                  {"ref": n["ref"], "pid": ctx.app["pid"], "action": act, "frame": n.get("frame")}, context=ctx_text, key=ikey))
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
                           by_capability=bool(ctx.cfg.get("observe.window.by_capability", True)),
                           known_actions=list((ctx.cfg.get("observe.window.action_labels") or {}).keys()),
                           max_offscreen=int(ctx.cfg.get("observe.ax.max_offscreen", 200)),
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
    # An *empty* tree is not a sparse one: it means the app has no window open, and asking again with manual
    # accessibility on will not conjure one — it just waits out the same timeouts. Measured on an app with no
    # window: 1516 ms for nothing, then 2018 ms more for nothing.
    if mode == "auto" and s.get("nodes") and count(s.get("nodes", [])) < int(ax.get("sparse_tree_below", 8)):
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
    if s.get("not_answering"):   # the app did not answer Accessibility in time; the helper will not ask again soon
        obs.notes["window_not_answering"] = True
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
    """Windows from other processes that are on screen but belong to no app being worked in — a permission
    prompt, a system alert, a notification banner, a floating panel.

    Two kinds, and the second was missing. One covers the app: above its frontmost window and overlapping it.
    The other is simply *elsewhere on screen* — a banner in a corner overlaps nothing, so a notification
    arriving mid-task was invisible to the engine and to the decider both, which is how a task carries on
    into a dialog it was never told about.

    No process is named here. What makes these different from an ordinary app's window is that the system
    says so: no Dock presence, and something readable in them. That is the same test for an alert nobody has
    heard of as for one that ships with macOS. Their controls are options like any other, so a prompt can be
    answered; the policy floor makes granting anything ("Allow", "Authorize") the user's call.
    """
    if not ctx.app:
        return
    oc = ctx.cfg.section("observe.overlays")
    wins = ctx.helper.call("screen.windows")
    mine = [i for i, w in enumerate(wins) if w.get("pid") == ctx.app["pid"]]
    # An app with no window on screen used to end this provider here, which also took away everything the
    # second pass is for: a prompt or a banner somewhere else does not stop being relevant because the app
    # being worked in happens to be minimised — that is when one is *most* likely to be what wants answering.
    frames = [wins[i]["frame"] for i in mine]
    front = mine[0] if mine else len(wins)
    found: list[dict[str, Any]] = []
    elsewhere_left = int(oc.get("max_elsewhere", 4)) if oc.get("elsewhere", True) else 0
    if not mine and not elsewhere_left:
        return
    for i, w in enumerate(wins):
        if w.get("regular") or not w.get("alpha") or w.get("pid") == ctx.app["pid"] \
           or any(o["pid"] == w["pid"] for o in found):
            continue
        over = i < front and any(_overlap(w["frame"], f) for f in frames)
        if not over:
            if elsewhere_left <= 0:
                continue
            elsewhere_left -= 1
        try:
            nodes = ctx.helper.call("ax.snapshot", pid=w["pid"], scope="windows", max_nodes=300, max_depth=15).get("nodes", [])
        except HelperError:
            nodes = []
        text = " / ".join(dict.fromkeys(str(n.get(k)) for n in nodes for k in ("title", "value", "desc") if n.get(k)))[:400]
        if not text:   # nothing readable (the Dock's transparent layer, effects): nothing to tell the decider
            if not over:
                elsewhere_left += 1     # it cost a snapshot but it is not one of them: do not spend the slot
            continue
        where = "in front of the app" if over else "elsewhere on screen"
        found.append({"pid": w["pid"], "from": w.get("owner", ""), "text": text, "where": where})
        # its controls: pressed through Accessibility like the app's own; the effect is watched on the app
        sub = Ctx(ctx.cfg, ctx.helper, ctx.goal, ctx.inputs, {"pid": w["pid"], "name": w.get("owner", "")}, ctx.running, ctx.cache)
        n0 = len(obs.affordances)
        element_affordances(sub, obs, nodes, "c", where=f"a prompt from {w.get('owner', '')} {where}")
        for a in obs.affordances[n0:]:
            a.target["watch"] = ctx.app["pid"]
        if not over and not oc.get("elsewhere_actions", True):
            del obs.affordances[n0:]    # read it, but do not offer its controls
    if found:
        obs.notes["covered_by"] = [{"from": o["from"], "text": o["text"], "where": o["where"]} for o in found]
        lines = [f"[{o['where']}, from {o['from']}: {o['text']}]" for o in found]
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
                                 lambda helper: helper.call("ax.extras_owners")) or []
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


def _ocr(ctx: Ctx, obs: Observation, vc: dict[str, Any], sparse: bool = False) -> dict[str, Any] | None:
    """Read the window on-device; the same window content is read once (keyed by its Accessibility fingerprint).

    Except on a canvas, where that key is worthless: an app that draws its own content changes everything on
    screen without one Accessibility node changing, so scrolling a canvas and reading it again would have
    returned the text from before the scroll. There, anything the engine did counts as a change.
    """
    cache: dict[Any, Any] = ctx.cache.setdefault("vision.ocr", {})
    try:
        fp = ctx.helper.call("ax.fingerprint", pid=ctx.app["pid"], poll_nodes=int(vc.get("fingerprint_nodes", 400))).get("fingerprint")
    except HelperError:
        fp = None
    if sparse:
        fp = (fp, ctx.cache.get("actions_done", 0))
    hit = cache.get((ctx.app["pid"], fp)) if fp is not None else None
    if hit is not None:
        obs.notes["vision_cached"] = True
        return hit
    try:
        # no list here: "auto" lets the helper intersect this Mac's languages with what the recogniser
        # supports. The old default was a fixed [zh-Hans, en-US], written out in three places that disagreed.
        wanted = vc.get("languages")
        res = ctx.helper.call("screen.ocr", pid=ctx.app["pid"], near=obs.notes.get("window_frame"),
                              languages=[] if wanted in (None, "auto") else list(wanted),
                              correct=bool(vc.get("language_correction", False)),
                              fast=bool(vc.get("fast", False)), min_conf=float(vc.get("min_conf", 0.3)), timeout=15)
    except HelperError as exc:
        obs.notes["vision_error"] = str(exc)[:200]
        return None
    if fp is not None:
        if len(cache) > 32:
            cache.clear()
        cache[(ctx.app["pid"], fp)] = res
    return res


def _right_click_by_name(ctx: Ctx, obs: Observation, vc: dict[str, Any], boxes: list[dict[str, Any]]) -> None:
    """The secondary button, aimed by naming something that was read off the screen.

    One option, not one per spot: which text to aim at is a slot, the way dragging already works. Offering
    it per spot would double a canvas window's options against the budget that is already the scarce thing.
    Where the tree does describe an element, it offers `AXShowMenu` itself and this is not needed.
    """
    if not boxes or not vc.get("offer_right_click", True):
        return
    spots = {b["text"]: b["frame"] for b in boxes if b.get("text") and b.get("frame")}
    if not spots:
        return
    obs.affordances.append(Affordance(
        f"pc{len(obs.affordances)}", "pointer", "click_named",
        str(vc.get("right_click_label") or "open the context menu on something read from the screen"),
        {"spots": spots, "button": "right"},
        slots={"what": Slot("text", "the text on screen to aim at")}, context=(ctx.app or {}).get("name", "")))


@provider("wheel")
def wheel(ctx: Ctx, obs: Observation) -> None:
    """The scroll wheel, over whichever window is being worked in.

    This deliberately asks nothing about the window first. Deciding "is this a canvas, so does it need the
    wheel?" was tried and both ways of deciding it were wrong when measured: by geometry, one text area
    covering a terminal swallowed all 63 things read from it and the window looked fully described; by text,
    `screen_text` is a capped summary, so 62 of those 63 looked missing from it. A threshold picked to make
    those two come out right would be a guess about windows, which is the kind of knowledge that does not
    belong here.

    There is nothing to decide. The wheel is two options, not one per anything, and whether a given window
    scrolls is answered by scrolling it: the loop already records an action that changed nothing and never
    offers it again. `AXScrollDownByPage` is still offered wherever an app implements it — this is for the
    windows that do not, which is every app that draws its own content.
    """
    wc = ctx.cfg.section("observe.wheel")
    frame = obs.notes.get("window_frame")
    if not ctx.app or not wc.get("enabled", True) or not frame:
        return
    lines = int(wc.get("lines", 5))
    name = ctx.app.get("name", "")
    for how, dy in (("down", -lines), ("up", lines)):
        obs.affordances.append(Affordance(
            f"w{how}", "pointer", "scroll", f"scroll {how} in this window with the wheel",
            {"window_frame": list(frame), "dy": dy}, context=name))


@provider("vision")
def vision(ctx: Ctx, obs: Observation) -> None:
    """For what the Accessibility tree cannot say: read the window on-device (OCR) to name unlabeled controls,
    and, where the tree describes nothing, offer what is on the screen as targets.

    How "the tree describes nothing" is decided used to be a count: fewer than eight actionable nodes in the
    window. That is a number somebody picked, and measuring it against real windows showed what it costs —
    an editor that draws its own text had 477 actionable nodes in its surrounding chrome and none at all in
    the part the task is about, so it counted as well described and the pointer was never offered for the
    one region that needed it.

    What is actually being asked is whether anything can be read that the tree cannot reach, and that is not
    a guess: OCR gives boxes, the tree gives frames, and the boxes inside no frame are the answer. Both
    signals are kept — a window with no tree at all is still a canvas even before anything is read.
    """
    vc = ctx.cfg.section("observe.vision")
    mode = vc.get("mode", "auto")
    if not ctx.app or mode == "never":
        return
    # No window means nothing to read. The capture waits for one that is not there — measured at 1.5 s on
    # an app with none — and an empty Accessibility tree reads as "sparse", so this used to run every time.
    if obs.notes.get("open_windows") == [] or obs.notes.get("window_not_answering"):
        return
    unlabeled = obs.notes.get("unlabeled", [])
    empty = int(obs.notes.get("window_actionable", 0)) < int(vc.get("sparse_below", 8))
    key = f"{ctx.app['pid']}|{obs.window}"
    # what reading this window last told us about it: a window whose content the tree cannot describe stays
    # that way while the app does, and finding that out costs a read
    known_canvas = key in ctx.cache.setdefault("vision.canvas", set())
    if mode == "auto" and not empty and not unlabeled and not known_canvas:
        return
    # "this task asked to read that window" is that task's business; kept globally, one task's choice made
    # every later task OCR the same window for the life of the process
    wanted = ctx.cache.setdefault("vision.wanted", {}).setdefault(ctx.task, set())
    if mode == "auto" and not empty and not known_canvas and vc.get("on_demand", True) and key not in wanted:
        # the tree names most things: reading the screen for the rest costs ~0.3 s, so it happens when asked for
        obs.affordances.append(Affordance("vr", "vision", "reveal", f"read the {len(unlabeled)} controls without a label in this window "
                                          "from the screen (to see what they are)", {"key": key}, context=ctx.app.get("name", "")))
        return
    res = _ocr(ctx, obs, vc, sparse=empty or known_canvas)
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
    # Which of what was read is out of the tree's reach. `taken` is every frame the tree did reach, including
    # the unlabeled controls just named above — naming one does not make its text a separate target.
    taken = [a.target["frame"] for a in obs.affordances if a.channel == "window" and a.target.get("frame")]
    uncovered = [b for b in boxes if b.get("frame") and not any(_inside(b["frame"], t) for t in taken)]
    canvas = len(uncovered) >= int(vc.get("canvas_min_texts", 6))
    obs.notes["vision_uncovered"] = len(uncovered)
    if canvas:
        ctx.cache["vision.canvas"].add(key)
    elif known_canvas:
        ctx.cache["vision.canvas"].discard(key)     # the app grew a tree (or the window changed): stop paying
    if empty or canvas:
        for b in uncovered[: int(vc.get("max_text_targets", 80))]:
            x, y = _center(b["frame"])
            obs.affordances.append(Affordance(f"o{len(obs.affordances)}", "pointer", "click", f"click the text 「{b['text']}」" + (f" at {_where_in(b['frame'], win, grid)}" if grid else ""),
                                              {"x": x, "y": y, "frame": b["frame"]}))
        _right_click_by_name(ctx, obs, vc, boxes)
        # right-to-left text read left-to-right comes back as a different sentence, so which way a line runs
        # is asked of the system (Locale.characterDirection for the languages actually recognised)
        rtl = res.get("direction") == "rtl"
        # only the part the tree could not say: what it could say is already in screen_text, and putting it
        # in twice spends the budget on repeating itself
        lines = [b["text"] for b in sorted(uncovered, key=lambda b: (b["frame"][1] // 12,
                                                                     -b["frame"][0] if rtl else b["frame"][0]))]
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
            # Not hidden knowledge, a safety boundary: a "specifier" parameter is an object reference
            # (`document 1 of application "…"`), which is code, and writing code is what
            # allow.raw_applescript forbids. Measured across 60 apps: 18 commands offerable, 32 needing one.
            # Dropping them silently left the planner to rediscover the same wall; it is told instead.
            obs.notes.setdefault("commands_needing_a_reference", []).append(c["name"])
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


@provider("focus")
def focus(ctx: Ctx, obs: Observation) -> None:
    """Where the keyboard actually goes.

    Every other provider describes one app's windows. Typing at the cursor is the one action whose target is
    in none of them: it lands wherever the *system* focus is, which may be a sheet over this window, a panel
    belonging to another process, or nothing at all. The engine typed into it blind, and the decider was never
    told. `kAXFocusedUIElementAttribute` on the system-wide element is the Mac's own answer.

    What the element **is** is kept; what it **holds** never is. Focus sits in password fields, and this runs
    on every look.
    """
    fc = ctx.cfg.section("observe.focus")
    if not fc.get("enabled", True):
        return
    try:
        # value=False: this must never carry out what the element holds. timeout_ms: the app holding the focus
        # is the one answering, and a slow one costs 1.5 s on every single look.
        r = ctx.helper.call("ax.focused", value=False, actions=False,
                            timeout_ms=int(fc.get("timeout_ms", 250)))
    except HelperError as exc:
        obs.notes["focus_error"] = str(exc)[:120]
        return
    node = r.get("focused")
    if not isinstance(node, dict):
        obs.focused = {"secure_input": bool(r.get("secure_input"))}
        return
    # deliberately not `_label`, which falls back to the element's *value* — that is the one thing this
    # provider must never carry out. What names a field is its title, its description or its placeholder.
    name = next((str(node[k]) for k in ("title", "desc", "placeholder")
                 if node.get(k) and not str(node[k]).startswith("_NS:")), "")
    obs.focused = {"pid": r.get("pid"), "app": r.get("app") or "", "bundle_id": r.get("bundle_id") or "",
                   "role": node.get("role"), "rdesc": node.get("rdesc"), "label": name,
                   "ref": node.get("ref"), "secure_input": bool(r.get("secure_input"))}


def _cursor_note(ctx: Ctx, obs: Observation) -> str:
    """How to describe the cursor's whereabouts in a typing affordance, or "" when nothing is known."""
    f = obs.focused or {}
    if not f or f.get("ref") is None:
        return ""
    if f.get("secure_input"):
        return " — a password field has focus: keystrokes are refused while that is so"
    here = (ctx.app or {}).get("pid")
    if here and f.get("pid") and f["pid"] != here:
        # the decider is choosing among one app's actions; this one would not land there
        return f" — but the cursor is in {f.get('app') or 'another app'}, not {(ctx.app or {}).get('name')}"
    what = f.get("label") or f.get("rdesc") or f.get("role") or ""
    return f" (the cursor is in: {what})" if what else ""


@provider("typing")
def typing(ctx: Ctx, obs: Observation) -> None:
    """Type at the cursor: for editors and canvases that expose no text field (the text comes from the caller)."""
    tc = ctx.cfg.section("observe.typing")
    needs = tc.get("requires_input")   # only when the caller gave text: otherwise it lures the decider into typing junk
    if ctx.app and tc.get("enabled", True) and (not needs or ctx.inputs.get(needs)):
        where = _cursor_note(ctx, obs)
        obs.affordances.append(Affordance("y0", "keys", "type", str(tc.get("label") or "type the given text at the cursor") + where, {},
                                          slots={"text": Slot("text", "the text to type at the cursor")}, context=ctx.app.get("name", "")))
        obs.affordances.append(Affordance("y1", "keys", "type_submit", str(tc.get("submit_label") or "type the given text at the cursor and press Return") + where,
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


@provider("readall")
def readall(ctx: Ctx, obs: Observation) -> None:
    """Read everything this window says, not just what fits on screen.

    `screen_text` is a summary — capped, and only of what the window shows now. A long document, a chat
    backlog, an article: the thing the goal is actually about is mostly below the fold. This reads the
    window's text in full and it becomes a fact of the task, so the answer can be written from it.

    Nothing here is about the web. A browser is an app with a lot of text in it, like any other.
    """
    rc = ctx.cfg.section("observe.readall")
    if not ctx.app or not rc.get("enabled", True):
        return
    if len(obs.screen_text) < int(rc.get("offer_over", 200)):
        return   # the whole of it is already in front of the decider
    obs.affordances.append(Affordance(
        f"t{len(obs.affordances)}", "window", "read_all",
        str(rc.get("label", "read all the text in this window, including what is scrolled out of sight")),
        {"pid": ctx.app["pid"]}, context=ctx.app.get("name", "")))


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

    def scan(helper: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for app in installed_apps(ctx.cfg, helper):
            for s in parse_services(app.get("path") or ""):
                out.append({**s, "app": app.get("name") or ""})
        return out

    found = _refresh_in_background(ctx, "services.all", float(sc.get("cache_s", 900)), scan) or []
    for s in found[: int(sc.get("limit", 80))]:
        # What the service accepts is quoted from its own declaration rather than interpreted for it. The
        # names below are Apple's pasteboard vocabulary (the same kind of thing as kAXPressAction), not
        # knowledge about anybody's app; where they say "a file", the slot asks for a path.
        sends = [str(x) for x in s.get("sends") or []]
        wants_file = any("file" in x.lower() or "url" in x.lower() for x in sends)
        slot = "file" if wants_file else "text"
        asks = f"{'the path of the file' if wants_file else 'the text'} to hand over"
        obs.affordances.append(Affordance(
            f"v{len(obs.affordances)}", "service", "perform", f"hand content to {s['app']}: 「{s['name']}」",
            {"name": s["name"]}, slots={slot: Slot("text", f"{asks} (it accepts {', '.join(sends) or 'anything'})")},
            context=f"{s['app']} (a system service)"))


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


def _openers(ctx: Ctx, path: Path) -> list[dict[str, Any]]:
    """The apps this Mac would open a file with, asked of the system rather than guessed from the extension.
    The default one is first, and it is left out here: opening with it is the plain "open" above."""
    if path.is_dir():
        return []
    try:
        apps = ctx.helper.call("apps.openers", path=str(path),
                               limit=int(ctx.cfg.get("observe.files.openers", 4)) + 1) or []
    except HelperError:
        return []      # an older helper: the plain "open" is still there
    return [a for a in apps if not a.get("default")][: int(ctx.cfg.get("observe.files.openers", 4))]


@provider("files")
def files(ctx: Ctx, obs: Observation) -> None:
    fc = ctx.cfg.section("observe.files")
    if ctx.inputs.get("url"):
        obs.affordances.append(Affordance("u0", "file", "open", f"open {ctx.inputs['url']} in the default app/browser", {"path": str(ctx.inputs["url"])}))
    # A path the user named is a thing on this Mac, and the Mac can be asked about it: if it exists, the
    # engine can open it, show where it is, or read it, with no window to drive at all. Three real tasks
    # failed for want of this — each one opened Finder's "Go to Folder" and typed a long path instead.
    named = 0
    for value in [ctx.goal, *(str(v) for v in ctx.inputs.values())]:
        for raw in re.findall(r"(?:~|/)[^\s\u4e00-\u9fff，。、：；？！「」（）]+", str(value or "")):
            here = Path(raw.rstrip(".,;:").replace("\\ ", " ")).expanduser()
            if not here.exists() or str(here) in {a.target.get("path") for a in obs.affordances}:
                continue
            what = "folder" if here.is_dir() else "file"
            obs.affordances.append(Affordance(f"p{named}", "file", "open",
                                              f"open the {what} {here} in whichever app this Mac opens it with",
                                              {"path": str(here)}))
            # …and in any of the apps the system says can open it. A goal that names one ("open it in Safari")
            # can only be followed if that is an option: with just the line above, a real task opened the page
            # in the default browser and the goal was not met.
            for j, opener in enumerate(_openers(ctx, here)):
                obs.affordances.append(Affordance(
                    f"p{named}o{j}", "file", "open", f"open {here.name} with {opener['name']}",
                    {"path": str(here), "app": opener.get("path"), "bundle_id": opener.get("bundle_id")}))
            obs.affordances.append(Affordance(f"P{named}", "file", "reveal", f"show where {here} is on disk", {"path": str(here)}))
            if here.is_file():
                obs.affordances.append(Affordance(f"T{named}", "file", "read", f"read the text of {here}", {"path": str(here)}))
            # The Mac can put a file in the Trash without Finder being driven at all. It is offered like any
            # other action and gated like any other: the floor classifies it, and the user is asked first.
            obs.affordances.append(Affordance(f"X{named}", "file", "trash",
                                              f"move the {what} {here} to the Trash", {"path": str(here)}))
            named += 1
            if named >= int(fc.get("named_limit", 4)):
                break

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
        # "in Finder" was the one app name written into this repo, and it is 访达 on this Mac. The action is
        # `open -R`, which asks the system to reveal the file — the system picks who does that. Say what it
        # does, not who it opens.
        obs.affordances.append(Affordance(f"F{i}", "file", "reveal", f"show where 「{pp.name}」 is on disk", {"path": p}))
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
             "vision": "reading the screen"}
    return f"ch:{a.channel}", names.get(a.channel, f"the {a.channel} actions")


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
    # Smallest groups first is not a guess at what matters — it is what shows the most *distinct* places at
    # once; the largest become one "look into …" each, so nothing is dropped for being judged uninteresting.
    # What can still be dropped is the tail of this list when even that does not fit, and the caller is told.
    return flat[: max(0, budget - len(folded))], folded


def _disambiguate(affs: list[Affordance]) -> None:
    """Where several actions share one Accessibility identifier, add their labels — and only there.

    Apps reuse a selector for a whole dynamic list (`_recentItemRequested:` for every recent file). Those
    cannot be told apart without their text, so they fall back to being language-bound; everything with an
    identifier of its own stays language-independent.
    """
    seen: dict[str, int] = {}
    for a in affs:
        if a.key:
            seen[a.key] = seen.get(a.key, 0) + 1
    for a in affs:
        if a.key and seen[a.key] > 1:
            a.key = f"{a.key}|{a.label}"


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
    _disambiguate(obs.affordances)
    ids = [a.id for a in obs.affordances]
    if len(ids) != len(set(ids)):
        for i, a in enumerate(obs.affordances):   # providers are independent; make ids unique afterwards
            a.id = f"{a.id}_{i}"
    obs.notes["ms"] = round((time.monotonic() - t0) * 1000)
    return obs
