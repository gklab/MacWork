"""Looking at the window, the way a person does after pressing something.

A window that describes itself to Accessibility can be read from its tree. One that draws itself — a game, a
map, a canvas, a video — says nothing there, and the engine was blind in it: it could not tell an action
that moved the world from one that did nothing, so it either concluded wrongly or (after `Step.unseen`)
did not conclude at all.

A person does not need to know what a picture is *of* to see that it changed. Neither does this. The helper
hands back the window as a coarse grid of brightness (`screen.glance`, about 30 ms, nothing leaves the Mac);
two of them, before an action and after, give how much of the window changed and roughly where. That is
arithmetic, and it stays in code: the decider is told the result as a fact.
"""

from __future__ import annotations

import base64
from typing import Any


def compare(before: dict[str, Any] | None, after: dict[str, Any] | None, tolerance: int,
            within: list[int] | None = None) -> dict[str, Any] | None:
    """How much of the window's picture changed between two glances, or None when they cannot be compared
    (one is missing, or the window is not the same size — a different picture of a different thing).

    Where it changed is said twice: in words for the decider (`where`), and as a place on screen (`region`:
    the box of the changed cells with one cell around it, in screen points of the window as the second glance
    found it), which is where something the step drew can be read (observe.read_the_change).

    `within` (screen points of the window as the first glance found it) compares only the cells whose centre
    lies there: has that part of the window changed since? Cells, not points, so a window that has moved
    between the two is still compared part for part."""
    if not before or not after or before.get("grid") != after.get("grid"):
        return None
    if list(before.get("frame") or [])[2:] != list(after.get("frame") or [])[2:]:
        return None
    try:
        a, b = base64.b64decode(before["cells"]), base64.b64decode(after["cells"])
    except (KeyError, ValueError, TypeError):
        return None
    n = int(before["grid"])
    if len(a) != n * n or len(b) != n * n:
        return None
    looked = range(n * n) if within is None else _cells_in(before.get("frame"), n, within)
    if not looked:
        return None                         # no cell lies there: nothing can be said about it
    # `tolerance` is the capture's own noise floor — a caret blinking moves a cell by a level or two — not a
    # judgement about what counts as a change. Measured on an idle window: 0 cells beyond 2 levels.
    cells = [i for i in looked if abs(a[i] - b[i]) > tolerance]
    if not cells:
        return {"share": 0.0, "cells": 0, "where": ""}
    rows, cols = [i // n for i in cells], [i % n for i in cells]
    box = (min(cols) / n, min(rows) / n, (max(cols) + 1) / n, (max(rows) + 1) / n)
    out: dict[str, Any] = {"share": len(cells) / (n * n), "cells": len(cells), "where": _where(box)}
    region = _region(after.get("frame"), n, min(cols) - 1, min(rows) - 1, max(cols) + 2, max(rows) + 2)
    if region:
        out["region"] = region
    return out


def _frame(frame: Any) -> tuple[float, float, float, float] | None:
    try:
        x, y, w, h = (float(v) for v in frame)
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _region(frame: Any, n: int, left: int, top: int, right: int, bottom: int) -> list[int] | None:
    """Cells [left, right) × [top, bottom) of an n×n glance of the window at `frame`, in screen points, cut to
    the window."""
    f = _frame(frame)
    if f is None:
        return None
    x, y, w, h = f
    left, top, right, bottom = max(0, left), max(0, top), min(n, right), min(n, bottom)
    return [round(x + left * w / n), round(y + top * h / n), round((right - left) * w / n), round((bottom - top) * h / n)]


def _cells_in(frame: Any, n: int, region: list[int]) -> list[int]:
    """The cells of an n×n glance of the window at `frame` whose centre lies in `region` (screen points)."""
    f, r = _frame(frame), _frame(region)
    if f is None or r is None:
        return []
    x, y, w, h = f
    rx, ry, rw, rh = r
    return [row * n + col for row in range(n) for col in range(n)
            if rx <= x + (col + 0.5) * w / n <= rx + rw and ry <= y + (row + 0.5) * h / n <= ry + rh]


def _where(box: tuple[float, float, float, float]) -> str:
    left, top, right, bottom = box
    if (right - left) * (bottom - top) > 0.5:
        return "across the window"
    x, y = (left + right) / 2, (top + bottom) / 2
    across = "left" if x < 1 / 3 else "right" if x > 2 / 3 else ""
    down = "top" if y < 1 / 3 else "bottom" if y > 2 / 3 else ""
    return f"at the {' '.join(w for w in (down, across) if w)}" if across or down else "in the middle"


def describe(diff: dict[str, Any] | None) -> str:
    """The measurement as the decider reads it. Empty when there is nothing to add."""
    if not diff:
        return ""
    if not diff["cells"]:
        return "the picture in the window did not change either"
    share = diff["share"] * 100
    amount = f"{share:.0f}%" if share >= 1 else "under 1%"
    return f"the picture in the window changed ({amount} of it, {diff['where']})"
