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


# Promises checked the moment the action returns, and promises checked at the next look — when the screen
# before and the screen after are both in hand. Nothing is "unknown": a press promises the screen will
# change, and a screen that does not is the fact the memory records.
IMMEDIATE = ("typed", "switched", "returned")
DEFERRED = ("changed", "opened", "selected")


def promise(a: Affordance, params: dict[str, Any]) -> str:
    """What this action says it will do, in one word.

    typed     the field holds the text                       (checked now, by reading it back)
    switched  that app is in front                           (checked now, against the front app)
    returned  it handed something back — a file's text,     (checked now: did it?)
              a window read in full, a Shortcut's result
    opened    a window or another app appears               (checked at the next look, from the change)
    selected  a row or a range is selected                  (checked at the next look, from the change)
    changed   the screen is not what it was                 (checked at the next look, from the change)
    """
    if a.verb in ("type", "type_submit") and params.get("text") is not None:
        return "typed"
    if a.verb == "activate" or (a.channel == "app" and a.verb in ("open", "reopen")):
        return "switched"
    if a.yields or a.verb in ("read", "read_all") or (a.channel == "clipboard" and a.verb == "read"):
        return "returned"
    if a.channel == "file" and a.verb == "open":
        return "opened"
    if a.verb in ("select_text", "cursor_end", "select"):
        return "selected"
    return "changed"


def _marked(said: dict[str, Any]) -> str:
    """What an input method is still composing in an element, as the helper says when asked with marked=True
    (protocol 0.2.0): the text of the element's marked range. "" when nothing is being composed, when the
    element publishes no marked range (the focused cell of a single-line AppKit field does not), and when the
    helper is older and says nothing — which reads the field as it was read before."""
    marked = said.get("marked")
    return marked if isinstance(marked, str) else ""


def _field_text(ctx: Ctx, ref: str) -> tuple[str | None, str]:
    """What the field holds (None when it cannot be read), and what of that is still being composed."""
    try:
        got = ctx.helper.call("ax.get", ref=ref, attribute="AXValue", marked=True)
    except HelperError:
        return None, ""
    return str(got.get("value") or ""), _marked(got)


def _cursor_text(ctx: Ctx, limit: int) -> tuple[str | None, str]:
    """What the element with the keyboard focus holds, or None when it may not or cannot be read; and what of
    it an input method is still composing.

    Typing at the cursor had no check at all: it goes wherever the system focus is, so there was no ref to read
    back and every such step came back "nothing to read back" — unverified, including the ones that went into
    the wrong window. The focus is a system-wide question and `ax.focused` answers it.

    It refuses while secure input is on. That is not a role test: it is the window server's own answer about
    whether a password field has focus anywhere, and the point is to never read a secret back, not to work out
    what kind of field this is.
    """
    try:
        r = ctx.helper.call("ax.focused", max_text=limit, marked=True)
    except HelperError:
        return None, ""
    if r.get("secure_input"):
        return None, ""
    node = r.get("focused")
    if not isinstance(node, dict):
        return None, ""
    # No AXValue at all is a canvas or a custom view — "cannot be read", not "reads as empty". An element
    # that does answer, with "", really is empty, and typing into it that leaves it empty did fail.
    text = node.get("selected_text") if node.get("selected_text") else node.get("value")
    return (str(text), _marked(node)) if isinstance(text, str) else (None, "")


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
            now, marked = _field_text(ctx, ref) if ref else _cursor_text(ctx, limit)
            if now is None:
                return None, f"{'the field' if ref else 'the focused element'} could not be read back"
            # AXValue holds what an input method is still composing as well as what was typed, so reading it
            # back passed 19 of the 21 typing steps on 09-20..23 that left the end of their text composing; the
            # other 2 failed because the input method's syllable separator changed the text. Those letters are
            # not in the field yet: the input method holds them until it is told what to make of them, and an
            # Escape at its panel deletes them ("hello" was left as "hel" or "hell" in 3 recorded tasks). Only an
            # element that publishes its marked range can say so; one that does not is read as before.
            if marked:
                return False, f"「{marked[:24]}」 is still being composed by the input method: not yet in {where}"
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
    if kind == "returned":
        if out.output:
            return True, "it handed something back"
        return False, "it was to hand something back, and handed back nothing"
    return None, "judged at the next look, from what changed"


def kept_by_change(step_promise: str, changed: bool, seen: bool) -> tuple[bool | None, str]:
    """The deferred promises, judged once the next look has shown what the action did.

    `changed`: the screen is not what it was (structure, text, an event, or the picture). `seen`: the
    engine could have seen a change — a step whose effect may not show in anything observed (a key held
    in a view that draws itself) with no glance before and after is not judged at all.
    """
    if step_promise not in DEFERRED:
        return None, ""
    if not seen:
        return None, "what it does may not show on screen, and the screen could not be compared"
    if changed:
        return True, "the screen changed"
    return False, "nothing on screen changed"
