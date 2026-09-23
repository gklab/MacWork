"""What the window server says is on screen needs no app to answer for it.

Accessibility asks the app itself, and an app that is launching, busy or hung answers late or not at all. The
window server lists every window — its owner, level, frame and whether it is drawn — without asking the app
(`screen.windows`). These are the rules for reading that list, in one place: which windows are an app's own,
which ones an input method floats over it, and when two frames are one window.
"""

from __future__ import annotations

from typing import Any

from .helper import HelperError


def _layer(w: dict[str, Any]) -> int:
    try:
        return int(w.get("layer") or 0)
    except (TypeError, ValueError):
        return 0


def ordinary(w: dict[str, Any]) -> bool:
    """A window at the level an app's own windows live at. A newer helper says so itself (`ordinary`: from
    the normal window level up to, not including, the main menu's); from an older one there is only the layer,
    and all it tells is the normal level, 0."""
    said = w.get("ordinary")
    return bool(said) if said is not None else _layer(w) == 0


def input_method_panel(w: dict[str, Any]) -> bool:
    """A window an input method floats over the app — its candidates, what is being composed — which is no
    prompt, no dialog and nothing a task answers or closes. The helper marks every window of an enabled input
    source (`input_method`); only those above the ordinary level are panels. The same process's window at the
    ordinary level is its settings, a window like any other, and is never skipped."""
    return bool(w.get("input_method")) and _layer(w) > 0


def same_place(a: Any, b: Any, slack: float = 3) -> bool:
    """Two frames ([x, y, width, height]) within `slack` points of each other in every number: one window,
    as the window server and Accessibility each report it. act and tidy each had a copy of this rule."""
    return bool(a and b) and all(abs(float(x) - float(y)) <= slack for x, y in zip(a, b))


def app_windows(helper: Any, pid: int, min_size: tuple[int, int] = (100, 60),
                all_spaces: bool = False) -> list[dict[str, Any]]:
    """The windows of one app a person would call its windows, front to back: at the ordinary level, drawn
    (alpha above 0), on this Space unless `all_spaces`, and at least `min_size` — not a toolbar strip or an
    unrealised panel. The helper is asked for that app's windows only; an older one does not know the
    parameter and lists every process's, so the owner is checked here as well. Nothing to tell is none."""
    try:
        wins = helper.call("screen.windows", pid=pid, all=all_spaces, timeout=5) or []
    except HelperError:
        return []
    out: list[dict[str, Any]] = []
    for w in wins:
        f = w.get("frame")
        if w.get("pid") != pid or not ordinary(w) or float(w.get("alpha", 1) or 0) <= 0:
            continue
        if not (all_spaces or w.get("on_screen", True)):
            continue
        if not (isinstance(f, (list, tuple)) and len(f) == 4 and f[2] >= min_size[0] and f[3] >= min_size[1]):
            continue
        out.append({"id": w.get("id"), "title": str(w.get("title") or ""), "frame": list(f), "layer": _layer(w)})
    return out
