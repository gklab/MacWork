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


def kept(ctx: Ctx, a: Affordance, params: dict[str, Any], out: Any, events: list[str]) -> tuple[bool | None, str]:
    """Did the action keep its promise? (True / False / None = cannot be checked here), with the reason."""
    if not out.ok:
        return False, out.error or "the action failed"
    kind = promise(a, params)
    if kind == "typed":
        text = str(params.get("text", ""))
        ref = a.target.get("ref")
        if not ref or a.target.get("secure"):
            return None, "typed at the cursor: nothing to read back"
        now = _field_text(ctx, ref)
        if now is None:
            return None, "the field could not be read back"
        if text.strip() and text.strip() not in now:
            return False, f"the field holds {now[:40]!r}, not what was typed"
        return True, "the text is in the field"
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
        return True, "the screen reacted"
    return None, "nothing to check"
