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


def compare(before: dict[str, Any] | None, after: dict[str, Any] | None, tolerance: int) -> dict[str, Any] | None:
    """How much of the window's picture changed between two glances, or None when they cannot be compared
    (one is missing, or the window is not the same size — a different picture of a different thing)."""
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
    # `tolerance` is the capture's own noise floor — a caret blinking moves a cell by a level or two — not a
    # judgement about what counts as a change. Measured on an idle window: 0 cells beyond 2 levels.
    cells = [i for i in range(n * n) if abs(a[i] - b[i]) > tolerance]
    if not cells:
        return {"share": 0.0, "cells": 0, "where": ""}
    rows, cols = [i // n for i in cells], [i % n for i in cells]
    box = (min(cols) / n, min(rows) / n, (max(cols) + 1) / n, (max(rows) + 1) / n)
    return {"share": len(cells) / (n * n), "cells": len(cells), "where": _where(box)}


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
