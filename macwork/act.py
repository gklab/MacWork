"""Channels execute affordances. Each is ``(ctx, affordance, params) -> Outcome``; register more with
``@channel("name")`` or the ``macwork.channels`` entry point. Executors report which app to watch for
the effect; the engine does the waiting and the judging.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable

from .helper import HelperError
from .model import Affordance
from .observe import Ctx


@dataclass
class Outcome:
    ok: bool
    watch_pid: int | None = None         # app whose UI events show the effect
    target: dict[str, Any] | None = None # new app to work in (after switching/opening)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    wait: bool = True                    # False: the effect is not a UI change worth waiting for
    final: bool = False                  # the result itself answers the goal (e.g. web research)


Channel = Callable[[Ctx, Affordance, dict[str, Any]], Outcome]
CHANNELS: dict[str, Channel] = {}


def channel(name: str) -> Callable[[Channel], Channel]:
    def reg(fn: Channel) -> Channel:
        CHANNELS[name] = fn
        return fn
    return reg


def get_channel(name: str) -> Channel | None:
    if name in CHANNELS:
        return CHANNELS[name]
    for ep in entry_points(group="macwork.channels"):
        if ep.name == name:
            CHANNELS[name] = ep.load()
            return CHANNELS[name]
    return None


def _frontmost(ctx: Ctx) -> dict[str, Any] | None:
    return (ctx.helper.call("apps.frontmost") or {}).get("app")


class NotInFront(RuntimeError):
    pass


def _bring_forward(ctx: Ctx, pid: int) -> None:
    """Keystrokes go to whatever app is frontmost, so before typing the target MUST be in front — verified, not
    assumed. Accessibility activation first, LaunchServices second; if neither works, nothing is typed.
    Taking the front app away is only allowed while the user is not using the Mac (see Engine.take_hands)."""
    if ctx.hands is not None and not ctx.hands():
        raise NotInFront("the Mac is in use: the front app was left alone")
    def front_pid() -> int | None:
        return (_frontmost(ctx) or {}).get("pid")

    if front_pid() == pid:
        return
    wait = float(ctx.cfg.get("engine.activate_wait_s", 1.5))
    ctx.helper.call("apps.activate", pid=pid)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if front_pid() == pid:
            return
        time.sleep(0.1)
    app = next((a for a in ctx.running or ctx.helper.call("apps.running") if a.get("pid") == pid), None)
    if app and (app.get("path") or app.get("bundle_id")):   # LaunchServices may activate where a background process cannot
        subprocess.run(["open", "-a", app["path"]] if app.get("path") else ["open", "-b", app["bundle_id"]], capture_output=True, timeout=10, check=False)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if front_pid() == pid:
                time.sleep(float(ctx.cfg.get("engine.activate_settle_s", 0.3)))   # let its key window take focus
                return
            time.sleep(0.1)
    raise NotInFront(f"could not bring {(app or {}).get('name', pid)} to the front; nothing was typed")


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _type(ctx: Ctx, text: str) -> None:
    ctx.helper.call("input.type", text=text, non_ascii=str(ctx.cfg.get("input.non_ascii", "paste")), timeout=60)


def _focus_field(ctx: Ctx, t: dict[str, Any]) -> None:
    """Give a field keyboard focus: click it like a person where its frame is known (some apps ignore AX focus)."""
    f = t.get("frame")
    if ctx.cfg.get("input.focus_by_click", True) and f and f[2] > 2 and f[3] > 2:
        ctx.helper.call("input.click", x=f[0] + f[2] / 2, y=f[1] + f[3] / 2)
        time.sleep(0.15)
        return
    try:
        ctx.helper.call("ax.set", ref=t["ref"], attribute="AXFocused", value=True)
    except HelperError:
        ctx.helper.call("ax.perform", ref=t["ref"], action="AXPress")


@channel("app")
def app_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    t = a.target
    if a.verb == "activate":
        ctx.helper.call("apps.activate", pid=t["pid"])
        return Outcome(True, watch_pid=t["pid"], target={"pid": t["pid"], "name": t.get("name"), "bundle_id": t.get("bundle_id")})
    if a.verb == "reopen":          # like clicking its Dock icon: LaunchServices sends the app a reopen event
        r = subprocess.run(["open", "-a", t["path"]], capture_output=True, text=True, timeout=15, check=False)
        return Outcome(r.returncode == 0, watch_pid=t["pid"], error=(r.stderr or "").strip()[:200] or None)
    r = subprocess.run(["open", "-a", t["path"]], capture_output=True, text=True, timeout=15, check=False)
    if r.returncode != 0:
        return Outcome(False, error=r.stderr.strip()[:200] or "open failed")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:   # wait for the process so later steps can talk to it
        for app in ctx.helper.call("apps.running"):
            if app.get("path") == t["path"] or (t.get("bundle_id") and app.get("bundle_id") == t["bundle_id"]):
                return Outcome(True, watch_pid=app["pid"], target={"pid": app["pid"], "name": app.get("name"), "bundle_id": app.get("bundle_id")})
        time.sleep(0.2)
    return Outcome(False, error="app did not start")


@channel("menu")
def menu_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    ctx.helper.call("ax.perform", ref=a.target["ref"], action="AXPress")
    return Outcome(True, watch_pid=a.target["pid"])


@channel("window")
def window_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    t = a.target
    if a.verb == "read_all":        # everything the window says, not only what is on screen
        rc = ctx.cfg.section("observe.readall")
        snap = ctx.helper.call("ax.snapshot", pid=t["pid"], scope="focused_window", visible_only=False,
                               max_nodes=int(rc.get("max_nodes", 12000)), max_depth=int(rc.get("max_depth", 60)),
                               max_children=int(rc.get("max_children", 2000)), max_text=int(rc.get("max_text", 4000)),
                               budget_ms=int(rc.get("budget_ms", 8000)), actions=False, timeout=30)
        seen: list[str] = []
        for n in snap.get("nodes", []):
            for value in (n.get("value"), n.get("title"), n.get("desc")):
                text = str(value or "").strip()
                if text and text not in seen:
                    seen.append(text)
        whole = "\n".join(seen)[: int(rc.get("chars", 40000))]
        if not whole:
            return Outcome(False, error="there is no readable text in this window", wait=False)
        return Outcome(True, output={"read_window": {"app": (ctx.app or {}).get("name") or "", "text": whole,
                                                     "truncated": bool(snap.get("truncated"))}}, wait=False)
    if a.verb == "raise":           # bring another window of the app to the front
        ctx.helper.call("ax.perform", ref=t["ref"], action="AXRaise")
        _bring_forward(ctx, t["pid"])
        return Outcome(True, watch_pid=t["pid"])
    if a.verb in ("select_text", "cursor_end"):   # a range of a document's text (AX counts UTF-16 units)
        value = str(ctx.helper.call("ax.get", ref=t["ref"], attribute="AXValue").get("value") or "")
        if a.verb == "cursor_end":
            loc, length = _utf16(value), 0
        else:
            want = str(params.get("selection", ""))
            i = value.find(want) if want else -1
            if i < 0:
                i = value.casefold().find(want.casefold()) if want else -1
            if i < 0:
                return Outcome(False, error=f"「{want[:40]}」 is not in that text", wait=False)
            loc, length = _utf16(value[:i]), _utf16(value[i:i + len(want)])
        _bring_forward(ctx, t["pid"])
        try:
            ctx.helper.call("ax.set", ref=t["ref"], attribute="AXFocused", value=True)
        except HelperError:
            pass
        ctx.helper.call("ax.set_range", ref=t["ref"], location=loc, length=length)
        return Outcome(True, watch_pid=t["pid"])
    if a.verb == "select":          # a row in a list/table/outline: select it; click it where selecting is refused
        try:
            ctx.helper.call("ax.set", ref=t["ref"], attribute="AXSelected", value=True)
        except HelperError:
            f = t.get("frame")
            if not f:
                raise
            _bring_forward(ctx, t["pid"])
            ctx.helper.call("input.click", x=f[0] + min(f[2] / 2, 60), y=f[1] + f[3] / 2)
        return Outcome(True, watch_pid=t["pid"])
    if a.verb not in ("type", "type_submit"):
        ctx.helper.call("ax.perform", ref=t["ref"], action=t.get("action") or "AXPress")
        return Outcome(True, watch_pid=t.get("watch") or t["pid"])   # a prompt over the app: watch the app
    text = str(params.get("text", ""))
    if a.verb == "type_submit":   # real keystrokes: apps like browsers ignore a value set behind their back
        _bring_forward(ctx, t["pid"])
        _focus_field(ctx, t)
        ctx.helper.call("input.key", combo="cmd+a")
        _type(ctx, text)
        time.sleep(0.1)
        ctx.helper.call("input.key", combo="return")
        return Outcome(True, watch_pid=t["pid"])
    # like a person: focus the field, select what is in it, type. Setting the value behind the app's back looks
    # right in the Accessibility tree but many fields never hear of it (Finder's "Go to Folder", browser address
    # bars): the app keeps using its old text. The direct set is only the fallback when the app cannot be brought
    # to the front (it also never touches the clipboard).
    if not ctx.cfg.get("input.set_value_first", False):
        try:
            _bring_forward(ctx, t["pid"])
            _focus_field(ctx, t)
            ctx.helper.call("input.key", combo="cmd+a")
            _type(ctx, text)
            return Outcome(True, watch_pid=t["pid"])
        except NotInFront:
            pass
    try:
        ctx.helper.call("ax.set", ref=t["ref"], attribute="AXValue", value=text)
        if t.get("secure") or ctx.helper.call("ax.get", ref=t["ref"], attribute="AXValue").get("value") == text:
            ctx.helper.call("ax.set", ref=t["ref"], attribute="AXFocused", value=True)
            return Outcome(True, watch_pid=t["pid"])
    except HelperError:
        pass
    _bring_forward(ctx, t["pid"])
    _focus_field(ctx, t)
    ctx.helper.call("input.key", combo="cmd+a")
    _type(ctx, text)
    return Outcome(True, watch_pid=t["pid"])


@channel("keys")
def keys_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    if ctx.app:
        _bring_forward(ctx, ctx.app["pid"])
    if a.verb in ("type", "type_submit"):
        _type(ctx, str(params.get("text", "")))
        if a.verb == "type_submit":
            ctx.helper.call("input.key", combo="return")
        return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
    ctx.helper.call("input.key", combo=a.target["combo"])
    return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))


@channel("shortcut")
def shortcut_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    """Run one of the person's own Shortcuts.

    This is also as close as anything gets to *invoking* an App Intent. An app declares its intents and the
    engine reads them (`appmodel.parse_app_intents`), but there is no public way for one process to perform
    another app's intent: the system performs them, through Shortcuts, Siri and Spotlight. A shortcut the
    person made is that route, already built and already authorised by them.

    `shortcuts run` writes what the shortcut *returns* to `--output-path`, not to stdout, so without asking
    for one the result was dropped on the floor — a shortcut that looks something up ran, succeeded, and
    told the task nothing.
    """
    cmd = ["shortcuts", "run", a.target["name"]]
    limit = int(ctx.cfg.get("observe.shortcuts.output_chars", 4000))
    with tempfile.TemporaryDirectory() as box:
        out_path = pathlib.Path(box) / "out"
        cmd += ["--output-path", str(out_path)]
        if params.get("input"):
            in_path = pathlib.Path(box) / "in.txt"
            in_path.write_text(str(params["input"]), encoding="utf-8")
            cmd += ["--input-path", str(in_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        result = ""
        try:
            if out_path.exists():
                result = out_path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:      # it can return a file of any kind; one that is not text is not an error here
            result = ""
    output: dict[str, Any] = {}
    if r.stdout.strip():
        output["stdout"] = r.stdout[:2000]
    if result.strip():
        # what it returned is something the task has now seen, so it may be reported and written back
        output["shortcut_result"] = {"name": a.target["name"], "text": result}
    return Outcome(r.returncode == 0, output=output, error=r.stderr.strip()[:200] or None, wait=False)


@channel("service")
def service_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    files = [str(params["file"])] if params.get("file") else []
    out = ctx.helper.call("services.perform", name=a.target["name"], text=str(params.get("text", "")), files=files)
    if not out.get("ok"):
        return Outcome(False, error=f"the system did not run the service 「{a.target['name']}」", wait=False)
    time.sleep(0.4)                       # a service usually brings its app forward
    front = _frontmost(ctx) or {}
    return Outcome(True, watch_pid=front.get("pid"),
                   target={"pid": front["pid"], "name": front.get("name"), "bundle_id": front.get("bundle_id")} if front.get("pid") else None)


@channel("clipboard")
def clipboard_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    out = ctx.helper.call("clipboard.write", text=str(params.get("text", "")))
    return Outcome(True, output={"clipboard_change_count": out.get("change_count")}, wait=False)


@channel("file")
def file_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    if a.verb == "read":        # its text becomes a fact, so the task may write what the file said
        path = str(a.target["path"])
        try:
            got = ctx.helper.call("file.read_text", path=path, max_chars=int(ctx.cfg.get("observe.files.read_chars", 20000)))
        except HelperError as exc:
            return Outcome(False, error=str(exc), wait=False)
        return Outcome(True, output={"file_read": {"path": path, "kind": got.get("kind"), "text": got.get("text", "")}}, wait=False)
    what = str(params.get("url") or a.target["path"])
    args = ["open", "-R", what] if a.verb == "reveal" else ["open", what]
    r = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    time.sleep(0.4)
    front = _frontmost(ctx) or {}
    return Outcome(r.returncode == 0, watch_pid=front.get("pid"), error=r.stderr.strip()[:200] or None,
                   target={"pid": front["pid"], "name": front.get("name"), "bundle_id": front.get("bundle_id")} if front.get("pid") else None)


@channel("script")
def script_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    if not ctx.cfg.policy.get("allow", {}).get("raw_applescript"):
        return Outcome(False, error="raw AppleScript is disabled by policy (allow.raw_applescript)", wait=False)
    res = ctx.helper.call("script.applescript", source=str(params.get("source", "")), timeout=60)
    return Outcome(True, output={"result": res.get("result")}, watch_pid=(ctx.app or {}).get("pid"))


def _as_literal(value: Any, typ: str) -> str:
    """A caller-supplied value as an AppleScript literal of the declared type (never raw code)."""
    t = (typ or "text").split("|")[0]
    s = str(value)
    if t in ("integer", "real", "number"):
        float(s)                       # raises on anything that is not a number
        return s
    if t == "boolean":
        return "true" if s.strip().lower() in ("1", "true", "yes", "on") else "false"
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'POSIX file "{esc}"' if t == "file" else f'"{esc}"'


@channel("script_cmd")
def script_cmd_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    """A command from the app's own scripting dictionary, e.g. `playpause` or `open location "…"`."""
    if not ctx.cfg.policy.get("allow", {}).get("sdef_commands", True):
        return Outcome(False, error="scripting commands are disabled by policy (allow.sdef_commands)", wait=False)
    t = a.target
    parts = [t["command"]]
    try:
        if t.get("direct") and "direct" in params:
            parts.append(_as_literal(params["direct"], t["direct"]["type"]))
        for p in t.get("params", []):
            key = re.sub(r"\W+", "_", p["name"])
            if key in params:
                parts.append(f"{p['name']} {_as_literal(params[key], p['type'])}")
    except ValueError as exc:
        return Outcome(False, error=f"bad parameter: {exc}", wait=False)
    source = f'tell application id "{t["bundle_id"]}"\n{" ".join(parts)}\nend tell'
    try:
        res = ctx.helper.call("script.applescript", source=source, timeout=60)
    except HelperError as exc:
        return Outcome(False, error=str(exc)[:300], wait=False)
    return Outcome(True, output={"result": res.get("result")} if res.get("result") else {}, watch_pid=(ctx.app or {}).get("pid"))


