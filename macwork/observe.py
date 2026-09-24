"""Providers turn the current Mac into affordances. Each is a function ``(ctx, obs) -> None`` that appends to
the observation; register more with ``@provider("name")`` or the ``macwork.providers`` entry point and
list them in ``observe.providers``. Nothing here names an app — everything comes from what is installed,
running and on screen.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .helper import Helper, HelperError
from .appmodel import parse_services
from .model import Affordance, Observation, Slot, clip, with_state, TaskScope
from .onscreen import app_windows, input_method_panel, still_launching, unreadable

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
    scope: TaskScope = field(default_factory=TaskScope)   # that task's live working state (see model.TaskScope)


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


def known_apps(ctx: Ctx) -> list[dict[str, Any]]:
    """The apps on this Mac, the running ones first: what the apps provider offers, from the same cache."""
    installed = _cached(ctx, "apps.installed", 600, lambda: installed_apps(ctx.cfg, ctx.helper)) \
        if ctx.cfg.get("observe.apps.include_installed", True) else []
    running = {a.get("path") for a in ctx.running}
    return list(ctx.running) + [a for a in installed or [] if a.get("path") not in running]


def names_of(app: dict[str, Any]) -> set[str]:
    """What a person calls an app: the name the Mac shows for it, in the Mac's language, and its file name."""
    path = str(app.get("path") or "")
    file = app.get("file") or (path.rsplit("/", 1)[-1].removesuffix(".app") if path else "")
    return {n for n in (" ".join(str(x or "").split()) for x in (app.get("name"), file)) if len(n) >= 2}


