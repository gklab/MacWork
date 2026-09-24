"""What the window server says is on screen needs no app to answer for it.

Accessibility asks the app itself, and an app that is launching, busy or hung answers late or not at all. The
window server lists every window — its owner, level, frame and whether it is drawn — without asking the app
(`screen.windows`). These are the rules for reading that list, in one place: which windows are an app's own,
which ones an input method floats over it, and when two frames are one window.
"""

from __future__ import annotations

import time
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
    source (`input_method`); those above the normal window level, layer 0, are panels. The same process's
    window at layer 0 is its settings, a window like any other, and is never skipped. Layer 0 is not where
    `ordinary` draws its line: a newer helper calls ordinary every layer from 0 up to the main menu's, 24, so
    an input method's window at layers 1 to 23, a floating or a modal panel of its own, is both ordinary and
    a panel here."""
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


def still_launching(said: dict[str, Any], running: list[dict[str, Any]] | None, pid: int, bound_s: float) -> bool:
    """Is this app still starting? The helper says whether it has finished launching (`launching`, from a
    0.2.0 helper), and the answer is believed only while its launch is recent: Apple documents that some
    processes never report finished launching, and one that never does is not starting for ever. How recent
    is `bound_s` (engine.open_front_s), counted from the launch time `apps.running` gives for it."""
    if not said.get("launching"):
        return False
    launched = next((a.get("launched") for a in running or [] if a.get("pid") == pid), None)
    try:
        return time.time() - float(launched) <= bound_s
    except (TypeError, ValueError):
        return False


def app_readiness(helper: Any, pid: int, running: list[dict[str, Any]] | None, ask_now: bool = False,
                  launch_bound_s: float = 6.0, min_size: tuple[int, int] = (100, 60)) -> dict[str, Any]:
    """Can this app be read now? A question of its windows alone, asked twice when the first reply is empty:
    nothing is read of them.

    - answering: it answered Accessibility: the helper did not report `not_answering`;
    - launching: it is still starting (`still_launching`);
    - windows: how many windows it has: Accessibility's count, or, while it is launching, the window
      server's (`app_windows`), since an app that answers while it starts may list no window yet;
    - ready: answering, and launched or showing a window;
    - can_ask_now: the helper said whether the app is launching at all. A 0.2.0 helper always does on a
      not-answering reply and honours `ask_now`; an older one says neither and keeps its 20 s cooldown
      whatever it is asked.

    The helper (0.1.0 and 0.2.0 alike) says `not_answering` only on a call it skips, for an app it had
    already marked. The call whose own question times out marks the app and replies as for an app with no
    window: nothing, and no `not_answering`. Read as an answer, that made a busy app ready: a look whose first
    question it was did not wait, and the decider was asked about a look the app had not answered; and in a
    wait, a question asked again that the app answered, then a window list that timed out, ended the wait as
    answered at once, with the next look blind. So an empty reply is asked for once more at once, without
    `ask_now`: an app the first call marked is skipped and says so, and one that has no window answers again
    in milliseconds.

    A helper that cannot be asked reads as ready: not being able to tell is no reason to wait."""
    try:
        said = helper.call("ax.snapshot", pid=pid, scope="windows", max_depth=0, max_nodes=10, actions=False,
                           ask_now=ask_now)
        if not said.get("nodes") and not said.get("not_answering"):
            said = helper.call("ax.snapshot", pid=pid, scope="windows", max_depth=0, max_nodes=10, actions=False,
                               ask_now=False)
    except HelperError:
        return {"answering": True, "launching": False, "windows": 0, "ready": True, "can_ask_now": False}
    answering = not said.get("not_answering")
    launching = still_launching(said, running, pid, launch_bound_s)
    windows = len(app_windows(helper, pid, min_size)) if launching else len(said.get("nodes") or [])
    return {"answering": answering, "launching": launching, "windows": windows,
            "ready": answering and (not launching or windows > 0), "can_ask_now": "launching" in said}


def unreadable(obs: Any) -> str:
    """Why this look could read nothing of the app, or "" when it could: 'starting' or 'busy' when the app
    did not answer Accessibility, and 'starting' when it answered while still launching with no window open
    yet. What such a look lacks is not evidence about the app, and this is what its readers do with it: the
    decider is not told that the app has no window (observe.windows), no sub-goal advances on it, a rethink,
    ask-user, blocked or impossible vote on it waits first (loop._judge), the planner is not asked about it
    while a wait is still possible (consult._consult), and the floor's verdict on a key or on typing sent on
    it serves only looks that could not read the window either (observe._keys_in). A done vote and a progress
    vote made on it are taken as on any other look."""
    notes = obs.notes
    if notes.get("window_not_answering"):
        return "starting" if notes.get("app_launching") else "busy"
    if notes.get("app_launching") and not (notes.get("open_windows") or notes.get("window_frame") or obs.window):
        return "starting"
    return ""
