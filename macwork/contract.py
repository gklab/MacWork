"""What a step promises.

Before: an action that uses the keyboard or the front app may only run when the engine holds the hands and the
app is really in front (``act`` enforces that where it types; here it is stated once, for the loop to check).
After: the action must show the effect it promised. Typed text has to be in the field the app reads — setting a
value that the app ignores looks right in the Accessibility tree and is the reason a "Go to Folder" ends up
somewhere else. A switch has to make that app frontmost. Anything else falls back to "did the screen change",
which the loop already measures.

An action whose promise cannot be checked (a menu command with no visible result) is *unverified*, not failed:
the stricter "is it really done" question decides those, as before.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .helper import HelperError
from .model import Affordance
from .observe import Ctx

log = logging.getLogger(__name__)


def promise(a: Affordance, params: dict[str, Any]) -> str:
    """What this action says it will do, in one word: typed / switched / changed / unknown."""
    if a.verb in ("type", "type_submit") and params.get("text") is not None:
        return "typed"
    if a.verb in ("activate", "open", "reopen") or (a.channel == "app" and a.verb != "quit"):
        return "switched"
    if a.verb in ("select_text", "cursor_end", "select"):
        return "selected"
    return "unknown"


def _field_text(ctx: Ctx, ref: str) -> str | None:
    try:
        return str(ctx.helper.call("ax.get", ref=ref, attribute="AXValue").get("value") or "")
    except HelperError:
        return None


def _cursor_text(ctx: Ctx, limit: int) -> str | None:
    """What the element with the keyboard focus holds, or None when it may not or cannot be read.

    Typing at the cursor had no check at all: it goes wherever the system focus is, so there was no ref to read
    back and every such step came back "nothing to read back" — unverified, including the ones that went into
    the wrong window. The focus is a system-wide question and `ax.focused` answers it.

    It refuses while secure input is on. That is not a role test: it is the window server's own answer about
    whether a password field has focus anywhere, and the point is to never read a secret back, not to work out
    what kind of field this is.
    """
    try:
        r = ctx.helper.call("ax.focused", max_text=limit)
    except HelperError:
        return None
    if r.get("secure_input"):
        return None
    node = r.get("focused")
    if not isinstance(node, dict):
        return None
    # No AXValue at all is a canvas or a custom view — "cannot be read", not "reads as empty". An element
    # that does answer, with "", really is empty, and typing into it that leaves it empty did fail.
    text = node.get("selected_text") if node.get("selected_text") else node.get("value")
    return str(text) if isinstance(text, str) else None


def kept(ctx: Ctx, a: Affordance, params: dict[str, Any], out: Any, events: list[str]) -> tuple[bool | None, str]:
    """Did the action keep its promise? (True / False / None = cannot be checked here), with the reason."""
    if not out.ok:
        return False, out.error or "the action failed"
    kind = promise(a, params)
    if kind == "typed":
        text = str(params.get("text", ""))
        if a.target.get("secure"):
            return None, "a password field: what it holds is never read back"
        ref = a.target.get("ref")
        # A document is longer than any readback: ask for room for the text several times over, and treat an
        # answer that fills it as "could not be read" rather than "wrong". Reporting a long file as failed
        # because the typed line sits past the cut is the worse mistake of the two.
        limit = max(2000, len(text) * 4)
        where = "the field" if ref else "where the cursor is"
        if not text.strip():
            return True, "there was nothing to type"
        # Read back until the field settles. A field is not written the instant the keys are sent: a long path
        # arrives character by character and an app may complete it as it goes, so one immediate read caught
        # "/Users/…/Caches/macwor" mid-flight and a real step was recorded as having broken its promise.
        deadline = time.monotonic() + float(ctx.cfg.get("engine.verify.readback_ms", 800)) / 1000
        now = seen = None
        while True:
            now = _field_text(ctx, ref) if ref else _cursor_text(ctx, limit)
            if now is None:
                return None, f"{'the field' if ref else 'the focused element'} could not be read back"
            if text.strip() in now:
                return True, f"the text is in {where}"
            if time.monotonic() >= deadline or now == seen:   # settled on something else: that is an answer
                break
            seen = now
            time.sleep(0.08)
        if len(now) >= limit:
            return None, f"{where} holds more text than can be read back"
        return False, f"{where} holds {now[:40]!r}, not what was typed"
    if kind == "switched":
        pid = (out.target or {}).get("pid") or a.target.get("pid")
        try:
            front = (ctx.helper.call("apps.frontmost") or {}).get("app") or {}
        except HelperError:
            return None, "the frontmost app could not be read"
        if pid and front.get("pid") != pid:
            return False, f"{front.get('name')} is in front, not the app that was asked for"
        return True, "that app is in front"
    if events:
        pid = (ctx.app or {}).get("pid")
        # a window that changes with nobody acting — a clock, live figures — reacts to everything. The
        # engine measures that once per window (`ambient`); its events are not evidence of this action
        if pid is not None and any(k.startswith(f"{pid}|") for k in ctx.cache.get("ambient", set())):
            return None, "the screen moved, but this app's window moves by itself"
        return True, "the screen reacted"
    return None, "nothing to check"