def _latin(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def apps_named(text: str, apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The apps a sentence calls by name, in the order it names them — asked of the Mac's own list, so
    nothing here knows any app.

    A name counts where it stands as one: a Latin name not run into a longer word ("Maps" in "roadmaps") or
    into a file name ("News" in "news.html"), and a name lying inside a longer name found at the same place
    is that longer name ("App" in "App Store", 「信息」 in 「系统信息」). A script written without spaces has
    no word edges to look for, so there a name is found wherever it is written — 「在词典里查」 names 词典.
    """
    hay = " ".join(str(text or "").split()).casefold()
    found: list[tuple[int, int, dict[str, Any]]] = []
    for app in apps:
        for name in names_of(app):
            n = name.casefold()
            i = hay.find(n)
            while i != -1:
                j = i + len(n)
                before, after, beyond = hay[i - 1:i] or " ", hay[j:j + 1] or " ", hay[j + 1:j + 2] or " "
                run_in = (_latin(n[0]) and _latin(before)) or \
                    (_latin(n[-1]) and (_latin(after) or (after == "." and _latin(beyond))))
                if not run_in:
                    found.append((i, j, app))
                i = hay.find(n, i + 1)
    whole = [f for f in found if not any(o[0] <= f[0] and f[1] <= o[1] and o[1] - o[0] > f[1] - f[0] for o in found)]
    out: list[dict[str, Any]] = []
    for _i, _j, app in sorted(whole, key=lambda f: f[0]):
        if not any(app is seen for seen in out):
            out.append(app)
    return out


# ----------------------------------------------------------------------------- apps
@provider("apps")
def apps(ctx: Ctx, obs: Observation) -> None:
    here = (ctx.app or {}).get("pid")
    # An app the goal calls by name is a thing on this Mac, the way a path it names is one. It was one of 400
    # behind "look into open another app", and the planner, which is never shown that group, was never told
    # 「词典」 is an app here: it read 「在词典里查 serendipity」 as "look it up on a dictionary website", and
    # six real runs of that task went to the person's own browser instead.
    named = {k for k in (a.get("path") or a.get("bundle_id") for a in apps_named(ctx.goal, known_apps(ctx))) if k}
    running_paths = set()
    for i, a in enumerate(ctx.running):
        running_paths.add(a.get("path"))
        if a.get("pid") == here or not a.get("name"):
            continue
        obs.affordances.append(Affordance(f"a{i}", "app", "activate", f"switch to app {a['name']}",
                                          {"pid": a["pid"], "bundle_id": a.get("bundle_id"), "name": a["name"],
                                           **({"named": True} if (a.get("path") or a.get("bundle_id")) in named else {})}))
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
                                          {"path": a["path"], "bundle_id": a.get("bundle_id"), "name": a["name"],
                                           **({"named": True} if (a.get("path") or a.get("bundle_id")) in named else {})}))


# ----------------------------------------------------------------------------- menu bar
_MODS = [(1, "⇧"), (2, "⌥"), (4, "⌃")]
# A key equivalent that prints nothing, as AppKit reports it (AXMenuItemCmdChar: an ASCII control, or one of
# NSEvent's function-key characters, U+F700 on), shown the way the Mac's own menus show it. Apple's glyphs,
# not names: the name "delete" in a label is a hit on the floor's own word for deleting.
_KEY_GLYPHS = {"\x03": "⌤", "\x08": "⌫", "\t": "⇥", "\r": "↩", "\x19": "⇤", "\x1b": "⎋", " ": "␣", "\x7f": "⌫",
               "\uf700": "↑", "\uf701": "↓", "\uf702": "←", "\uf703": "→", "\uf728": "⌦", "\uf729": "↖",
               "\uf72b": "↘", "\uf72c": "⇞", "\uf72d": "⇟", "\uf739": "⌧",
               **{chr(0xF704 + i): f"F{i + 1}" for i in range(35)}}


def _shortcut(cmd: dict[str, Any] | None) -> str:
    """The menu item's key equivalent as its menu shows it: 「 (⇧⌘S)」, 「 (⌘⌫)」.

    What prints nothing was written as the raw character AppKit reports for it: 109 menu items in 11 apps of
    this Mac's audit carried one into the labels the decider and the floor read ('menu 文件 ▸ 移到废纸篓
    (⌘\\x08)'), ten of them bound to a delete key. One the Mac has no glyph for is left out rather than
    written raw."""
    if not cmd or not cmd.get("char"):
        return ""
    ch = str(cmd["char"])
    shown = _KEY_GLYPHS.get(ch, ch if ch.isprintable() else "")
    if not shown:
        return ""
    m = int(cmd.get("mods") or 0)
    keys = "".join(sym for bit, sym in _MODS if m & bit) + ("" if m & 8 else "⌘")
    return f" ({keys}{shown})"


_COMBO_MODS = [(8, None), (4, "ctrl"), (2, "alt"), (1, "shift")]


def _combo(cmd: dict[str, Any] | None) -> str | None:
    """The menu item's own key equivalent as a key combo (only printable keys; glyph keys are left to the press)."""
    ch = str((cmd or {}).get("char") or "")
    if len(ch) != 1 or not ch.isprintable() or ch.isspace():
        return None
    m = int((cmd or {}).get("mods") or 0)
    mods = ([] if m & 8 else ["cmd"]) + [name for bit, name in _COMBO_MODS if name and m & bit]
    return "+".join(mods + [ch.lower()]) if mods else None


# Keys as they get written: modifiers by name or by the glyph the Mac's menus show (⌘ ⌃ ⌥ ⇧), named keys by
# name, alias or glyph (↩ ⎋ ⌫ …), each read as the name input.key takes. macOS numbers F-keys up to F20; the
# helper's own key table stops at F12.
_COMBO_ORDER = ("cmd", "ctrl", "alt", "shift", "fn")
_MOD_WORDS = {"cmd": "cmd", "command": "cmd", "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt",
              "opt": "alt", "shift": "shift", "fn": "fn"}
_MOD_GLYPHS = {"⌘": "cmd", "⌃": "ctrl", "⌥": "alt", "⇧": "shift"}
_KEY_ALIASES = {"enter": "return", "esc": "escape", "backspace": "delete", "↩": "return", "⎋": "escape", "⇥": "tab",
                "⌫": "delete", "⌦": "forwarddelete", "←": "left", "→": "right", "↑": "up", "↓": "down",
                "↖": "home", "↘": "end", "⇞": "pageup", "⇟": "pagedown"}
_NAMED_KEYS = frozenset({"return", "escape", "tab", "space", "delete", "forwarddelete", "up", "down", "left", "right",
                         "home", "end", "pageup", "pagedown"} | {f"f{i}" for i in range(1, 21)})
_LABEL_WORDS = frozenset({"press", "the", "key"})      # our own option wording ("press the return key"), said back


def canonical_combo(said: Any) -> str | None:
    """A key as the keyboard reads it — `cmd+shift+g` for "⇧⌘G", "shift+cmd+g", "Cmd + Shift + G" alike — or
    None where it is not one.

    Modifiers come in `_combo`'s order (cmd, ctrl, alt, shift, then fn, which is never dropped: fn+delete is
    another key than delete). The key is one printable character or a named physical key. A named key alone
    is a key; a character alone is typing, not a key; and what the helper could not split into modifiers
    and a key — "cmd++", "menu item 「General」" — is None, not a guess.
    """
    return _canonical(str(said or ""))


@lru_cache(maxsize=4096)
def _canonical(said: str) -> str | None:
    # named_combo reads every menu item's combo again for each key it is asked about, and the same few hundred
    # strings come back on every look: the 13 physical keys over 400 menu items took 6.2 ms a look read afresh,
    # 0.5 ms remembered (0.4 ms when combos were compared as written)
    words = [w for w in re.sub(r"\s*\+\s*", "+", said.strip().lower()).split() if w not in _LABEL_WORDS]
    if len(words) != 1:
        return None
    *mods, key = words[0].split("+")
    held: set[str] = set()
    for m in mods:
        if m in _MOD_WORDS:
            held.add(_MOD_WORDS[m])
        elif m and all(g in _MOD_GLYPHS for g in m):
            held.update(_MOD_GLYPHS[g] for g in m)
        else:
            return None
    while key[:1] in _MOD_GLYPHS:                      # "⇧⌘g": the menus' own way of writing it
        held.add(_MOD_GLYPHS[key[0]])
        key = key[1:]
    key = _KEY_ALIASES.get(key, key)
    if key not in _NAMED_KEYS and not (held and len(key) == 1 and key.isprintable() and not key.isspace()):
        return None
    return "+".join([m for m in _COMBO_ORDER if m in held] + [key])


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
                    # where it sits: `path` as the menus spell it (what a key is named by, see named_combo),
                    # `menu_path` as the titles from the top menu down
                    place = path + [title]
                    obs.affordances.append(Affordance(f"m{len(obs.affordances)}", "menu", "press",
                                                      f"menu {' ▸ '.join(place)}{checked}{_shortcut(n.get('cmd'))}",
                                                      {"ref": c, "pid": ctx.app["pid"], "combo": _combo(n.get("cmd")), "title": title,
                                                       "path": " ▸ ".join(place), "menu_path": place},
                                                      context=' ▸ '.join(path[:1]),
                                                      key=_identity(ident, n.get("role"), n.get("subrole"), title),
                                                      # the selector behind the command, as the app names it; a
                                                      # "_NS:" one is AppKit's numbering where the app gave no name
                                                      facts={"identifier": ident} if ident and not ident.startswith("_NS:") else {}))
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


def _structural_identity(n: dict[str, Any], by_ref: dict[str, dict[str, Any]], kids: dict[str, list[str]],
                         content_roles: set[str]) -> str:
    """An identity from where a control sits, for controls the app gave no identifier.

    Most window controls carry no Accessibility identifier, and there the label was the only identity —
    which is the interface language, and what the control happens to show. Where the control *is* does not
    change with either: the chain of roles from the window down to it, and its place among siblings of
    the same role at each level. Two identical toolbars give two different paths; a relabelled button gives
    the same one. Content — rows, cells, images, links, text — is left out: a row's place changes with every
    sort and its text is what it is, so its name stays its identity.
    """
    role = n.get("role") or ""
    if not role or role in content_roles:
        return ""
    path: list[str] = []
    ref, hops = n.get("ref"), 0
    while ref and ref in by_ref and hops < 40:
        node = by_ref[ref]
        r, parent = node.get("role") or "", node.get("parent")
        if r in content_roles:
            return ""          # inside content (a button in a row): the row's place is not stable, so neither is this
        same = [c for c in kids.get(parent, [])] if parent else [ref]
        index = [by_ref[c].get("role") for c in same if c in by_ref].count(r) and \
            [c for c in same if c in by_ref and by_ref[c].get("role") == r].index(ref)
        path.append(f"{r}[{index}]")
        ref, hops = parent, hops + 1
    return "axpath|" + "/".join(reversed(path))


def _label(n: dict[str, Any]) -> tuple[str, str]:
    """What names an element, and the attribute that name came from: ("", "") when nothing does. "help" is a
    tooltip, the one name that is not the control's own words (see `element_affordances`)."""
    for k in ("title", "desc", "value", "placeholder", "help"):
        v = n.get(k)
        if v and not str(v).startswith("_NS:"):
            return str(v), k
    return "", ""


def _kind(n: dict[str, Any]) -> str:
    """An element's role and subrole as the Mac declares them: 'AXWindow/AXStandardWindow', 'AXSheet'."""
    role, sub = str(n.get("role") or ""), str(n.get("subrole") or "")
    return f"{role}/{sub}" if role and sub else role


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
    content_roles = set(wcfg.get("content_roles") or [])
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

    # Where an element sits, as the Mac declares it: the nearest sheet or window above it, and whether a web
    # page lies on the way, where the page writes the roles and identifiers itself. Worked out once per node.
    placed: dict[str, tuple[str, bool]] = {}

    def place(ref: str) -> tuple[str, bool]:
        if ref not in placed:
            placed[ref] = ("", False)              # a tree that led back to itself would stop here
            up = by_ref.get(by_ref.get(ref, {}).get("parent"))
            if up is not None:
                holder, web = place(up["ref"])
                placed[ref] = (_kind(up) if up.get("role") in ("AXSheet", "AXWindow") else holder,
                               web or up.get("role") == "AXWebArea")
        return placed[ref]

    def declared(n: dict[str, Any]) -> dict[str, Any]:
        """What the Mac declares about an element, for every action it offers (`Affordance.facts`). An
        identifier only where the app wrote it for a control: content builds its identifiers from its data
        (content_roles), and a page writes its own."""
        holder, web = place(n["ref"])
        web = web or n.get("role") == "AXWebArea"
        ident = str(n.get("ident") or "")
        out: dict[str, Any] = {"control": _kind(n)}
        if holder:
            out["in"] = holder
        if ident and not ident.startswith("_NS:") and n.get("role") not in content_roles and not web:
            out["identifier"] = ident
        if web:
            out["_web_content"] = True            # never sent: see PolicyMixin._declared
        return out

    for n in nodes:
        role = n.get("role", "")
        rd = n.get("rdesc") or role.removeprefix("AX").lower()
        if n.get("enabled", True) is False:
            continue
        own, source = _label(n)
        ikey = _identity(n.get("ident") or "", role, n.get("subrole"), own) \
            or _structural_identity(n, by_ref, kids, content_roles)   # "" for content, whose name is its identity
        if n["ref"] in in_row and (role in text_roles or role in read_roles or role in ("AXCell", "AXImage", "AXGroup")):
            continue
        ctx_text = _context_of(n, by_ref) or where
        facts = declared(n)
        # A control named only by its tooltip is shown by it, but the tooltip is not the control's own words:
        # 「此按钮也可以执行缩放窗口的操作」 names the full-screen button, and its 执行 is a floor word for running
        # code, which raised the bar the button had to clear to 0.9: 29 of the 42 verdicts recorded for it since
        # 09-22 05:00 were gated there, and none would have been at the 0.7 an unflagged action has to clear. The
        # floor's words are matched on the label as it would read without the tooltip, `floor_text`, made here:
        # a label cut to fit could not have the tooltip taken back out of it.
        tooltip = source == "help"
        if selectable(n, role):   # rows are chosen by selecting them, not by an action
            shown = " · ".join(dict.fromkeys(inner_text(n["ref"])))
            name = own or shown
            if name:
                state = "selected" if n.get("selected") else ""
                target = {"ref": n["ref"], "pid": ctx.app["pid"], "frame": n.get("frame")}
                if tooltip:
                    target["floor_text"] = with_state(f"select {rd}" + (f" 「{shown[:80]}」" if shown else ""), state)
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select",
                                                  with_state(f"select {rd} 「{name[:80]}」", state),
                                                  target, context=ctx_text, key=ikey, facts=dict(facts)))
        if role in text_roles and n.get("editable") is False and not by_capability:   # shows text, cannot be typed into
            role = "AXStaticText"
        if typeable(n, role):
            label = n.get("title") or n.get("desc") or n.get("placeholder") or ctx_text or rd
            current = f"now: {str(n['value'])[:60]}" if n.get("value") and role != "AXSecureTextField" else ""
            target = {"ref": n["ref"], "pid": ctx.app["pid"], "secure": role == "AXSecureTextField", "frame": n.get("frame")}
            if n.get("value") and role != "AXSecureTextField":   # what fields hold is the best evidence that typing worked
                obs.notes.setdefault("fields", []).append(f"{label}: {str(n['value'])[: int(wcfg.get('field_chars', 300))]}")
            obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type",
                                              with_state(f"type into {rd} 「{label}」", current),
                                              target, slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text, key=ikey,
                                              facts=dict(facts)))
            if has_range(n, role) and n.get("value") and n.get("editable") is not False:
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "select_text",
                                                  f"select part of the text in {rd} 「{label}」", dict(target),
                                                  slots={"selection": Slot("text", "the exact text to select, as it appears there")}, context=ctx_text, key=ikey,
                                                  facts=dict(facts)))
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "cursor_end",
                                                  f"put the cursor at the end of the text in {rd} 「{label}」", dict(target), context=ctx_text, key=ikey,
                                                  facts=dict(facts)))
            if role in set(wcfg.get("submit_roles") or []):
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "type_submit",
                                                  with_state(f"type into {rd} 「{label}」 and press Return", current), dict(target),
                                                  slots={"text": Slot("text", f"what to type into 「{label}」")}, context=ctx_text, key=ikey,
                                                  facts=dict(facts)))
            # An element can be both: a table cell takes text *and* has a context menu. Probing capabilities
            # finds many more typing targets than the role list did, so swallowing their actions here would
            # quietly take away what they could already do.
            if not (n.get("actions") and by_capability):
                continue
        # a subrole (close button, sort button…) makes the role description itself a name
        by_subrole = str(n["rdesc"]) if n.get("subrole") and n.get("rdesc") else ""
        label = own or by_subrole
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
            state = "selected" if n.get("selected") else ""
            for act in offered:
                verb = labels.get(act) or str((n.get("action_desc") or {}).get(act) or "")   # the app's own word
                goes = f" → {n['url']}" if n.get("url") else ""   # a link's target, from the app itself
                text = with_state(f"{verb + ' ' if verb else ''}{rd} 「{label}」{goes}", state)
                target = {"ref": n["ref"], "pid": ctx.app["pid"], "action": act, "frame": n.get("frame"), "title": label}
                if tooltip:
                    target["floor_text"] = with_state(f"{verb + ' ' if verb else ''}{rd}"
                                                      + (f" 「{by_subrole}」" if by_subrole else "") + goes, state)
                obs.affordances.append(Affordance(f"{prefix}{len(obs.affordances)}", "window", "press", text,
                                                  target, context=ctx_text, key=ikey, facts=dict(facts)))
        if role in read_roles or (n.get("role") in text_roles and n.get("editable") is False):
            t = str(n.get("value") or n.get("title") or n.get("desc") or "").strip()
            if t and n.get("url"):   # where a link goes is the thing worth knowing about it
                t = f"{t} → {n['url']}"
            if t and t not in seen_text:
                seen_text.add(t)
                texts.append(t)
    if texts:
        _more_text(ctx, obs, texts)


def undescribed_share(nodes: list[dict[str, Any]], win: list[int] | None, actions: set[str], text_roles: set[str], cells: int = 48) -> float:
    """How much of the window's area no node the tree *describes* covers — a fact about this window, measured.

    A node describes something when it can be acted on, is a field, or is a leaf that carries a name or a
    value. A container with children is what its children are; a container with nothing inside describes
    nothing, whatever its size — that is exactly the scroll area a spreadsheet draws its cells into, and the
    web area of a page that is still loading. The union of the described frames is rasterised over the window
    (cells a side) rather than computed exactly: 48×48 is finer than any decision made on it.
    """
    if not win or len(win) != 4 or not win[2] or not win[3]:
        return 0.0
    by_ref, kids = _tree(nodes)
    x0, y0, w, h = float(win[0]), float(win[1]), float(win[2]), float(win[3])
    covered = [[False] * cells for _ in range(cells)]
    for n in nodes:
        f = n.get("frame")
        if not f or len(f) != 4 or n.get("role") == "AXWindow":
            continue
        described = bool(actions & set(n.get("actions") or [])) or n.get("role") in text_roles or \
            (not kids.get(n["ref"]) and any(n.get(k) for k in ("title", "value", "description", "placeholder")))
        if not described:
            continue
        cx0 = max(0, int((float(f[0]) - x0) / w * cells)); cy0 = max(0, int((float(f[1]) - y0) / h * cells))
        cx1 = min(cells, int(math.ceil((float(f[0]) + float(f[2]) - x0) / w * cells)))
        cy1 = min(cells, int(math.ceil((float(f[1]) + float(f[3]) - y0) / h * cells)))
        for cy in range(cy0, cy1):
            row = covered[cy]
            for cx in range(cx0, cx1):
                row[cx] = True
    seen = sum(sum(row) for row in covered)
    return round(1.0 - seen / float(cells * cells), 3)


