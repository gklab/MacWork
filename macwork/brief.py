"""What the planner is told of what can be done: each place its own share of the brief (planner.brief).

The planner was shown the first `planner.context_actions` labels of the options, the window's first. After c632ed9
made that 40, not one of the 97 plan and replan prompts in this Mac's audit held an item of the app's own menus:
1,145 of their 3,742 labels were the Apple menu's, 965 of them its Recent Items, and 48 of the prompts had no menu
item at all. Each place now has a share of its own, sized by planner.brief:

* apps_the_goal_names, first (30e3a4a): the options of the apps on this Mac the goal names;
* on_screen: what the app's own window offers — every window or pointer option that names no other process, which
  takes in what a canvas or a Qt window is worked by (vision's click targets, the wheel, dragging by name: none of
  them carries a pid) — and bringing its main window back. One entry per name; a run of more than `fold_over` of
  one content role (observe.window.content_roles: rows, cells, links…) is one entry, a run of controls never is:
  a calculator's keypad is twenty buttons of one kind, and each of them is the app's own;
* menus: one line per top-level menu, in the menu bar's order: each item by its place below the top menu, with its
  mark and key equivalent, those with a key equivalent first; a submenu of more than `fold_over` items (Recent
  Items, Open Recent, Services) is one entry that says how many there are and names two;
* elsewhere: a line for each other process with controls on screen, and one for the Services, naming those of
  the apps the goal names.

The planner's own options are not in it: they are its moves already."""

from __future__ import annotations

import re
from typing import Any

from .model import Affordance
from .observe import key_equivalent

_FOLDED = re.compile(r" \(and \d+ more like it\)$")


def as_named(said: str) -> str:
    """A name copied out of the brief, without what the brief added to it: a folded run's count."""
    return _FOLDED.sub("", said or "")


def shares(cfg: Any, affs: list[Affordance], app: dict[str, Any] | None, running: list[dict[str, Any]]) -> dict[str, Any]:
    """{apps_the_goal_names, on_screen, menus, elsewhere}, in that order: on_screen and menus always, the others
    when there is something in them."""
    conf = cfg.section("planner.brief")
    fold_over = int(conf.get("fold_over", 4))
    affs = [a for a in affs if "try" not in a.target]
    out: dict[str, Any] = {}
    named = [a for a in affs if a.target.get("named")]
    if named:
        out["apps_the_goal_names"] = list(dict.fromkeys(a.label for a in named))
    out["on_screen"] = on_screen(affs, app, set(cfg.get("observe.window.content_roles") or []),
                                 int(conf.get("on_screen", 16)), fold_over)
    out["menus"] = menus(affs, int(conf.get("menu_entries", 12)), fold_over)
    lines = elsewhere(affs, app, running, {str(a.target.get("name")) for a in named if a.target.get("name")},
                      int(conf.get("elsewhere", 3)), fold_over)
    if lines:
        out["elsewhere"] = lines
    return out


def _capped(entries: list[str], cap: int) -> list[str]:
    return entries[:cap] + ([f"(+{len(entries) - cap} more)"] if len(entries) > cap else [])


def on_screen(affs: list[Affordance], app: dict[str, Any] | None, content_roles: set[str], cap: int,
              fold_over: int) -> list[str]:
    """The app's own window, as names: one entry each, a long run of one content role as one entry, capped."""
    pid = (app or {}).get("pid")
    rows: list[tuple[str, str]] = []            # (name, its content role, or "")
    seen: set[str] = set()
    for a in affs:
        own = a.channel in ("window", "pointer") and a.target.get("pid") in (None, pid)
        if not (own or (a.channel == "app" and a.verb == "reopen")) or a.name() in seen:
            continue
        seen.add(a.name())
        role = str(a.facts.get("control") or "").split("/")[0]
        rows.append((a.name(), role if role in content_roles else ""))
    entries: list[str] = []
    i = 0
    while i < len(rows):
        name, role = rows[i]
        j = i + 1
        while role and j < len(rows) and rows[j][1] == role:
            j += 1
        if role and j - i > fold_over:
            entries.append(f"{name} (and {j - i - 1} more like it)")
            i = j
        else:
            entries.append(name)
            i += 1
    return _capped(entries, cap)


def menus(affs: list[Affordance], cap: int, fold_over: int) -> dict[str, str]:
    """{top-level menu: its line}, in the menu bar's order."""
    tops: dict[str, list[Affordance]] = {}
    for a in affs:
        path = (a.target.get("menu_path") or []) if a.channel == "menu" else []
        if len(path) >= 2:
            tops.setdefault(str(path[0]), []).append(a)
    return {top: " · ".join(_capped(menu_entries(items, top, fold_over), cap)) for top, items in tops.items()}


def menu_entries(items: list[Affordance], top: str, fold_over: int) -> list[str]:
    """One top menu's entries in the order the brief lists them: each item by its place below `top`, with its mark
    and key equivalent (its label less 'menu Top ▸ '), those with a key equivalent first, then the menu's order; a
    submenu of more than `fold_over` items is one entry, 'Sub ▸ … (N: a, b, …)', where its first item stood."""
    subs: dict[tuple[str, ...], list[Affordance]] = {}
    for a in items:
        subs.setdefault(tuple(a.target["menu_path"][1:-1]), []).append(a)
    keyed: list[str] = []
    rest: list[str] = []
    for a in items:
        sub = tuple(a.target["menu_path"][1:-1])
        if sub and len(subs[sub]) > fold_over:
            if subs[sub][0] is a:
                names = [str(m.target["menu_path"][-1]) for m in subs[sub][:2]]
                rest.append(f"{' ▸ '.join(sub)} ▸ … ({len(subs[sub])}: {', '.join(names)}, …)")
            continue
        (keyed if key_equivalent(a.label) else rest).append(a.label.removeprefix(f"menu {top} ▸ "))
    return keyed + rest


def elsewhere(affs: list[Affordance], app: dict[str, Any] | None, running: list[dict[str, Any]], goal_apps: set[str],
              cap: int, fold_over: int) -> list[str]:
    """A line per other process with controls on screen (named as the Mac's list of running apps names it, else
    where its controls are), then one for the Services; `cap` lines in all."""
    pid = (app or {}).get("pid")
    called = {r.get("pid"): str(r.get("name") or "") for r in running}
    procs: dict[Any, tuple[str, list[str]]] = {}
    services: list[Affordance] = []
    for a in affs:
        if a.channel == "service":
            services.append(a)
            continue
        other = a.target.get("pid")
        if a.channel in ("window", "pointer") and other not in (None, pid):
            _who, names = procs.setdefault(other, (called.get(other) or a.context or "another process", []))
            if a.name() not in names:
                names.append(a.name())
    lines = [f"{who} ({len(names)}: {', '.join(names[:2])}, …)" if len(names) > fold_over else f"{who}: {' · '.join(names)}"
             for who, names in procs.values()]
    room = max(cap - (1 if services else 0), 0)
    if len(lines) > room:
        lines = lines[:room]
        if lines:
            lines[-1] += f" (+{len(procs) - room} more processes)"
    if services:
        mine = [a.label for a in services if a.target.get("app") in goal_apps]
        lines.append(f"Services on this Mac: {len(services)}" + (f"; of the apps the goal names: {' · '.join(mine)}" if mine else ""))
    return lines