@channel("menusearch")
def menusearch_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    """Open the menu that holds the search field and type the words; matches appear as menu items to pick next."""
    t = a.target
    _bring_forward(ctx, t["pid"])
    ctx.helper.call("ax.perform", ref=t["menu_ref"], action="AXPress")
    time.sleep(0.2)
    try:
        ctx.helper.call("ax.set", ref=t["field_ref"], attribute="AXValue", value=str(params.get("text", "")))
    except HelperError:
        ctx.helper.call("input.type", text=str(params.get("text", "")))
    return Outcome(True, watch_pid=t["pid"])


@channel("pointer")
def pointer_channel(ctx: Ctx, a: Affordance, params: dict[str, Any]) -> Outcome:
    """Act on a point rather than on an element: for windows the Accessibility tree cannot describe.

    A canvas app — an editor that draws its own text, a design tool, a game — publishes a window and nothing
    inside it. What is there can still be read (on-device OCR), and what can be read can be pointed at. This
    is the channel for that, and it is deliberately the last resort: a point is not an element, so nothing
    here can be verified by reading a value back, and the window moving invalidates every coordinate.
    """
    t = a.target
    if ctx.app:
        _bring_forward(ctx, ctx.app["pid"])
    if a.verb == "drag":            # from one element to another (a planner suggestion, resolved on the live screen)
        ctx.helper.call("input.drag", x1=float(t["x1"]), y1=float(t["y1"]), x2=float(t["x2"]), y2=float(t["y2"]))
        return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
    if a.verb == "drag_named":      # both ends named by the caller, looked up among what was on screen
        spots = t.get("spots") or {}

        def centre(name: str) -> tuple[float, float] | None:
            frame = spots.get(name) or next((f for label, f in spots.items() if name and name in label), None)
            return (frame[0] + frame[2] / 2, frame[1] + frame[3] / 2) if frame else None
        start, end = centre(str(params.get("from", ""))), centre(str(params.get("onto", "")))
        if start is None or end is None:
            missing = "from" if start is None else "onto"
            return Outcome(False, error=f"「{params.get(missing)}」 is not on screen", wait=False)
        ctx.helper.call("input.drag", x1=start[0], y1=start[1], x2=end[0], y2=end[1])
        return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
    if a.verb == "scroll":          # a canvas has no scroll area to perform an action on; the wheel still works
        win = t.get("window_frame") or t.get("frame") or [0, 0, 0, 0]
        ctx.helper.call("input.scroll", x=win[0] + win[2] / 2, y=win[1] + win[3] / 2, dy=int(t.get("dy", 0)))
        return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
    if a.verb == "click_named":     # the target is named by the caller and looked up among what was read
        spots = t.get("spots") or {}
        want = str(params.get("what", ""))
        frame = spots.get(want) or next((f for label, f in spots.items() if want and want in label), None)
        if frame is None:
            return Outcome(False, error=f"「{want}」 is not on screen", wait=False)
        ctx.helper.call("input.click", x=frame[0] + frame[2] / 2, y=frame[1] + frame[3] / 2,
                        button=t.get("button", "left"), count=int(t.get("count", 1)))
        return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
    ctx.helper.call("input.click", x=float(t["x"]), y=float(t["y"]),
                    button=t.get("button", "left"), count=int(t.get("count", 1)))
    return Outcome(True, watch_pid=(ctx.app or {}).get("pid"))