def undescribed_frame(nodes: list[dict[str, Any]], win: list[int] | None, actions: set[str], text_roles: set[str],
                      cells: int = 48) -> list[int] | None:
    """Where the undescribed part of the window is: the bounding box of the rows of the raster that are mostly
    uncovered. A toolbar row is mostly covered and stays out; the grid below it is in. Nothing here knows what
    a toolbar or a grid is — only which rows the tree describes and which it does not."""
    if not win or len(win) != 4 or not win[2] or not win[3]:
        return None
    by_ref, kids = _tree(nodes)
    x0, y0, w, h = float(win[0]), float(win[1]), float(win[2]), float(win[3])
    covered = [[False] * cells for _ in range(cells)]

    def described(n: dict[str, Any]) -> bool:
        return bool(actions & set(n.get("actions") or [])) or n.get("role") in text_roles or \
            (not kids.get(n["ref"]) and any(n.get(k) for k in ("title", "value", "description", "placeholder")))
    # For *where* the chrome is, a container whose children are described covers its whole area: a toolbar's
    # buttons are small and spaced, and by their own frames its row is mostly blank — but the toolbar as a
    # node is not. (For the share, that container counts for nothing: its children already count.)
    for n in nodes:
        f = n.get("frame")
        if not f or len(f) != 4 or n.get("role") == "AXWindow":
            continue
        if not (described(n) or any(described(by_ref[k]) for k in kids.get(n["ref"], []) if k in by_ref)):
            continue
        if float(f[2]) * float(f[3]) >= 0.9 * w * h:     # a node the size of the window says nothing about where
            continue
        cx0 = max(0, int((float(f[0]) - x0) / w * cells)); cy0 = max(0, int((float(f[1]) - y0) / h * cells))
        cx1 = min(cells, int(math.ceil((float(f[0]) + float(f[2]) - x0) / w * cells)))
        cy1 = min(cells, int(math.ceil((float(f[1]) + float(f[3]) - y0) / h * cells)))
        for cy in range(cy0, cy1):
            for cx in range(cx0, cx1):
                covered[cy][cx] = True
    rows = [cy for cy in range(cells) if sum(covered[cy]) <= cells // 2]
    if not rows:
        return None
    cols = [cx for cx in range(cells) if any(not covered[cy][cx] for cy in rows)]
    top, bottom, left, right = min(rows), max(rows) + 1, min(cols), max(cols) + 1
    return [int(x0 + left / cells * w), int(y0 + top / cells * h), int((right - left) / cells * w), int((bottom - top) / cells * h)]


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


def _more_text(ctx: Ctx, obs: Observation, lines: list[str]) -> None:
    """Add to the screen text, up to the cap — and say when the cap cut something.

    Three providers wrote to one pool, each cutting it to the cap as it went, and none said so: a prompt read
    after a long window lost its words, and the decider was told nothing was missing. The cap stays (it is
    what the decider reads on every step); the cut is a fact about the observation, and is noted.
    """
    cap = int(ctx.cfg.get("observe.window.screen_text_chars", 1500))
    joined = "\n".join(filter(None, [obs.screen_text] + list(lines)))
    if len(joined) > cap:
        obs.notes["screen_text_cut"] = int(obs.notes.get("screen_text_cut", 0)) + len(joined) - cap
    obs.screen_text = joined[:cap]


def _note_ax_trust(ctx: Ctx, obs: Observation) -> None:
    """An empty tree can mean no window, or no permission — and the two looked the same: `open_windows: []`,
    which then switched reading the screen off as well. Asked once, and again after a refusal, since the
    permission may have been granted meanwhile."""
    trusted = ctx.cache.get("ax_trusted")
    if trusted is not True:
        try:
            trusted = bool(ctx.helper.call("ping", timeout=5).get("ax_trusted", True))
        except HelperError:
            return
        ctx.cache["ax_trusted"] = trusted
    if not trusted:
        obs.notes["ax_trusted"] = False


def _own_prompts(ctx: Ctx, obs: Observation, nodes: list[dict[str, Any]], n0: int) -> None:
    """A sheet or dialog the app itself put up — a recovery notice, "keep this window?", an alert — handed
    to the loop as an interruption, exactly like a prompt from another process.

    Only windows of *other* processes were. An app's own dialog was poured in with the app's controls, and
    a real task met a recovery prompt that way: the planner called it a thing only the user could dismiss,
    and the task ended `blocked` with the question already answered. What the dialog is — the task itself,
    something to set aside, or the user's — is the same three-way judgement, made the same way, from the
    goal and from what the floor says each button does; nothing here knows what any dialog says.
    """
    if not nodes or not ctx.app:
        return
    by_ref = {n["ref"]: n for n in nodes if n.get("ref")}
    roots = [n for n in nodes if n.get("role") == "AXSheet"
             or (n.get("role") == "AXWindow" and n.get("subrole") in ("AXDialog", "AXSystemDialog"))]
    if not roots:
        return

    def within(ref: Any, root: str) -> bool:
        hops = 0
        while ref and hops < 64:
            if ref == root:
                return True
            ref = (by_ref.get(ref) or {}).get("parent")
            hops += 1
        return False

    for root in roots:
        inside = [n for n in nodes if within(n.get("ref"), root["ref"])]
        words = [str(n.get(k)) for n in inside if n.get("role") in ("AXStaticText", "AXHeading", "AXSheet", "AXWindow")
                 for k in ("title", "value", "desc") if n.get(k)]
        text = " / ".join(dict.fromkeys(words))[:400]
        if not text:
            continue
        name = str(ctx.app.get("name") or "")
        key = f"{name}|{hashlib.sha1(text.encode()).hexdigest()[:10]}"
        where = "a sheet of the app itself" if root.get("role") == "AXSheet" else "a dialog of the app itself"
        obs.notes.setdefault("interruptions", []).append({"key": key, "from": name, "text": text, "where": where,
                                                          "frame": root.get("frame"), "over": True})
        obs.notes.setdefault("covered_by", []).append({"from": name, "text": text, "where": where})
        for a in obs.affordances[n0:]:
            if within(a.target.get("ref"), root["ref"]):
                a.target["interruption"] = key
                a.target["watch"] = ctx.app["pid"]


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
    if not nodes:
        _note_ax_trust(ctx, obs)
    if nodes and nodes[0].get("role") == "AXWindow":
        obs.window = nodes[0].get("title") or obs.window
        obs.notes["window_frame"] = nodes[0].get("frame")
        if nodes[0].get("frame"):
            ctx.cache.setdefault("window_frame_by_pid", {})[ctx.app["pid"]] = nodes[0]["frame"]
        if nodes[0].get("document"):      # the file this window is showing, as the app itself reports it
            obs.notes["window_document"] = nodes[0]["document"]
        _note_window_kind(obs, s, nodes)
    n0 = len(obs.affordances)
    element_affordances(ctx, obs, nodes, "w")
    _own_prompts(ctx, obs, nodes, n0)
    obs.notes["window_actionable"] = count(nodes)
    if nodes and obs.notes.get("window_frame"):
        # how much of the window the tree says nothing about: the one signal that does not depend on how
        # the app happened to structure its chrome (see `vision`)
        cells = int(ctx.cfg.get("observe.vision.coverage_cells", 48))
        obs.notes["undescribed_share"] = undescribed_share(nodes, obs.notes["window_frame"], actions, text_roles, cells)
        region = undescribed_frame(nodes, obs.notes["window_frame"], actions, text_roles, cells)
        if region:
            obs.notes["undescribed_frame"] = region
    if s.get("not_answering"):   # the app did not answer Accessibility in time; the helper will not ask again soon
        obs.notes["window_not_answering"] = True
    _note_launching(ctx, obs, s)
    obs.notes["window_ms"] = s.get("ms")
    obs.notes["window_truncated"] = s.get("truncated")


def _note_window_kind(obs: Observation, s: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    """What kind of window a key pressed now goes to, as the Mac declares it, and which buttons Return and
    Escape press there.

    - `window_kind`: the role and subrole of the sheet in front if one is up ('AXSheet'), else of the window
      ('AXWindow/AXStandardWindow', 'AXWindow/AXDialog'). A key was judged by its name alone, so its first
      verdict in an app served every window of it, and every sheet.
    - 'a window read only in part' when the walk was cut and no sheet was seen. A sheet is among the window's
      last children and the walk reaches it last, so a cut walk loses it first: a window read in part is not
      known to be the window without the sheet.
    - `default_button` and `cancel_button`: the title of the button that sheet or window names as the one
      Return or Escape presses (helper 0.2.0), never its tooltip. An older helper names none, and neither does
      a walk cut before it reached the button; a disabled button is not pressed by its key."""
    by_ref = {n.get("ref"): n for n in nodes}

    def depth(n: dict[str, Any]) -> int:
        d, p = 0, n.get("parent")
        while p in by_ref and d < 64:
            d, p = d + 1, by_ref[p].get("parent")
        return d
    sheets = [(depth(n), i, n) for i, n in enumerate(nodes) if n.get("role") == "AXSheet"]
    if sheets:
        holder = max(sheets, key=lambda x: x[:2])[2]    # a sheet on a sheet: the one in front
    elif s.get("truncated") or nodes[0].get("more_children"):
        obs.notes["window_kind"] = "a window read only in part"
        return
    else:
        holder = nodes[0]
    obs.notes["window_kind"] = _kind(holder)
    for key in ("default_button", "cancel_button"):
        button = by_ref.get(holder.get(key)) or {}
        title = str(button.get("title") or "").strip()
        if title and button.get("enabled", True) is not False:
            obs.notes[key] = title


def _note_launching(ctx: Ctx, obs: Observation, said: dict[str, Any]) -> None:
    """An app that has not finished launching may have no window *yet*: not the same as having none. The
    helper says so (0.2.0), believed for engine.open_front_s after the launch (`onscreen.still_launching`)."""
    if still_launching(said, ctx.running, ctx.app["pid"], float(ctx.cfg.get("engine.open_front_s", 6))):
        obs.notes["app_launching"] = True


@provider("windows")
def windows(ctx: Ctx, obs: Observation) -> None:
    """Every window of the app, not just the focused one: the thing needed is often in another window."""
    if not ctx.app:
        return
    s = ctx.helper.call("ax.snapshot", pid=ctx.app["pid"], scope="windows", max_depth=0, max_nodes=50, actions=False)
    _note_launching(ctx, obs, s)
    if s.get("not_answering"):
        # An app that did not answer has told nothing about its windows, and an empty list here used to say
        # it had none: the decider read "none: the app has no window open" and was offered "bring back the
        # main window" for an app that was starting or busy (reopen was chosen on 45 of the 158 such looks
        # since 22ce357, against 49 of 2,338 answering ones). The window server needs no answer from the app,
        # so what it has on screen is asked there; with nothing there, what it has open stays unknown.
        obs.notes["window_not_answering"] = True
        size = ctx.cfg.get("observe.windows.min_size") or [100, 60]
        shown = app_windows(ctx.helper, ctx.app["pid"], (int(size[0]), int(size[1])))
        obs.notes["window_stand_in"] = {k: shown[0][k] for k in ("id", "title", "frame")} if shown else None
        if shown:
            obs.notes["open_windows"] = [(f"「{w['title']}」" if w["title"] else "a window") + " (on screen; the app is not answering yet)"
                                         for w in shown]
        return                   # and nothing is reopened for an app that has not said what it has open
    titles = []
    for n in s.get("nodes", []):
        t = n.get("title") or ""
        titles.append(t or "(untitled)")
        if t and t != obs.window:
            obs.affordances.append(Affordance(f"x{len(obs.affordances)}", "window", "raise", f"switch to the window 「{t}」",
                                              {"ref": n["ref"], "pid": ctx.app["pid"], "action": "AXRaise"}, context=ctx.app.get("name", "")))
    obs.notes["open_windows"] = titles
    # running without a window: macOS "reopen" brings its main window back — unless it is still starting,
    # when no window *yet* is not no window
    if not titles and ctx.app.get("path") and not obs.notes.get("app_launching"):
        obs.affordances.append(Affordance(f"x{len(obs.affordances)}", "app", "reopen", f"bring back the main window of {ctx.app.get('name', 'the app')}",
                                          {"pid": ctx.app["pid"], "path": ctx.app["path"], "bundle_id": ctx.app.get("bundle_id"),
                                           "name": ctx.app.get("name")}, context=ctx.app.get("name", "")))


def _overlap(a: list[int], b: list[int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _read_by_sight(ctx: Ctx, w: dict[str, Any], said: str) -> str:
    """The text on a window as read from the screen, added to what its tree said (only what the tree did not)."""
    vc = ctx.cfg.section("observe.vision")
    try:
        wanted = vc.get("languages")
        res = ctx.helper.call("screen.ocr", pid=w["pid"], near=w.get("frame"),
                              languages=[] if wanted in (None, "auto") else list(wanted),
                              correct=bool(vc.get("language_correction", False)), fast=True,
                              min_conf=float(vc.get("min_conf", 0.3)), timeout=15)
    except HelperError:
        return said
    rtl = res.get("direction") == "rtl"
    boxes = sorted((b for b in res.get("boxes", []) if b.get("text") and b.get("frame")),
                   key=lambda b: (b["frame"][1] // 12, -b["frame"][0] if rtl else b["frame"][0]))
    fresh = [b["text"] for b in boxes if b["text"] not in said]
    return " / ".join(filter(None, [said, " / ".join(dict.fromkeys(fresh))]))


def _window_id(w: dict[str, Any]) -> Any:
    """What tells one window from another: its number where the system gives one, else where it is."""
    return w["id"] if w.get("id") is not None else (w.get("pid"), tuple(w.get("frame") or ()))


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
    # A window that appeared since the last look is what the last action most likely opened — the panel a
    # status item drops down, the sheet a button raised — and it was read in the window server's order,
    # behind whatever else was on screen, or not at all once the elsewhere budget was spent. A real task
    # pressed the menu bar clock three times and never read what came down. New first.
    seen: set[Any] = ctx.scope.windows_seen
    order = sorted(range(len(wins)), key=lambda i: (_window_id(wins[i]) in seen, i))
    for i in order:
        w = wins[i]
        # one entry per *window*: keyed on the process, a second dialog from the same one was invisible.
        # An input method's own floating windows — its candidate panel, its status bar, the tip it shows on a
        # change of mode — are no prompt, and nothing on them is a task's to answer, press or read: they get no
        # snapshot, no read by sight and no entry here. Taken for a prompt, the candidate panel was in
        # covered_by on 48 looks in 27 tasks on 09-20..23: 168 candidates offered as options, 4 reports that it
        # was left for the person to answer, and Escapes pressed at it that deleted the letters it was composing
        # ("hello" left as "hel" or "hell", in 3 tasks). On 16 of those looks the candidates were for letters
        # the task had not typed, the person's own typing in another window, and went to the decider as a
        # prompt. Its window at the ordinary level (its settings) is read like any other.
        if w.get("regular") or not w.get("alpha") or w.get("pid") == ctx.app["pid"] or input_method_panel(w) \
           or any(o["window"] == _window_id(w) for o in found):
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
        whole = " / ".join(dict.fromkeys(str(n.get(k)) for n in nodes for k in ("title", "value", "desc") if n.get(k)))
        if seen and _window_id(w) not in seen and oc.get("read_by_sight", True) and w.get("frame"):
            # `seen` empty is the first look: everything is new then, and reading four windows by sight
            # before the first decision would cost seconds for what the first look is not about. The first
            # look records what was there; what appears after an action is what gets read.
            # What its tree cannot say, the screen can — for a window of another process too. Notification
            # Center, dropped down by the menu bar clock, names its notifications in its tree and draws its
            # widgets, and the date a task was sent for was in a widget. Once, when the window first
            # appears: reading is not free (~0.3 s), and the seen set is what "first" means.
            whole = _read_by_sight(ctx, w, whole)
        cap = int(oc.get("text_chars", 1200))
        text = whole[:cap]
        if not text:   # nothing readable (the Dock's transparent layer, effects): nothing to tell the decider
            if not over:
                elsewhere_left += 1     # it cost a snapshot but it is not one of them: do not spend the slot
            continue
        where = "in front of the app" if over else "elsewhere on screen"
        # which interruption this is, across looks: who put it up and what it says (its position may move)
        key = f"{w.get('owner', '')}|{hashlib.sha1(text.encode()).hexdigest()[:10]}"
        found.append({"pid": w["pid"], "window": _window_id(w), "from": w.get("owner", ""), "text": text, "where": where,
                      "key": key, "frame": w.get("frame"), "over": bool(over)})
        # its controls: pressed through Accessibility like the app's own; the effect is watched on the app
        sub = Ctx(ctx.cfg, ctx.helper, ctx.goal, ctx.inputs, {"pid": w["pid"], "name": w.get("owner", "")}, ctx.running, ctx.cache)
        n0 = len(obs.affordances)
        element_affordances(sub, obs, nodes, "c", where=f"a prompt from {w.get('owner', '')} {where}")
        if len(whole) > cap:
            # A panel can say more than fits here — the menu bar clock drops down Notification Center, and
            # the date sat past the cut while the decider was told the panel held only notifications. What
            # was cut is said, and the whole of it can be read at once, like the app's own window.
            found[-1]["cut"] = len(whole) - cap
            obs.affordances.append(Affordance(f"c{len(obs.affordances)}", "window", "read_all",
                                              f"read all the text in the panel from {w.get('owner', '')} — returns everything it says at once "
                                              f"({len(whole) - cap} more characters than shown)",
                                              {"pid": w["pid"]}, context=str(w.get("owner", ""))))
        for a in obs.affordances[n0:]:
            a.target["watch"] = ctx.app["pid"]
            a.target["interruption"] = key      # the loop decides what these are *for* before any is offered
        if not over and not oc.get("elsewhere_actions", True):
            del obs.affordances[n0:]    # read it, but do not offer its controls
    seen.update(_window_id(w) for w in wins)
    if found:
        obs.notes["covered_by"] = [{"from": o["from"], "text": o["text"] + (f" … ({o['cut']} more characters, not shown)" if o.get("cut") else ""),
                                    "where": o["where"]} for o in found]
        obs.notes["interruptions"] = [{k: o[k] for k in ("key", "from", "text", "where", "frame", "over")} for o in found]
        lines = [f"[{o['where']}, from {o['from']}: {o['text']}]" for o in found]
        # after the app's own text, not before it. At the top, a prompt that had nothing to do with the goal
        # was the first thing the decider read on every look — and the first thing it then chose.
        _more_text(ctx, obs, lines)


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
def empty_crossings(boxes: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    """Places on a canvas that hold nothing, found from the things around them that do.

    Reading a screen gives back text, so a place with no text in it does not exist — and in a table that is
    exactly the place a task is sent to: the empty cell where a total goes. A task told to put a column's
    total in the 「合计」 row clicked 「合计」, 「金额」 and 「数量 金额」 in turn, eleven steps of it, because
    those were the only places it had. A person sees that the words line up in rows and columns and clicks
    where the row and the column cross.

    Nothing here knows what a spreadsheet is. Things that are aligned — by their left edges, their right
    edges or their centres — are a column, things at one height are a row, and a row with nothing where a
    column passes through it has an empty crossing. A calendar, a form laid out as a grid and a seating plan
    are the same thing to this.

    Alignment, not overlap: two adjacent headers are often read as one wide box, which overlaps both of the
    columns beneath it, and judged by overlap it glued 「数量」 and 「金额」 into a single column with its
    crossing in the gap between them. A wide box is aligned with at most one of them.
    """
    items = [b for b in boxes if b.get("frame") and len(b["frame"]) == 4 and str(b.get("text") or "").strip()]
    if len(items) < 3:
        return []
    heights = sorted(b["frame"][3] for b in items)
    h = float(heights[len(heights) // 2]) or 1.0

    def aligned(a: list[float], b: list[float]) -> bool:       # a shared left edge, right edge or centre
        tol = 0.5 * h
        return (abs(a[0] - b[0]) <= tol or abs((a[0] + a[2]) - (b[0] + b[2])) <= tol
                or abs((a[0] + a[2] / 2) - (b[0] + b[2] / 2)) <= tol)

    columns: list[list[dict[str, Any]]] = []
    for b in sorted(items, key=lambda b: b["frame"][0]):
        home = next((c for c in columns if any(aligned(b["frame"], m["frame"]) for m in c)), None)
        (home.append(b) if home is not None else columns.append([b]))
    # A column is continuous. Texts that happen to share a left edge from the title bar down to the grid —
    # 「WPS Office」, a ribbon icon's caption, 「金额」, 「1790」 — are not one column, and treated as one
    # they made every row between the title and the grid "part of its grid". A gap of more than three
    # lines (the distance already used for "too far from the column") ends a column and starts another.
    runs: list[list[dict[str, Any]]] = []
    for c in columns:
        run: list[dict[str, Any]] = []
        for m in sorted(c, key=lambda m: m["frame"][1]):
            if run and m["frame"][1] - (run[-1]["frame"][1] + run[-1]["frame"][3]) > 3 * h:
                runs.append(run)
                run = []
            run.append(m)
        runs.append(run)
    columns = [c for c in runs if len(c) >= 2]                # one thing is not a column
    rows: list[list[dict[str, Any]]] = []
    for b in sorted(items, key=lambda b: b["frame"][1]):
        cy = b["frame"][1] + b["frame"][3] / 2
        home = next((r for r in rows if abs((r[0]["frame"][1] + r[0]["frame"][3] / 2) - cy) <= 0.6 * h), None)
        (home.append(b) if home is not None else rows.append([b]))

    out: list[dict[str, Any]] = []
    for r in rows:
        y = sum(m["frame"][1] + m["frame"][3] / 2 for m in r) / len(r)
        for c in columns:
            if any(m in r for m in c):
                continue                                       # the column already has something in this row
            x = sorted(m["frame"][0] + m["frame"][2] / 2 for m in c)[len(c) // 2]
            if any(m["frame"][0] - 2 <= x <= m["frame"][0] + m["frame"][2] + 2 for m in r):
                continue                                       # something in this row already covers that spot
            top, bottom = min(m["frame"][1] for m in c), max(m["frame"][1] + m["frame"][3] for m in c)
            if not (top - 3 * h <= y <= bottom + 3 * h):
                continue                                       # too far from the column to be part of its grid
            out.append({"x": x, "y": y,
                        "row": [str(m["text"]) for m in sorted(r, key=lambda m: m["frame"][0])],
                        "column": [str(m["text"]) for m in sorted(c, key=lambda m: m["frame"][1])]})
    # The places worth offering first are the ones in the best-attested grid: a column of five figures
    # before two captions that happen to share an edge. Top-down order put a toolbar's coincidences ahead
    # of the table's last row, and the limit cut the table off.
    out.sort(key=lambda e: (-len(e["column"]), -len(e["row"]), e["y"]))
    return out[:limit]


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


def _start_glance(ctx: Ctx) -> dict[str, Any] | None:
    """Begin the glance on the helper's second connection while the tree is being read on the first.

    The glance is a screen capture and a resize; the tree is Accessibility round trips. They share nothing
    but the app, and were taken one after the other on every look. In socket mode the helper serves each
    connection on its own thread and the capture never needs the main thread, so the two overlap. The
    window frame is the last one seen for this app: close enough to pick the same window, and the glance
    of a window that has moved is compared against nothing anyway.
    """
    sc = ctx.cfg.section("observe.sight")
    bg = getattr(ctx.helper, "background", None)
    if not ctx.app or not sc.get("enabled", True) or bg is None:
        return None
    helper, pid = bg(), ctx.app["pid"]
    near = (ctx.cache.get("window_frame_by_pid") or {}).get(pid)
    holder: dict[str, Any] = {}

    def run() -> None:
        try:
            holder["glance"] = helper.call("screen.glance", pid=pid, near=near, grid=int(sc.get("grid", 32)),
                                           timeout=float(sc.get("timeout_s", 3)))
        except HelperError as exc:
            holder["error"] = exc.code
        except Exception as exc:  # noqa: BLE001  (a fake or a dying connection: the look goes on without a glance)
            holder["error"] = str(exc)[:80]

    thread = threading.Thread(target=run, name="glance", daemon=True)
    thread.start()
    holder["thread"] = thread
    return holder


@provider("sight")
def sight(ctx: Ctx, obs: Observation) -> None:
    """One coarse look at the window, kept with the observation (see sight.py). It offers nothing: it is what
    lets the next look say whether the picture changed."""
    sc = ctx.cfg.section("observe.sight")
    if not ctx.app or not sc.get("enabled", True):
        return
    started = obs.notes.pop("_glance", None)
    if started is not None:                       # begun with the look: collect it
        started["thread"].join(float(sc.get("timeout_s", 3)) + 1)
        if "glance" in started:
            obs.notes["glance"] = started["glance"]
        else:
            obs.notes["glance_unavailable"] = started.get("error", "timeout")
        return
    try:
        obs.notes["glance"] = ctx.helper.call("screen.glance", pid=ctx.app["pid"], near=obs.notes.get("window_frame"),
                                              grid=int(sc.get("grid", 32)), timeout=float(sc.get("timeout_s", 3)))
    except HelperError as exc:
        # no Screen Recording permission, no window on screen, an older helper: the engine is then as blind
        # in a self-drawn window as it was, and says so rather than failing the look
        obs.notes["glance_unavailable"] = exc.code


def parse_controls(declared: Any, max_ms: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Controls as someone declared them -> (what can be offered, what was wrong with the rest).

        move forward: {keys: [w], ms: [300, 1200]}
        sprint:       {keys: [shift, w], ms: 1000}
        look left:    {move: {dx: -200}, ms: 150}
        attack:       {buttons: [left], ms: 100}

    One entry per length: "for 0.3 s" and "for 1.2 s" are different actions with different consequences, and
    choosing between stated lengths is a choice the decider can make — producing a number is not.
    """
    good: list[dict[str, Any]] = []
    bad: list[str] = []
    for name, spec in (declared.items() if isinstance(declared, dict) else []):
        if not isinstance(spec, dict):
            bad.append(f"{name}: not a mapping")
            continue
        keys = [str(k) for k in spec.get("keys") or []] if isinstance(spec.get("keys"), (list, tuple)) else []
        buttons = [str(b) for b in spec.get("buttons") or []] if isinstance(spec.get("buttons"), (list, tuple)) else []
        move = spec.get("move") if isinstance(spec.get("move"), dict) else {}
        try:
            move = {k: int(move.get(k) or 0) for k in ("dx", "dy")}
            lengths = [int(x) for x in (spec["ms"] if isinstance(spec.get("ms"), (list, tuple)) else [spec["ms"]])]
        except (KeyError, TypeError, ValueError):
            bad.append(f"{name}: needs ms, a length in milliseconds (or a list of them)")
            continue
        if not (keys or buttons or any(move.values())) or any(x < 0 for x in lengths):
            bad.append(f"{name}: needs keys, buttons or move, and lengths that are not negative")
            continue
        for ms in dict.fromkeys(min(x, max_ms) for x in lengths):     # the ceiling is applied before the label is
            good.append({"name": str(name), "keys": keys, "buttons": buttons,   # written, so the label stays true
                         "move": move if any(move.values()) else {}, "ms": ms})
    return good, bad


@provider("controls")
def controls(ctx: Ctx, obs: Observation) -> None:
    """Controls someone declared for what is in front: keys to hold, buttons to hold, the pointer to move.

    A view that is steered rather than operated — a game, a map, a 3D scene — tells Accessibility nothing
    about what its keys do, and nothing in this repository may know it either. Somebody does: the caller
    (`inputs.controls`), or the person, per app, in their own config (`observe.controls.apps.<bundle id>`).
    This turns what they said into options and adds no knowledge of its own. A plugin that knows one
    particular game is the same thing through `macwork.providers`.

    Each option states what will physically happen and for how long, because that is all that is known:
    "move forward — hold w for 1.2 s". What it does in the world is the name its author gave it.
    """
    cc = ctx.cfg.section("observe.controls")
    if not cc.get("enabled", True):
        return
    declared: dict[str, Any] = {}
    per_app = (cc.get("apps") or {}).get((ctx.app or {}).get("bundle_id") or "")
    if isinstance(per_app, dict):
        declared.update(per_app)
    if isinstance(ctx.inputs.get("controls"), dict):
        declared.update(ctx.inputs["controls"])          # the caller's word for this task wins
        obs.notes["inputs_used"] = [*(obs.notes.get("inputs_used") or []), "controls"]
    good, bad = parse_controls(declared, int(cc.get("max_ms", 5000)))
    if bad:
        obs.notes["controls_rejected"] = bad
    where = (ctx.app or {}).get("name", "")
    for i, c in enumerate(good):
        parts = []
        if c["keys"] or c["buttons"]:
            parts.append("hold " + " + ".join(c["keys"] + [f"the {b} mouse button" for b in c["buttons"]]))
        if c["move"]:
            parts.append(f"move the pointer by ({c['move']['dx']}, {c['move']['dy']})")
        label = f"{c['name']} — {' and '.join(parts)} {'for' if c['keys'] or c['buttons'] else 'over'} {c['ms'] / 1000:g} s"
        obs.affordances.append(Affordance(f"h{i}", "hold", "hold", label, c, context=where,
                                          key=f"control:{c['name']}:{c['ms']}"))


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
    blank = float(obs.notes.get("undescribed_share") or 0.0) >= float(vc.get("canvas_area_share", 0.35))
    if mode == "auto" and not empty and not unlabeled and not known_canvas and not blank:
        return
    # "this task asked to read that window" is that task's business; kept globally, one task's choice made
    # every later task OCR the same window for the life of the process
    # A window can be richly described and still say nothing about the part the task is about. Numbers has 29
    # actionable nodes in its chrome and one scroll area, 989x1103 of a 1260x1201 window — 72% of it — that
    # the tree describes not at all: no cell, no number, no header. Reading it was offered as one option
    # among 843 and the decider took it about as often as it took anything else. How much of a window the
    # tree leaves undescribed is a fact it can be asked for before anything is read, and where most of a
    # window is undescribed, looking at it is not one way of seeing what is there — it is the only one.
    win = obs.notes.get("window_frame") or []
    area = float(win[2]) * float(win[3]) if len(win) == 4 and win[2] and win[3] else 0.0
    share = float(vc.get("canvas_area_share", 0.35))
    # …and "undescribed" has to mean the region holds nothing the tree named, not merely that the region
    # itself is unnamed. Chrome's web area is one unlabeled container covering the whole window, with 520
    # described controls inside it; Numbers' is one covering 72% with nothing inside at all. Measured the
    # first way they look identical, and Chrome would pay for an OCR on every look for nothing.
    # An action on something the size of the window — raising the window, pressing a group that is the whole
    # content area — describes nothing *in* it; counted as covering, it made every text read off a
    # spreadsheet "already reached by the tree", and the sheet was declared unreadable with 60 texts in hand.
    def describes(f: Any) -> bool:
        return isinstance(f, (list, tuple)) and len(f) == 4 and not (area and float(f[2]) * float(f[3]) >= share * area)
    placed = [a.target["frame"] for a in obs.affordances if a.channel in ("window", "pointer") and describes(a.target.get("frame"))]
    hollow = 0.0
    for u in unlabeled:
        f = u.get("frame")
        if not f or len(f) != 4:
            continue
        inside = sum(1 for g in placed if _inside(g, f))
        if inside <= int(vc.get("canvas_max_inside", 2)):
            hollow = max(hollow, float(f[2]) * float(f[3]))
    # Two readings of the same fact. The hollow container: a node that covers most of the window with nothing
    # the tree named inside it (Numbers' scroll area). And the window as a whole: the area no described node
    # covers at all (`undescribed_share`) — a spreadsheet whose grid is not in the tree as anything, a Qt
    # app, a canvas, a page still loading. The second is measured in `window()` and does not care whether
    # the app wrapped its blank in a container or not; WPS's grid was 80% of a window with 35 actionable
    # nodes in the toolbar, two 16-pixel unlabeled nodes, and nothing at all where the cells were.
    mostly_undescribed = (bool(area) and hollow / area >= share) or blank

    wanted = ctx.scope.vision_wanted
    if mode == "auto" and not empty and not known_canvas and not mostly_undescribed \
            and vc.get("on_demand", True) and key not in wanted:
        # the tree names most things: reading the screen for the rest costs ~0.3 s, so it happens when asked for
        obs.affordances.append(Affordance("vr", "vision", "reveal", f"read the {len(unlabeled)} controls without a label in this window "
                                          "from the screen (to see what they are)", {"key": key}, context=ctx.app.get("name", "")))
        return
    res = _ocr(ctx, obs, vc, sparse=empty or known_canvas or mostly_undescribed)
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
                                                  {"x": x, "y": y, "frame": b["frame"], "window_frame": win}, context=u.get("context", "")))
            for e in empty_crossings(inside_boxes, int(vc.get("max_empty_places", 12))):
                beside = " ".join(f"「{t[:20]}」" for t in e["row"][:3])
                under = " ".join(f"「{t[:20]}」" for t in e["column"][:5])
                obs.affordances.append(Affordance(
                    f"o{len(obs.affordances)}", "pointer", "click",
                    f"click the empty place in the row of {beside}, in line with {under}{where_c}",
                    {"x": e["x"], "y": e["y"], "window_frame": win}, context=u.get("context", "")))
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
            # A crop of the window is a screenshot. It went to /tmp under a name anyone could predict, readable
            # by anyone, and stayed there. It goes to a file only this user can read, and does not stay.
            fd, path = tempfile.mkstemp(prefix="macwork-icon-", suffix=".png")
            os.close(fd)
            try:
                ctx.helper.call("screen.capture", pid=ctx.app["pid"], near=obs.notes.get("window_frame"), crop=f, path=path, timeout=15)
                out = subprocess.run([*describe, path], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
                if out:
                    label += f" looks like: {out[:80]}"
            except (HelperError, OSError, subprocess.SubprocessError) as exc:
                obs.notes["describer_error"] = str(exc)[:120]
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        obs.affordances.append(Affordance(f"v{len(obs.affordances)}", "window", "press", label,
                                          {"ref": u["ref"], "pid": u["pid"], "action": u.get("action"), "frame": f}, context=u.get("context", "")))
    # Which of what was read is out of the tree's reach. `taken` is every frame the tree did reach, including
    # the unlabeled controls just named above — naming one does not make its text a separate target.
    taken = [a.target["frame"] for a in obs.affordances if a.channel == "window" and describes(a.target.get("frame"))]
    uncovered = [b for b in boxes if b.get("frame") and not any(_inside(b["frame"], t) for t in taken)]
    canvas = len(uncovered) >= int(vc.get("canvas_min_texts", 6))
    obs.notes["vision_uncovered"] = len(uncovered)
    if mostly_undescribed and not uncovered:
        # The tree describes nothing there, and neither does the screen: a fact the decider is told (it may be
        # loading: wait), and a cause for a task that ends here — not "no route", which says nothing.
        obs.notes["window_unreadable"] = float(obs.notes.get("undescribed_share") or round(hollow / area, 3) if area else 1.0)
        _more_text(ctx, obs, [f"(most of this window — {int(obs.notes['window_unreadable'] * 100)}% — shows nothing that can be read: "
                              "the app describes nothing there and nothing is readable on the screen)"])
    if canvas:
        ctx.cache["vision.canvas"].add(key)
    elif known_canvas:
        ctx.cache["vision.canvas"].discard(key)     # the app grew a tree (or the window changed): stop paying
    if empty or canvas:
        for b in uncovered[: int(vc.get("max_text_targets", 80))]:
            x, y = _center(b["frame"])
            obs.affordances.append(Affordance(f"o{len(obs.affordances)}", "pointer", "click", f"click the text 「{b['text']}」" + (f" at {_where_in(b['frame'], win, grid)}" if grid else ""),
                                              {"x": x, "y": y, "frame": b["frame"], "window_frame": win}))
        # what was read lines up in rows and columns — a grid the tree holds as nothing — and the places
        # where a row and a column cross with nothing in them are where something is to be put. Only the
        # texts in the undescribed part of the window line up: a toolbar's words above a grid happen to
        # share its columns, and crossings with them named rows after 「Page Layout」
        region = obs.notes.get("undescribed_frame")
        in_region = [b for b in uncovered if not region or _inside(b["frame"], region)]
        for e in empty_crossings(in_region, int(vc.get("max_empty_places", 12))):
            beside = " ".join(f"「{t[:20]}」" for t in e["row"][:3])
            under = " ".join(f"「{t[:20]}」" for t in e["column"][:5])
            obs.affordances.append(Affordance(f"o{len(obs.affordances)}", "pointer", "click",
                                              f"click the empty place in the row of {beside}, in line with {under}",
                                              {"x": e["x"], "y": e["y"], "window_frame": win}))
        _right_click_by_name(ctx, obs, vc, boxes)
        # right-to-left text read left-to-right comes back as a different sentence, so which way a line runs
        # is asked of the system (Locale.characterDirection for the languages actually recognised)
        rtl = res.get("direction") == "rtl"
        # only the part the tree could not say: what it could say is already in screen_text, and putting it
        # in twice spends the budget on repeating itself
        lines = [b["text"] for b in sorted(uncovered, key=lambda b: (b["frame"][1] // 12,
                                                                     -b["frame"][0] if rtl else b["frame"][0]))]
        _more_text(ctx, obs, lines)
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
def named_combo(obs: Observation, combo: str) -> str | None:
    """What this app calls the key combination, taken from its own menus — or None if it publishes no
    item with that key equivalent.

    A combination says nothing about itself: `press cmd+s` matches no word in any list, and a classifier
    reading that label has only the combo to go on, so both guards were blind to the same action at once
    and a save could be released as an edit. A table of well-known shortcuts is not the answer — that is
    knowledge about the outside world, and it depends on the app and the keyboard layout. Every app
    publishes the key equivalent of every menu item, and the menu provider has already read them.

    Never a filter: an app may implement a key with no menu item behind it, and that key still works.

    Both sides are read as the keyboard reads them (`canonical_combo`). Compared as written, the planner's
    `shift+cmd+p` named nothing where the menu's own ⇧⌘P is stored as `cmd+shift+p`.
    """
    want = canonical_combo(combo)
    if want is None:
        return None
    for a in obs.affordances:
        if a.channel == "menu" and a.target.get("combo") and canonical_combo(a.target["combo"]) == want:
            return str(a.target.get("path") or a.label)
    return None


@provider("keys")
def keys(ctx: Ctx, obs: Observation) -> None:
    """Physical keys (Return, Escape, Tab, paging) — the keyboard itself, not any app's commands: those come from
    the app's own menus (with their shortcuts) or are proposed by the planner for this app."""
    for i, combo in enumerate(ctx.cfg.get("observe.keys") or []):
        named = named_combo(obs, combo)      # the app's own name for it, where it has one
        obs.affordances.append(Affordance(f"k{i}", "keys", "key",
                                          f"press the {combo} key" + (f" ({named})" if named else ""),
                                          {"combo": combo}))


def _keys_in(ctx: Ctx, obs: Observation) -> str:
    """Where a key pressed now goes: the kind of window the look read (`_note_window_kind`), 'no window open',
    or 'a window that could not be read'. The last when the app did not answer or is still starting with
    nothing on screen yet (`onscreen.unreadable`), and when its tree came back empty while the window server
    shows a window of its own: the look on which an app first times out returns an empty tree and nothing
    else, and was taken for one with no window."""
    if unreadable(obs):
        return "a window that could not be read"
    if obs.notes.get("window_kind"):
        return str(obs.notes["window_kind"])
    pid = (ctx.app or {}).get("pid")
    if pid:
        size = ctx.cfg.get("observe.windows.min_size") or [100, 60]
        if app_windows(ctx.helper, pid, (int(size[0]), int(size[1]))):
            return "a window that could not be read"
    return "no window open"


def declare_keys(ctx: Ctx, obs: Observation, affs: list[Affordance]) -> None:
    """What the Mac declares about where each key and each keystroke of typing goes, as `Affordance.facts`:
    `in` (`_keys_in`), `keyboard_on` (the role and subrole of the focused element, when the focus is in the
    app being worked in) and `window_shows_a_file`. The floor keys its verdicts on them (policy._floor_key):
    a key's verdict was formed once per app and served every window, sheet and field after it. 9 key picks in
    this Mac's audit were judged by a verdict formed while the app had no window up, 7ffdee1b's Return at 0.58
    among them.

    A plain key's label also says what it triggers, where the Mac says so: the button a sheet or window names
    for Return or Escape, and the kind of field the keyboard is in, by its role only — a field's name is the
    page's or the app's text. Asked for every look's keys, the planner's suggested keys and the keys a way back
    is chosen among, from the same look."""
    keyed = [a for a in affs if a.channel == "keys"]
    if not keyed:
        return
    if "keys_in" not in obs.notes:            # once a look: its keys and the planner's go to the same place
        obs.notes["keys_in"] = _keys_in(ctx, obs)
    where = str(obs.notes["keys_in"])
    f = obs.focused or {}
    here = (ctx.app or {}).get("pid")
    on = _kind(f) if here and f.get("pid") == here and f.get("role") else ""
    text_roles = set(ctx.cfg.get("observe.window.text_roles") or [])
    field = (str(f.get("rdesc") or "") or str(f["role"]).removeprefix("AX").lower()) if on and f.get("role") in text_roles else ""
    facts: dict[str, Any] = {"in": where, "window_shows_a_file": bool(obs.notes.get("window_document"))}
    if on:
        facts["keyboard_on"] = on
    readable = where == obs.notes.get("window_kind")
    for a in keyed:
        a.facts.update(facts)
        combo = canonical_combo(a.target.get("combo")) if a.verb == "key" else None
        if not combo or "+" in combo:         # a combination is a command: its menu item says what it does
            continue
        said = []
        if combo == "return" and readable and obs.notes.get("default_button"):
            said.append(f"default button 「{obs.notes['default_button']}」")
        if combo == "escape" and readable and obs.notes.get("cancel_button"):
            said.append(f"cancel button 「{obs.notes['cancel_button']}」")
        if field:
            said.append(f"keyboard in {field}")
        for s in said:
            if f"({s})" not in a.label:
                a.label += f" ({s})"


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
                   "role": node.get("role"), "subrole": node.get("subrole"), "rdesc": node.get("rdesc"), "label": name,
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
    if (obs.focused or {}).get("secure_input"):
        # a password field has the keyboard: the helper refuses every keystroke while that is so, and the
        # option was offered anyway — steps spent reaching a refusal that was known before they were taken
        obs.notes["typing_withheld"] = "a password field has focus: keystrokes are refused while that is so"
        return
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

    Each one names the app that declares it, for the policy (`declared_by`). The file channel works through the
    system and was charged to no app, so "open a 「ssh:」 link with 终端" was offered from the terminal running
    this engine: a link that runs a command in the host, where nothing else of the host's may be done. Not as
    the app it opens with: `open` hands the link to the scheme's handler, which need not be the app declaring
    it, so the link still leaves the app, and whether that serves the goal is asked (`_leaves_for`).
    """
    models = ctx.cache.get("appmodels")
    if not ctx.app or models is None:
        return
    model = models.get(ctx.app) or {}
    name = ctx.app.get("name") or model.get("name") or ""
    for scheme in (model.get("url_schemes") or [])[: int(ctx.cfg.get("observe.schemes.limit", 8))]:
        obs.affordances.append(Affordance(
            f"h{len(obs.affordances)}", "file", "open", f"open a 「{scheme}:」 link with {name}",
            {"path": "", "scheme": scheme, "declared_by": ctx.app.get("bundle_id")},
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
    # From the same exact screen it returns the same text: once a task has it, it is not an option there.
    seen = zlib.crc32(f"{obs.window}\n{obs.screen_text}".encode("utf-8"))
    obs.affordances.append(Affordance(
        f"t{len(obs.affordances)}", "window", "read_all",
        str(rc.get("label", "read all the text in this window — returns everything it says at once, "
                            "including what is scrolled out of sight, with nothing to scroll")),
        {"pid": ctx.app["pid"]}, context=ctx.app.get("name", ""), yields=f"window:{ctx.app['pid']}:{seen}"))


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
            # Named by what the Service is called, not by a gloss of the engine's own: "hand content to TextEdit"
            # read as sending, and the floor stopped 「New TextEdit Window Containing Selection」 and 「Look Up in
            # Dictionary」 as outward-facing on three cross-app tasks in a row. The Service's name is the fact.
            f"v{len(obs.affordances)}", "service", "perform", f"「{s['name']}」 — a Service of {s['app']} on this Mac",
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


def _same_file(a: Any, b: Any) -> bool:
    try:
        return bool(a) and bool(b) and os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _measure(ctx: Ctx, path: Path) -> dict[str, Any]:
    """What can be said about a file without reading it to anyone: how big, how many lines, how it ends.

    Arithmetic stays in code. "The file is longer than the window" is a comparison the decider gets wrong
    and the engine gets right, so it is made here and handed over as a fact. Kept per (size, modified), so
    a file is measured once and again only when it has changed.
    """
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = f"{st.st_size}.{st.st_mtime_ns}"
    known = ctx.cache.setdefault("files.measured", {})
    if known.get(str(path), {}).get("stamp") == stamp:
        return known[str(path)]
    out: dict[str, Any] = {"stamp": stamp, "bytes": st.st_size}
    if 0 < st.st_size <= int(ctx.cfg.get("observe.files.measure_bytes", 200000)):
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""                          # not plain text: the helper may still read it (a PDF, an RTF)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()] if "\x00" not in text else []
        if lines:
            out.update(lines=len(text.splitlines()), tail=lines[-1])
    known[str(path)] = out
    return out


def _read_file(ctx: Ctx, obs: Observation, aid: str, path: Path, shown: str, is_open: bool) -> Affordance:
    """`read the text of …`, saying what it returns.

    The bare label lost to scrolling on a real task: asked what a 17-line file ended with, with the first
    9 lines on screen, the decider put 0.03 on reading it and never chose it. Nothing in the label said
    that it returns the *whole* file, or that the window did not. The same request with those two facts
    stated put 0.72 on it and chose it 5 times out of 5. Where it sat among the options made no difference.
    """
    m = _measure(ctx, path)
    n = m.get("lines")
    label = f"read the text of {shown} — returns " + (
        f"all {n} lines of it at once" if n and n > 1 else "everything it says at once") + ", with nothing to open or scroll"
    # Whether the window shows the end of it is measured, not guessed: the file's last line is either
    # among what the screen says or it is not.
    if is_open and m.get("tail") and _squash(m["tail"]) not in _squash(obs.screen_text):
        label += "; the window in front shows only part of it"
    return Affordance(aid, "file", "read", label, {"path": str(path)}, yields=f"file:{path}:{m.get('stamp', '')}")


def _squash(text: str) -> str:
    return "".join(str(text).split())


def _openers(ctx: Ctx, path: Path) -> list[dict[str, Any]]:
    """The apps this Mac would open a file with, asked of the system rather than guessed from the extension.
    The default one is first and marked; the plain "open" is that one, and says so."""
    if path.is_dir():
        return []
    try:
        apps = ctx.helper.call("apps.openers", path=str(path),
                               limit=int(ctx.cfg.get("observe.files.openers", 4)) + 1) or []
    except HelperError:
        return []      # an older helper: the plain "open" is still there
    return apps[: int(ctx.cfg.get("observe.files.openers", 4)) + 1]


# A path starts at a ~ or / that is not continuing a word — so "and/or" starts nothing, while a language
# that writes no space before it ("打开/Users/…") does. Stated as what a path character *is*, so it holds
# for every script rather than listing one script's punctuation.
_PATH_START = re.compile(r"(?:(?<![A-Za-z0-9._~/\\-])|(?<=^))[~/]")


def named_paths(text: str, max_words: int = 6, max_trim: int = 40) -> list[Path]:
    """Paths the caller named, found by asking the file system where each one ends.

    It used to be a regex that excluded CJK ideographs and some Chinese punctuation — there to keep
    `打开 ~/Downloads/x.pdf 这个文件` from swallowing the trailing words. It broke the script it was written
    for: `~/文稿/发票.pdf` was cut at the first Chinese character, so a Chinese filename could not be found
    at all, while Korean, Thai and Greek got neither the help nor the harm.

    Where a path ends is not a question about writing systems. It is a question about this Mac, and the
    Mac can be asked: take the longest prefix that exists. That handles a name in any script, a language
    that attaches its particles without a space, a trailing full stop, and a filename with spaces in it,
    without knowing anything about any of them.
    """
    out: list[Path] = []
    seen: set[str] = set()
    for m in _PATH_START.finditer(text):
        rest = text[m.start():]
        # candidate ends: each whitespace run (a path may contain spaces, but not many), then the end
        ends = [w.start() for w in re.finditer(r"\s", rest)][:max_words] + [len(rest)]
        for end in reversed(ends):                     # longest first: prefer the whole name
            found = None
            for cut in range(end, max(0, end - max_trim) - 1, -1):   # trim what the sentence added
                cand = rest[:cut].replace("\\ ", " ").strip()
                if not cand or cand in ("~", "/"):
                    break
                # What follows the cut decides whether this is the thing named or a piece of it. A `/`
                # next means the text was naming something deeper — "/a/b/missing.pdf" must not quietly
                # become "/a/b", which exists and is not what anyone asked for.
                if cut < len(rest) and rest[cut] == "/":
                    continue
                if cand.endswith("/"):
                    continue      # the text went on into a child; `Path` would quietly drop the slash
                try:
                    here = Path(cand).expanduser()
                    if here.exists():
                        found = here
                        break
                except (OSError, ValueError):          # a candidate too long for the file system, a bad byte
                    continue
            if found is not None:
                if str(found) not in seen:
                    seen.add(str(found))
                    out.append(found)
                break
    return out


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
        for here in named_paths(str(value or ""), int(fc.get("max_path_words", 6)),
                                int(fc.get("max_path_trim", 40))):
            if str(here) in {a.target.get("path") for a in obs.affordances}:
                continue
            what = "folder" if here.is_dir() else "file"
            # Already open in the window in front — the app says so itself (its window's document). Opening
            # it is then a completed action, and offering one reads as "this still needs doing": with a file
            # open and the goal about its contents, a real run was offered eight ways to open it again.
            is_open = _same_file(obs.notes.get("window_document"), here)
            openers = _openers(ctx, here)
            usual = next((o for o in openers if o.get("default")), None)
            if not is_open:
                # Which app that is, is known — and a goal that names it ("open it in WPS Office") was given
                # "in whichever app this Mac opens it with" and four other apps by name. It went looking for
                # the app by hand instead.
                how = f"in {usual['name']}, the app this Mac opens it with" if usual and usual.get("name") \
                    else "in whichever app this Mac opens it with"
                obs.affordances.append(Affordance(f"p{named}", "file", "open", f"open the {what} {here} {how}", {"path": str(here)}))
            # …and in any of the apps the system says can open it. A goal that names one ("open it in Safari")
            # can only be followed if that is an option: with just the line above, a real task opened the page
            # in the default browser and the goal was not met.
            front = (ctx.app or {}).get("bundle_id")
            for j, opener in enumerate(o for o in openers if not o.get("default")):
                if is_open and opener.get("bundle_id") == front:
                    continue                  # open in this very app; another app is still a different act
                obs.affordances.append(Affordance(
                    f"p{named}o{j}", "file", "open", f"open {here.name} with {opener['name']}",
                    {"path": str(here), "app": opener.get("path"), "bundle_id": opener.get("bundle_id")}))
            obs.affordances.append(Affordance(f"P{named}", "file", "reveal", f"show where {here} is on disk", {"path": str(here)}))
            if here.is_file():
                obs.affordances.append(_read_file(ctx, obs, f"T{named}", here, str(here), is_open))
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
        obs.affordances.append(_read_file(ctx, obs, f"R{i}", pp, f"「{pp.name}」",
                                          _same_file(obs.notes.get("window_document"), pp)))


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
        if a.target.get("named"):     # the goal names it: a group of its own, too small ever to be folded away
            return "app:named", "the apps the goal names"
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


SCREEN = "screen"   # the screen in front as one group: the key of its rest, when it alone is more than there is room for

Anchor = tuple[str, int]   # where a page begins: a member's handle, and which of the members with that handle it is


def on_screen(group: str) -> bool:
    """Is this group (a `group_of` key) part of the screen in front, an area or a list of the window, rather
    than a place to look into?"""
    return group.startswith(("area:", "list:"))


def page_members(affs: list[Affordance], group: str, pinned: set[str] | frozenset[str] = frozenset()) -> list[Affordance]:
    """What `arrange` pages through for a group: its members in `affs` order, less the planner's suggestions,
    which are offered on their own. SCREEN is every area and list of the screen in front."""
    return [a for a in affs if a.id not in pinned
            and (on_screen(group_of(a)[0]) if group == SCREEN else group_of(a)[0] == group)]


def page_anchor(members: list[Affordance], first: Affordance) -> Anchor:
    """Where a page that begins at `first` begins, in a form the next look can find again: its handle, and
    which of the members with that handle it is.

    The handle alone is not enough. Content has no identity, so its handle is its label, and a page anchored
    on a handle that an earlier member shares began at that earlier member instead. A window of 320 controls,
    every eleventh a 「Archive」 button, paged at 199: page 1 came back eight times running, and 122 controls
    were never shown. The window options of the 186 looks in this Mac's audit that had 120 or more, paged
    that way at a budget of 100 and of 60, left controls out of reach on 65 and 68 of them; anchored on
    (handle, which), on none."""
    h = first.handle()
    same = [a for a in members if a.handle() == h]
    return h, next((i for i, a in enumerate(same) if a is first), 0)


def _from(members: list[Affordance], anchor: Anchor | None) -> list[Affordance]:
    """The members in their order, beginning with the one `anchor` names, and the ones before it after the
    last: a page, then the rest, then round again. A missing anchor, or one that names no member here, is
    the first."""
    if anchor:
        handle, nth = anchor
        at = [i for i, a in enumerate(members) if a.handle() == handle]
        if 0 <= nth < len(at):
            return members[at[nth]:] + members[:at[nth]]
    return members


def _sample(members: list[Affordance], sample: int) -> str:
    """The first few members of a group by the last part of their names, each cut where a word ends."""
    return ", ".join(clip(m.label.split(" ▸ ")[-1], 40) for m in members[:sample]) + (", …" if len(members) > sample else "")


def arrange(affs: list[Affordance], budget: int, expanded: set[str] | dict[str, Anchor | None], sample: int = 12,
            fold_over: int = 0, pinned: set[str] | frozenset[str] = frozenset(),
            min_page: int = 60) -> tuple[list[Affordance], dict[str, tuple[str, list[Affordance]]]]:
    """Fit what can be done into one choice of at most ``budget`` options without guessing relevance: everything
    when it fits. Otherwise, in this order:

    * the screen in front, whole; when it alone is more than there is room for, its first part, and the rest
      one "look into the rest of the window" away;
    * the planner's suggestions (`pinned`, by id), each on its own, never with its whole group;
    * the group the decider opened (`expanded`: {group: the `Anchor` its page begins at, None for its first
      member}, or a set of one group, opened at its first), shown from there: at least ``min_page`` of it
      (all of it when smaller) and as much more as the room left holds, its rest one "look into the rest
      of …" away. One group is open at a time; SCREEN opens the screen's own rest;
    * then the smallest other groups whole, and the others as one "look into …" each, which the decider can
      open like a person opens a menu.

    Every place keeps at least its own entry, so nothing is dropped: an action is an option, or inside an
    entry that names it. Only more places than options can break that, and `invariants.check_options` says so."""
    opened = dict(expanded) if isinstance(expanded, dict) else dict.fromkeys(expanded)
    group, anchor = next(iter(opened.items()), (None, None))   # one group is open at a time
    groups: dict[str, list[Affordance]] = {}
    names: dict[str, str] = {}
    for a in affs:
        if a.id in pinned:
            continue
        k, n = group_of(a)
        groups.setdefault(k, []).append(a)
        names[k] = n
    # What is on the screen in front is not a place to look into; it is the place. A spreadsheet read from
    # the screen put 69 targets and 54 controls in the window's group, which made it "huge" like the list of
    # every installed app, and the decider was shown 185 options — Format ▸ Rows ▸ Hide, eight URL schemes,
    # the user's Shortcuts — with the whole sheet folded behind one "look into the window" it never opened.
    # Where an action lives is still the only thing this goes by: on the screen now, before anywhere else.
    huge = {k for k in groups if fold_over and len(groups[k]) > fold_over and k != group and not on_screen(k)}
    if len(affs) <= budget and not huge:
        return affs, {}
    pins = [a for a in affs if a.id in pinned]
    screen = page_members(affs, SCREEN, pinned)
    others = [k for k in groups if not on_screen(k)]
    opens = group if group in groups and not on_screen(group) else None
    # …and it goes first when room runs out, before what the decider opened. Opened groups were listed first
    # and the tail cut: a real task looked into the 535 installed apps while Calculator was not answering, the
    # list stayed open, and on the next nine looks 102 to 195 apps stood in front of the keypad and not one of
    # Calculator's 59 controls was an option. The opened group is owed a page; the screen has the rest.
    room = budget - len(pins) - len(others)                  # every other place keeps at least its own entry
    owed = min(len(groups[opens]) - 1, min_page) if opens else 0
    fits = max(room - owed, 0)
    folded: dict[str, tuple[str, list[Affordance]]] = {}
    shown_screen, screen_rest = screen, []
    if len(screen) > fits:
        paged = _from(screen, anchor if group == SCREEN else None)
        shown_screen, screen_rest = paged[: max(fits - 1, 0)], paged[max(fits - 1, 0):]
        room -= 1
    room -= len(shown_screen)
    page: list[Affordance] = []
    if opens:
        members = _from(groups[opens], anchor)
        if len(members) - 1 <= room:
            page, room = members, room - (len(members) - 1)
        else:
            spare = max(room, 0)
            page, rest, room = members[:spare], members[spare:], room - spare
            # worded as a "look into" like every other entry: the decider's instructions say what those are
            folded[opens] = (f"look into the rest of {names[opens]} ({len(rest)} more of {len(members)}: "
                             f"{_sample(rest, sample)})", rest)
    # Smallest groups first is not a guess at what matters — it is what shows the most *distinct* places at
    # once; the largest become one "look into …" each, so nothing is dropped for being judged uninteresting.
    shown: set[str] = set()
    for k in sorted((k for k in others if k != opens and k not in huge), key=lambda k: len(groups[k])):
        if len(groups[k]) - 1 > room:
            break
        shown.add(k)
        room -= len(groups[k]) - 1
    for k in others:
        if k != opens and k not in shown:
            folded[k] = (f"look into {names[k]} ({len(groups[k])} options: {_sample(groups[k], sample)})", groups[k])
    if screen_rest:
        folded[SCREEN] = (f"look into the rest of the window ({len(screen_rest)} more of {len(screen)} on screen: "
                          f"{_sample(screen_rest, sample)})", screen_rest)
    flat = shown_screen + pins + page + [a for a in affs if a.id not in pinned and group_of(a)[0] in shown]
    # Only more places than options can still cut anything here; the caller is told, and the look breaks
    # an invariant (`invariants.check_options`).
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
    providers = ctx.cfg.get("observe.providers") or []
    if "sight" in providers and bool(ctx.cfg.get("observe.sight.overlap", True)):
        started = _start_glance(ctx)
        if started is not None:
            obs.notes["_glance"] = started
    for name in providers:
        fn = get_provider(name)
        if fn is None:
            log.warning("unknown provider %s", name)
            continue
        t, n0 = time.monotonic(), len(obs.affordances)
        try:
            fn(ctx, obs)
        except (HelperError, OSError, subprocess.SubprocessError) as exc:
            # an older helper answers a method it does not have with `no_method`; that is not the provider
            # failing, and the way out is a rebuild, not a bug report. Only the helper's errors carry a code: a
            # command that timed out (`shortcuts list`, mdfind) made this line raise, and the look with it.
            obs.notes[f"{name}_error"] = ("the helper is older than this engine and does not know " + exc.message.split()[-1]
                                          + ": rebuild it with `macwork helper install`") if getattr(exc, "code", None) == "no_method" else str(exc)[:200]
            log.info("provider %s failed: %s", name, exc)
        obs.notes[f"{name}_n"] = len(obs.affordances) - n0
        obs.notes[f"{name}_ms"] = round((time.monotonic() - t) * 1000)
    declare_keys(ctx, obs, obs.affordances)   # after every provider: where the keys go is read from all of them
    _disambiguate(obs.affordances)
    ids = [a.id for a in obs.affordances]
    if len(ids) != len(set(ids)):
        for i, a in enumerate(obs.affordances):   # providers are independent; make ids unique afterwards
            a.id = f"{a.id}_{i}"
    obs.notes["ms"] = round((time.monotonic() - t0) * 1000)
    return obs
