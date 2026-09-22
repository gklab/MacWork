"""Evaluation on apps nobody tuned for: run each task through the engine, check the result on screen, clean up,
and write a report. The suite is YAML (see evals/unseen.yaml); checks and clean-up steps are declarative.

Task fields:
  id, app (optional, where to start), goal, inputs (optional)
  setup:   steps run before the task so leftovers from earlier runs cannot make it pass (same forms as cleanup)
  check:   {screen_contains: [...], window_contains: [...], frontmost: name|bundle, output_contains: [...]}  (any string matches)
  cleanup: [{key: "cmd+w"}, {press: ["删除", "Don't Save", …]}, {activate: true}, {wait: 0.5},
            {close_untitled: ["未命名", "Untitled"], dont_save: ["删除", "Don't Save", …]}]
  close_untitled only touches windows whose title starts with one of the given prefixes (documents the task
  created), through each window's own close button, and discards them — named documents are never touched.
A task that failed because the decider could not be reached is reported ``error`` and not counted.

``fresh: true`` (the default) measures what matters for unseen apps: no learned routines offered or recorded, and
an empty app-model directory — every task is solved by reasoning on the live screen, not by recall. Set
``fresh: false`` to measure how much faster a warmed-up engine gets.
Tasks must be harmless: nothing is saved, sent, bought or deleted.

Suite v2 additions (see evals/v2.yaml):
  repeat: N          every task runs N times; the report gives k/N per task, the mean with a Wilson 95% interval,
                     pass^N (tasks that passed every time) and the same per category
  sandbox: true      a scratch folder (engine config evals.sandbox) is created fresh for each run and removed after;
                     ``{eval_dir}`` in goals, inputs and checks is replaced by its path; nothing else may be written
  task fields:       category, fixtures [{path, text | base64}], check_app (where screen checks look, if not app)
  checks:            expect_status (one or a list; default done), screen_excludes, file_exists, file_missing,
                     file_contains {path: text}, answer_contains (the engine's outputs.answer), trace_excludes
                     (regexes over the actions taken and text typed — for injection and must-not tasks)
A task passes only when the engine itself reports ``done`` AND the check holds. A task whose check already
holds before it starts (left over from an earlier run) is reported as ``invalid`` and not counted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from .act import NotInFront, _bring_forward
from .appmodel import _info_plist
from .decider import choice
from .engine import Engine
from .observe import Ctx, observe


def _text_of(engine: Engine, app: Any) -> tuple[str, str, str]:
    """(window title, screen text + all labels, frontmost app name/bundle) as the engine sees them now."""
    ctx = engine._ctx("", {}, app, "eval")
    obs = observe(ctx)
    labels = "\n".join(a.label for a in obs.affordances if a.channel in ("window", "pointer"))
    front = (engine.helper.call("apps.frontmost") or {}).get("app") or {}
    return obs.window or "", f"{obs.screen_text}\n{labels}", f"{front.get('name', '')} {front.get('bundle_id', '')}"


SCREEN_CHECKS = ("screen_contains", "window_contains", "frontmost")


def _wants(v: Any) -> list[str]:
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def check(engine: Engine, task: dict[str, Any], result: dict[str, Any], only: tuple[str, ...] | None = None) -> tuple[bool, str]:
    spec = {k: v for k, v in (task.get("check") or {}).items() if k != "expect_status" and (only is None or k in only)}
    if not spec:
        return True, "no check"
    window = screen = front = ""
    if any(k in spec for k in (*SCREEN_CHECKS, "screen_excludes")):
        window, screen, front = _text_of(engine, task.get("check_app") or result.get("_app") or task.get("app"))
    outputs = result.get("outputs", {}) or {}
    trace = "\n".join(result.get("steps", [])
                      + [str(t.get("text", "")) for t in outputs.get("typed_by_planner", [])]
                      + [str(t.get("text", "")) for t in outputs.get("planner_inputs", [])])
    fields = {"screen_contains": screen + "\n" + window, "window_contains": window, "frontmost": front,
              "output_contains": json.dumps(outputs, ensure_ascii=False), "answer_contains": str(outputs.get("answer", ""))}
    for key, want in spec.items():
        if key in fields:
            hay = fields[key]
            if not any(w.casefold() in hay.casefold() for w in _wants(want)):
                return False, f"{key}: none of {_wants(want)} in {hay[:160]!r}"
        elif key == "screen_excludes":
            hit = [w for w in _wants(want) if w.casefold() in (screen + "\n" + window).casefold()]
            if hit:
                return False, f"screen_excludes: {hit} still on screen"
        elif key == "trace_excludes":
            hit = [rx for rx in _wants(want) if re.search(rx, trace, re.I)]
            if hit:
                return False, f"trace_excludes: {hit} in what was done"
        elif key == "app_changed":
            # the app shows something it did not before the task — a new window, or the same window now
            # saying other things. Settings open in a window in one app and in the window in the next
            # (an Electron app's route, a Catalyst app's pane), and a check that only counts windows
            # failed two tasks the engine had done.
            if not result.get("_app_changed"):
                return False, "app_changed: the app shows nothing it did not before the task"
        elif key == "app_windows_grew":
            # a window of the task's app that was not there before it — a settings window, a document —
            # judged without knowing what the window is called in this app or this language
            grew = result.get("_windows_grew") or {}
            pid = (engine._resolve_app(task.get("app"), engine.helper.call("apps.running")) or {}).get("pid")
            if not grew.get(pid):
                return False, "app_windows_grew: no new window of the app is on screen"
        elif key in ("file_exists", "file_missing"):
            for f in _wants(want):
                if Path(f).expanduser().exists() != (key == "file_exists"):
                    return False, f"{key}: {f}"
        elif key == "file_contains":
            for f, text in (want or {}).items():
                path = Path(f).expanduser()
                if not path.exists() or str(text) not in path.read_text(errors="replace"):
                    return False, f"file_contains: {f} lacks {text!r}"
        else:
            return False, f"unknown check {key}"
    return True, "checked"


def _expected(task: dict[str, Any]) -> list[str]:
    exp = (task.get("check") or {}).get("expect_status")
    if exp:
        return _wants(exp)
    return ["done", "blocked"] if task.get("accept_blocked") else ["done"]


def _sub(value: Any, eval_dir: str) -> Any:
    """``{eval_dir}`` and ``{day}`` (today's day of the month, as a number) in any string of a task.

    A check for a date used to look for 「日」, which is also the second character of 「日期」: an answer saying
    "the date is not shown" passed it. What today is, the harness knows."""
    if isinstance(value, str):
        return value.replace("{eval_dir}", eval_dir).replace("{day}", str(time.localtime().tm_mday))
    if isinstance(value, list):
        return [_sub(v, eval_dir) for v in value]
    if isinstance(value, dict):
        return {_sub(k, eval_dir): _sub(v, eval_dir) for k, v in value.items()}
    return value


def _sandbox(engine: Engine, task: dict[str, Any]) -> Path:
    root = Path(str(engine.cfg.get("evals.sandbox", "~/Library/Caches/macwork-sandbox"))).expanduser()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for f in task.get("fixtures") or []:
        path = root / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if "base64" in f:
            path.write_bytes(base64.b64decode(f["base64"]))
        elif f.get("dir"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text(str(f.get("text", "")), encoding="utf-8")
    return root


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a success rate from k of n (Wilson score): honest for small n."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def _launch(engine: Engine, app_hint: str) -> int | None:
    """Open the task's app in the background if it is not running; return its pid when the harness opened it."""
    if engine._resolve_app(app_hint, engine.helper.call("apps.running")):
        return None
    inst = engine._installed_named(app_hint)   # the same resolution the engine uses: name, bundle id, or file name
    if inst:
        import subprocess
        subprocess.run(["open", "-g", "-a", inst["path"]], capture_output=True, timeout=15, check=False)   # -g: stay in the background
        # until it is running, not for a fixed two seconds: 2.1 s of every task's harness time was this
        deadline = time.monotonic() + 6.0
        opened = None
        while opened is None and time.monotonic() < deadline:
            time.sleep(0.2)
            opened = engine._resolve_app(app_hint, engine.helper.call("apps.running"))
        time.sleep(0.5)      # a moment for its first window: apps restore their last state on launch
        return opened.get("pid") if opened else None
    return None


def _close_untitled(engine: Engine, app_hint: str, prefixes: list[str], dont_save: list[str]) -> int:
    app = engine._resolve_app(app_hint, engine.helper.call("apps.running")) if app_hint else None
    if not app:
        return 0
    closed = 0
    for _ in range(10):
        nodes = engine.helper.call("ax.snapshot", pid=app["pid"], scope="windows", max_depth=5, max_nodes=600)["nodes"]
        by = {n["ref"]: n for n in nodes}
        btn = next((n for n in nodes if n.get("role") == "AXButton" and n.get("title") in dont_save), None)
        if btn:        # a save sheet is up for one of our untitled documents: discard
            engine.helper.call("ax.perform", ref=btn["ref"], action="AXPress")
            time.sleep(0.6)
            continue
        close = next((n for n in nodes if n.get("subrole") == "AXCloseButton" and
                      any(str(by.get(n.get("parent"), {}).get("title") or "").startswith(p) for p in prefixes)), None)
        if not close:
            break
        engine.helper.call("ax.perform", ref=close["ref"], action="AXPress")
        closed += 1
        time.sleep(0.8)
    return closed


def cleanup(engine: Engine, task: dict[str, Any], key: str = "cleanup") -> None:
    for step in task.get(key) or []:
        try:
            if "close_untitled" in step:   # documents live where the result was checked (cross-app tasks: check_app)
                for app in dict.fromkeys(filter(None, [step.get("app"), task.get("check_app"), task.get("app")])):
                    _close_untitled(engine, app, [str(p) for p in step["close_untitled"]], [str(d) for d in step.get("dont_save") or []])
                continue
            if "key" in step:
                # keys go to whatever is in front: only send them once the task's app is verifiably there
                # (never let a cmd+q/cmd+w meant for the app land on the terminal running this)
                running = engine.helper.call("apps.running")
                target = step.get("app") or task.get("app")
                app = engine._resolve_app(target, running) if target else None
                if app is None:
                    continue
                try:
                    _bring_forward(Ctx(engine.cfg, engine.helper, running=running), app["pid"])
                except NotInFront:
                    continue
                engine.helper.call("input.key", combo=step["key"])
            elif "press" in step:   # the dialog may take a moment to appear
                names = [str(n) for n in step["press"]]
                deadline = time.monotonic() + float(step.get("within", 3))
                while time.monotonic() < deadline:
                    obs = engine.observe(task.get("app"), limit=400)
                    hit = next((a for a in obs["affordances"] if a["channel"] == "window" and any(re.search(rf"「{re.escape(n)}」", a["label"]) for n in names)), None)
                    if hit:
                        engine.act(hit["id"], confirm=True)
                        break
                    time.sleep(0.4)
            elif "wait" in step:
                pass
            time.sleep(float(step.get("wait", 0.4)))
        except Exception:  # noqa: BLE001  (clean-up is best effort)
            continue


def _real_windows(engine: Engine) -> dict[int, set[int]]:
    """Windows a person would call windows, by owner: on screen, at the ordinary level.

    The window server lists every window object a process owns — toolbars, tooltips, popovers, unrealised
    panels at [0, 0, 100, 30] — and a task that took no step at all was reported as leaving four TextEdit
    "windows" behind, which the sweep then could not find as anything Accessibility calls a window. What
    tidy closes and what the sweep matches are Accessibility windows; this counts the same things.
    """
    try:
        wins = engine.helper.call("screen.windows", all=True) or []   # every Space: a window left on another one is still left
    except Exception:  # noqa: BLE001  (a helper hiccup is not a leftover)
        return {}
    out: dict[int, set[int]] = {}
    for w in wins:
        f = w.get("frame") or [0, 0, 0, 0]
        if w.get("id") is None or not w.get("alpha", 1):
            continue
        # the ordinary level, visible, and big enough to be a window rather than a toolbar strip or a
        # tooltip: an afternoon of runs left a Mac full of windows on other Spaces that a sweep of the
        # current one called clean
        if w.get("layer", 0) == 0 and f[2] > 100 and f[3] > 60:
            out.setdefault(int(w["pid"]), set()).add(int(w["id"]))
        # …and a dialog *above* the ordinary level: a permission prompt an app the run launched made the
        # system put up. It belongs to no app the sweep quits, it stays through everything that follows,
        # and one of them sat over a whole suite — every task pressed Escape at it and ended blocked.
        # Big enough to be a dialog, not a menu bar item or a banner.
        elif w.get("layer", 0) > 0 and f[2] >= 200 and f[3] >= 100:
            out.setdefault(int(w["pid"]), set()).add(int(w["id"]))
    return out


def _dialogs_above(engine: Engine, before: dict[int, set[int]]) -> list[dict[str, Any]]:
    """Windows above the ordinary level that were not there before, by owner — what no task can get past."""
    try:
        wins = engine.helper.call("screen.windows", all=True) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for w in wins:
        f = w.get("frame") or [0, 0, 0, 0]
        if w.get("layer", 0) > 0 and f[2] >= 200 and f[3] >= 100 and w.get("id") is not None and w.get("alpha", 1) \
                and int(w["id"]) not in before.get(int(w["pid"]), set()):      # alpha 0: a launcher's hidden panel is not on screen
            out.append({"app": w.get("owner") or "?", "pid": int(w["pid"]), "new_app": False, "windows": 1,
                        "ids": [int(w["id"])], "dialog": True, "title": str(w.get("title") or "")[:80]})
    return out


def _desktop(engine: Engine) -> tuple[dict[int, set[int]], set[int]]:
    """Windows on screen (per app) and running apps: what a task must leave as it found it."""
    return _real_windows(engine), {a["pid"] for a in engine.helper.call("apps.running")}


def _leftovers(engine: Engine, before: tuple[dict[int, set[int]], set[int]]) -> list[dict[str, Any]]:
    wins0, pids0 = before
    wins1, _ = _desktop(engine)
    host = engine._host_bundles()
    out = []
    for a in engine.helper.call("apps.running"):
        if a.get("bundle_id") in host:
            continue
        if a["pid"] not in pids0:
            out.append({"app": a["name"], "pid": a["pid"], "new_app": True, "windows": len(wins1.get(a["pid"], set()))})
        else:
            extra = wins1.get(a["pid"], set()) - wins0.get(a["pid"], set())
            if extra:
                out.append({"app": a["name"], "pid": a["pid"], "new_app": False, "windows": len(extra), "ids": sorted(extra)})
    seen = {x["pid"] for x in out}
    out += [d for d in _dialogs_above(engine, wins0) if d["pid"] not in seen]   # the system's own prompts: no app of the list owns them
    return out


def _discard_prompt(engine: Engine, pid: int) -> bool:
    """A sheet asking about unsaved work: throw the work away. True when a button was pressed.

    The engine itself never does this — `tidy._quit` deliberately cancels a quit that asks about unsaved work,
    because the work is the user's. An eval sandbox is the one place where the opposite is true: its documents
    are made by the suite, for the suite, and leaving them piles up windows that change what the next run
    sees (Grapher reached ten, and the eleventh run was scored INVALID because a chart was already there).

    Which button discards is asked, not looked up. This used to be a tuple of button titles in four
    languages — the kind of list this project exists to not have, and one that a fifth language, or an app
    that words it differently, walks straight past.
    """
    try:
        nodes = engine.helper.call("ax.snapshot", pid=pid, scope="windows", max_depth=6, max_nodes=800).get("nodes", [])
    except Exception:  # noqa: BLE001
        return False
    by_ref = {n.get("ref"): n for n in nodes}

    def within_prompt(n: dict[str, Any]) -> bool:      # under a sheet, or in a dialog window
        seen = 0
        while n is not None and seen < 12:
            if n.get("role") == "AXSheet" or n.get("subrole") in ("AXDialog", "AXSystemDialog"):
                return True
            n, seen = by_ref.get(n.get("parent")), seen + 1
        return False

    buttons = [n for n in nodes if n.get("role") == "AXButton" and (n.get("title") or "").strip() and within_prompt(n)]
    if len(buttons) < 2:
        return False
    says = " / ".join(dict.fromkeys(str(n.get("value") or n.get("title")) for n in nodes
                                    if n.get("role") == "AXStaticText" and within_prompt(n) and (n.get("value") or n.get("title"))))[:300]
    options = {"none": "none of these throws it away"} | {f"b{i}": str(b["title"]).strip() for i, b in enumerate(buttons)}
    try:
        ans = engine.gate.decide(engine.redactor("harness"), {"the_app_asks": says},
                                 {"discard": choice(engine.cfg.question("discard_sandbox"), options)}, task="harness")
    except Exception:  # noqa: BLE001  (no decider: the window stays, and is reported as left over)
        return False
    pick = (ans.get("discard") or {}).get("choice", "none")
    if pick not in options or pick == "none":
        return False
    try:
        engine.helper.call("ax.perform", ref=buttons[int(pick[1:])]["ref"], action="AXPress")
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.4)
    return True


def sweep(engine: Engine, before: tuple[dict[int, set[int]], set[int]]) -> list[dict[str, Any]]:
    """The harness puts the desktop back as the task found it, whatever the engine left: new apps are quit
    (politely), new windows closed by their own close button, dialogs without one dismissed with Escape (only
    once their app is verifiably in front). Returns what could not be removed."""
    from .model import Task
    left = _leftovers(engine, before)
    if not left:
        return []
    task = Task(goal="put the desktop back as it was")
    engine.tasks[task.id] = task
    for item in left:
        try:
            if item["new_app"]:
                for _ in range(4):     # each unsaved document asks separately
                    engine._quit(task, {"pid": item["pid"], "name": item["app"]})
                    if not _alive(engine, item["pid"]) or not _discard_prompt(engine, item["pid"]):
                        break
                if _alive(engine, item["pid"]):
                    # it was not running when the run began, it was asked politely, and it is still here:
                    # an app of the harness's own making does not get to stay on the person's Mac
                    engine.helper.call("apps.quit", pid=item["pid"], force=True, timeout_ms=3000, timeout=15)
                    log_harness(f"sweep: {item['app']} would not quit and was forced")
                continue
            if item.get("dialog"):
                # A prompt above every window, owned by the system on behalf of an app the run launched. The
                # harness never answers it — Allow is a decision that is the person's — it steps back from it:
                # Escape is the one key every such prompt takes as "not now".
                log_harness(f"a dialog above every window is on screen: 「{item.get('title') or item['app']}」 — pressing Escape")
                try:
                    engine.helper.call("apps.activate", pid=item["pid"])
                except Exception:  # noqa: BLE001
                    pass
                engine.helper.call("input.key", combo="escape")
                time.sleep(0.5)
                continue
            nodes = engine.helper.call("ax.snapshot", pid=item["pid"], scope="windows", max_depth=1, max_nodes=400).get("nodes", [])
            screen = {int(w["id"]): w for w in engine.helper.call("screen.windows") if w.get("pid") == item["pid"]}
            for wid in item.get("ids", []):
                frame = (screen.get(wid) or {}).get("frame")
                win = next((n for n in nodes if n.get("role") in ("AXWindow", "AXSheet") and frame and n.get("frame")
                            and all(abs(x - y) <= 3 for x, y in zip(n["frame"], frame))), None)
                close = win and next((n for n in nodes if n.get("parent") == win["ref"] and n.get("subrole") == "AXCloseButton"), None)
                if close:
                    engine.helper.call("ax.perform", ref=close["ref"], action="AXPress")
                    time.sleep(0.5)
                    _discard_prompt(engine, item["pid"])   # "save this?" — in a sandbox, no
                    continue
                try:
                    _bring_forward(Ctx(engine.cfg, engine.helper, running=engine.helper.call("apps.running")), item["pid"])
                    engine.helper.call("input.key", combo="escape")
                    time.sleep(0.4)
                except NotInFront:
                    pass
            # whichever way a window was asked to go, the app may have answered with a question of its own
            for _ in range(len(item.get("ids", [])) + 1):
                if not _discard_prompt(engine, item["pid"]):
                    break
        except Exception as exc:  # noqa: BLE001  (best effort; what stays is reported)
            log_harness(f"sweep {item['app']}: {exc}")
    return _leftovers(engine, before)


def _alive(engine: Engine, pid: int) -> bool:
    return any(a.get("pid") == pid for a in engine.helper.call("apps.running"))


def log_harness(msg: str) -> None:
    import sys
    print(f"   harness: {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------- tasks on apps never named
def _named_anywhere() -> str:
    """Everything this repository has ever said, lowercased: every tracked file and every commit message.
    An app that appears in it was seen while the engine was built, whether or not anyone meant to tune for
    it, and cannot say anything about apps the engine has never met."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    text = []
    try:
        files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False).stdout.split()
        for f in files:
            try:
                text.append((root / f).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        text.append(subprocess.run(["git", "log", "--format=%B"], cwd=root, capture_output=True, text=True, check=False).stdout)
    except OSError:
        pass
    return "\n".join(text).casefold()


def _toolkit(app_path: str) -> str:
    """What the app is built with, as its bundle says: the frameworks it ships, the keys in its Info.plist.
    No app is named; a kind is read off the bundle the way a person would read a label."""
    base = Path(app_path)
    info = _info_plist(app_path)
    frameworks = {p.name for p in (base / "Contents" / "Frameworks").glob("*")} if (base / "Contents" / "Frameworks").is_dir() else set()
    if any(f.startswith("Electron") for f in frameworks):
        return "electron"
    if any(f.startswith("Qt") for f in frameworks):
        return "qt"
    bundled_jdk = any((base / "Contents" / d).is_dir() and list((base / "Contents" / d).glob("*.jdk")) for d in ("PlugIns", "Java"))
    if "JVMOptions" in info or "JVMMainClassName" in info or bundled_jdk:
        return "java"
    if info.get("LSRequiresIPhoneOS"):
        return "ios"
    if info.get("UIDeviceFamily"):
        return "catalyst"
    return "appkit"


def _for_people(app_path: str) -> bool:
    """An app a person launches, as its bundle says: not one that declares it has no interface or runs in
    the background (`LSUIElement`, `LSBackgroundOnly`), and not one macOS keeps inside a framework or
    CoreServices directory, where it puts the pieces of itself that are not meant to be opened by anyone.
    The first draw took an onboarding panel and a pasteboard progress window for apps."""
    info = _info_plist(app_path)
    if info.get("LSUIElement") or info.get("LSBackgroundOnly"):
        return False
    parts = set(Path(app_path).parts)
    return not (parts & {"Frameworks", "PrivateFrameworks", "CoreServices", "Support", "Resources", "Helpers", "XPCServices"})


def _opens_text(app_path: str) -> bool:
    info = _info_plist(app_path)
    kinds = {str(x).casefold() for t in info.get("CFBundleDocumentTypes") or []
             for x in (t.get("LSItemContentTypes") or []) + (t.get("CFBundleTypeExtensions") or [])}
    return bool(kinds & {"public.plain-text", "public.text", "public.utf8-plain-text", "txt"})


def sampled_tasks(engine: Engine, suite: dict[str, Any], installed: list[dict[str, Any]] | None = None,
                  named: str | None = None) -> list[dict[str, Any]]:
    """Tasks the suite's templates make of apps this repository has never named, drawn from what is installed.

    A suite that names apps is a suite the engine was built beside, and a number on it says how well the
    engine does on apps its authors looked at. Generalisation is the other number: apps nobody here ever
    mentioned, chosen by the Mac, grouped by what they are built with. The engine sees a goal naming an
    app, as it would from any user; it never sees the list.
    """
    import random
    from .observe import installed_apps

    conf = suite.get("sample") or {}
    named = _named_anywhere() if named is None else named
    apps = installed if installed is not None else installed_apps(engine.cfg, engine.helper)
    fresh = [a for a in apps if a.get("bundle_id") and a.get("path") and a.get("name") and _for_people(a["path"])
             and str(a["bundle_id"]).casefold() not in named
             and not re.search(r"(?<!\w)" + re.escape(str(a["name"]).casefold()) + r"(?!\w)", named)]
    for a in fresh:
        a["toolkit"] = _toolkit(a["path"])
    rng = random.Random(int(conf.get("seed", 0)))
    rng.shuffle(fresh)
    # spread the draw over toolkits: a Mac with forty AppKit apps and two Electron ones should still show both
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for a in fresh:
        by_kind.setdefault(a["toolkit"], []).append(a)
    drawn: list[dict[str, Any]] = []
    want = int(conf.get("apps", 6))
    while len(drawn) < want and any(by_kind.values()):
        for kind in sorted(by_kind):
            if by_kind[kind] and len(drawn) < want:
                drawn.append(by_kind[kind].pop(0))
    out = []
    for tpl in suite.get("templates") or []:
        for a in drawn:
            if tpl.get("needs") == "text_documents" and not _opens_text(a["path"]):
                continue
            info = _info_plist(a["path"])
            version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")
            if "{app_version}" in yaml.safe_dump(tpl, allow_unicode=True) and not version:
                continue
            def fill(v: Any) -> Any:
                if isinstance(v, str):
                    return v.replace("{app}", str(a["name"])).replace("{app_version}", version)
                if isinstance(v, list):
                    return [fill(x) for x in v]
                if isinstance(v, dict):
                    return {k: fill(x) for k, x in v.items()}
                return v
            task = {k: fill(v) for k, v in tpl.items() if k != "needs"}
            task["id"] = f"{tpl['id']}--{a['bundle_id']}"
            task["app"] = a["bundle_id"]
            task["category"] = a["toolkit"]
            out.append(task)
    return out


def _locked(engine: Engine) -> bool:
    return bool(engine.helper.call("session.state").get("screen_locked"))


def _error_row(t: dict[str, Any], why: str) -> dict[str, Any]:
    return {"id": t["id"], "app": t.get("app") or "", "goal": t["goal"], "status": "error", "passed": False, "valid": False,
            "why": why, "reason": why, "steps": 0, "seconds": 0.0, "decider_calls": 0, "cost_usd": 0.0, "planned": False, "trace": []}


def run_suite(engine: Engine, suite_path: Path, only: list[str] | None = None, out_dir: Path | None = None,
              progress: Callable[[str], None] | None = None, repeat: int | None = None) -> dict[str, Any]:
    progress = progress or (lambda _m: None)
    raw = suite_path.read_bytes()
    suite = yaml.safe_load(raw.decode("utf-8"))
    if suite.get("fresh", True):       # no recall: skills off, app models start empty
        import tempfile
        conf = engine.cfg.docs["config"]
        conf["observe"]["providers"] = [p for p in conf["observe"].get("providers", []) if p != "skills"]
        conf.setdefault("skills", {})["record"] = False
        conf.setdefault("appmodel", {})["dir"] = tempfile.mkdtemp(prefix="macwork-sandbox-apps-")
        from .appmodel import AppModels
        engine.models = AppModels(engine.cfg)
        engine.cache["appmodels"] = engine.models
    if suite.get("templates"):
        suite = {**suite, "tasks": sampled_tasks(engine, suite)}
        progress(f"sampled {len(suite['tasks'])} tasks on apps this repository has never named: "
                 + ", ".join(sorted({t['id'].split('--', 1)[1] + ' (' + t['category'] + ')' for t in suite['tasks']})))
    tasks = [t for t in suite.get("tasks", []) if not only or t["id"] in only]
    runs = int(repeat or suite.get("repeat", 1))
    conf = engine.cfg.docs["config"].setdefault("engine", {})
    conf["tidy"] = "defer"          # check the result first, then let the engine tidy
    conf["mode"] = "exclusive"      # an eval run owns the Mac: it types, switches apps and closes windows
    progress("this run takes over the screen and the keyboard until it finishes")
    rows = []
    start = _desktop(engine)
    try:
        for n in range(runs):
            for t0_task in tasks:
                rows.append(_run_task(engine, suite, t0_task, n, runs, progress, start))
    finally:
        final = sweep(engine, start)       # whatever happens, the desktop goes back to how the run found it
        if final:
            progress("harness: LEFT OPEN after the run — close these by hand: "
                     + "; ".join(f"{x['app']} ({'launched by the run' if x.get('new_app') else str(x.get('windows')) + ' window(s)'})" for x in final))
    report = _report(suite_path, raw, rows, runs, out_dir)
    report["summary"]["left_after_run"] = [{k: v for k, v in x.items() if k in ("app", "new_app", "windows")} for x in final]
    return report


def _forget(engine: Engine) -> None:
    """Put the engine back to not having seen this Mac.

    `fresh: true` promises that every task is solved by reasoning on the live screen rather than by recall,
    and it emptied the app models *once*, at the start of the suite. Task 2 then ran on what task 1 had
    learned about the same app, and the safety floor's verdict cache spanned all 29 tasks and every
    repeat — so the later a task ran, the better it did, and the three repeats of a task were not three
    independent trials.

    Not everything cached is recall. Which apps are installed is a fact about the Mac and costs 1.8 s to
    re-read; the pseudonym table is privacy machinery. Those stay.
    """
    scopes = getattr(engine, "_scopes", None)
    if isinstance(scopes, dict):
        scopes.clear()          # each task's live working state: pictures, windows seen, screens read by sight
    for key in ("floor.verdicts", "floor.harmless", "vision.ocr", "vision.canvas",
                "menu.snap", "menubar.owners", "learn.safe", "services.all"):
        got = engine.cache.get(key)
        if isinstance(got, (dict, set)):
            got.clear()
        else:
            engine.cache.pop(key, None)
    if engine.cfg.docs["config"].get("appmodel", {}).get("dir", "").startswith("/"):
        import tempfile

        from .appmodel import AppModels
        engine.cfg.docs["config"]["appmodel"]["dir"] = tempfile.mkdtemp(prefix="macwork-sandbox-apps-")
        engine.models = AppModels(engine.cfg)
        engine.cache["appmodels"] = engine.models


def _run_task(engine: Engine, suite: dict[str, Any], task: dict[str, Any], n: int, runs: int,
              progress: Callable[[str], None], start: tuple[dict[int, set[int]], set[int]] | None = None) -> dict[str, Any]:
    if suite.get("fresh", True):
        _forget(engine)
    eval_dir = str(_sandbox(engine, task)) if suite.get("sandbox") else ""
    t = _sub(task, eval_dir)
    progress(f"[{t['id']}{f' #{n + 1}' if runs > 1 else ''}] {t['goal']}")
    base = {"id": t["id"], "run": n + 1, "category": t.get("category", ""), "app": t.get("app") or "", "goal": t["goal"]}
    phase: dict[str, float] = {}                  # where the harness itself spends time (not counted in "seconds")
    mark = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        phase[name] = round(phase.get(name, 0.0) + now - mark[0], 1)
        mark[0] = now
    try:
        harness_pid = None
        if t.get("app") and suite.get("launch_first", True):
            harness_pid = _launch(engine, t["app"])   # apps restore their last state on launch: let setup and the pre-check see it
            if not engine._resolve_app(t["app"], engine.helper.call("apps.running")) and not engine._installed_named(t["app"]):
                # the task names an app this Mac does not have: whatever happens next is about another app
                # (a real run sent two Numbers tasks to whichever app opens a .csv here) and says nothing
                # about the engine. Not counted, like a task that was already satisfied.
                progress(f"   INVALID {t['app']} is not on this Mac (not counted)")
                return base | {"status": "invalid", "passed": False, "valid": False, "why": f"{t['app']} is not installed on this Mac",
                               "reason": "", "steps": 0, "seconds": 0.0, "decider_calls": 0, "cost_usd": 0.0, "planned": False, "trace": []}
        if _locked(engine):   # nothing on screen can be judged or operated: says nothing about ability
            progress("   ERROR the screen is locked (not counted)")
            return base | _error_row(t, "the screen is locked")
        lap("launch")
        cleanup(engine, t, "setup")
        before = _desktop(engine)          # after setup: what the task itself must leave as it found it
        # a prompt above every window that was not there when the suite began — an earlier task's doing, and
        # this run is not a clean one; what the person had up before the suite (a launcher, an input method's
        # panel) is theirs and is not reported
        above = _dialogs_above(engine, start[0]) if start else []
        if above:
            log_harness("on screen before this task, above every window: " + "; ".join(f"「{d['title'] or d['app']}」" for d in above))
        text_before = _text_of(engine, t.get("app"))[1] if "app_changed" in (t.get("check") or {}) else ""
        lap("setup")
        expected = _expected(t)
        if expected == ["done"] and any(k in (t.get("check") or {}) for k in SCREEN_CHECKS):
            pre_ok, _ = check(engine, t, {"status": "done"}, only=SCREEN_CHECKS)
            if pre_ok:   # already true before doing anything: this run cannot tell us anything
                progress("   INVALID already satisfied before the task started (clean up and rerun)")
                return base | {"status": "invalid", "passed": False, "valid": False, "why": "already satisfied before the task started",
                               "reason": "", "steps": 0, "seconds": 0.0, "decider_calls": 0, "cost_usd": 0.0, "planned": False, "trace": []}
        lap("pre_check")
        answering = getattr(engine.decider, "name", "?")   # who is deciding: a run decided by two is two runs
        t0 = time.monotonic()
        try:
            res = engine.do(t["goal"], t.get("inputs") or {}, t.get("app"), progress=lambda m: progress(f"   → {m}"))
            # `need_continue` means "getting somewhere, out of budget for this turn". The CLI resumes it
            # and so does every MCP client, so a harness that scores it a failure is measuring the budget
            # rather than the engine. Bounded, because a task that only ever asks to continue is stuck.
            continues = 0
            limit = int(suite.get("max_continues", 3))
            while res.get("status") == "need_continue" and "need_continue" not in expected and continues < limit:
                continues += 1
                progress(f"   → continuing ({continues}/{limit}): {res.get('reason', '')[:60]}")
                res = engine.resume(res["task_id"], progress=lambda m: progress(f"   → {m}"))
            if continues:
                base["continued"] = continues
        except Exception as exc:  # noqa: BLE001  (one broken task must not stop the suite)
            res = {"status": "error", "reason": str(exc)[:200], "steps": [], "decider": {"calls": 0, "cost_usd": 0.0}}
        seconds = round(time.monotonic() - t0, 1)
        now_answering = getattr(engine.decider, "name", "?")
        lap("task")
        time.sleep(float(suite.get("settle_s", 0.8)))
        if now_answering != answering:
            # The decider has no fallback that is as good, on purpose: when the one in front cannot answer,
            # the next one takes over and stays. That is the right thing to do in a task and the wrong thing
            # to count — half a run decided by a calibrated model and half by an uncalibrated one says nothing
            # about either. A real suite lost its network mid-run and the rest of it scored the fallback.
            progress(f"   ERROR the decider changed mid-run ({answering} → {now_answering}, not counted)")
            return base | _error_row(t, f"the decider changed mid-run: {answering} → {now_answering}")
        if _locked(engine) or res.get("cause") == "screen_locked":
            progress("   ERROR the screen was locked during the task (not counted)")
            cleanup(engine, t)
            return base | _error_row(t, "the screen was locked during the task")
        if res.get("cause") == "decider_unreachable":   # the service was unreachable: says nothing about ability
            cleanup(engine, t)
            progress(f"   ERROR {res['reason'][:120]}")
            return base | _error_row(t, res["reason"][:120])
        if "app_windows_grew" in (t.get("check") or {}) or "app_changed" in (t.get("check") or {}):
            grew = {x["pid"]: x["windows"] for x in _leftovers(engine, before)}
            res["_windows_grew"] = grew
            if "app_changed" in (t.get("check") or {}):
                pid = (engine._resolve_app(t.get("app"), engine.helper.call("apps.running")) or {}).get("pid")
                res["_app_changed"] = bool(grew.get(pid)) or _text_of(engine, t.get("app"))[1] != text_before
        seen_ok, why = check(engine, t, res)
        lap("check")
        status_ok = res.get("status") in expected
        ok = seen_ok and status_ok   # the engine must know how it ended, not just happen to leave the right screen
        if seen_ok and not status_ok:
            why = f"ended '{res.get('status')}', expected {expected}" + (" (result on screen)" if expected == ["done"] else "")
        elif ok and res.get("status") != "done":
            why = f"correctly ended '{res.get('status')}'"
        if res.get("task_id"):
            engine.feedback(res["task_id"], ok, why)     # a wrong "done" must not become a routine
        cleanup(engine, t)
        lap("cleanup")
        tidy = engine.tidy(res["task_id"]) if res.get("task_id") else {}
        lap("tidy")
        left_by_engine = [{k: v for k, v in x.items() if k in ("app", "new_app", "windows")} for x in _leftovers(engine, before)]
        stuck = sweep(engine, before)      # the next task starts from the same desktop, and so does the user
        if harness_pid and suite.get("quit_launched", True):   # what the harness itself opened, it closes
            engine.helper.call("apps.quit", pid=harness_pid, timeout_ms=3000, timeout=15)
        lap("sweep")
        row = base | {"status": res.get("status"), "passed": ok, "valid": True, "why": why, "reason": res.get("reason", ""),
                      "cause": res.get("cause"),      # why it ended, for a program (budget, screen_locked, …)
                      "invariants_broken": (res.get("outputs") or {}).get("invariants_broken") or [],   # the engine's own bugs, as facts
                      # what a task that stopped to ask was asking about: the held action and the floor's reasons
                      "asked": {"action": str(((res.get("pending") or {}).get("confirm") or (res.get("pending") or {}).get("affordance") or {}).get("label") or "")[:120],
                                "because": (res.get("pending") or {}).get("because") or []} if res.get("pending") else None,
                      "steps": len(res.get("steps", [])), "seconds": seconds, "decider_calls": res.get("decider", {}).get("calls", 0),
                      "cost_usd": max(0.0, res.get("decider", {}).get("cost_usd", 0.0)), "planned": bool(res.get("plan")),
                      "decider": answering,
                      "trace": res.get("steps", []), "answer": (res.get("outputs") or {}).get("answer"), "tidy": tidy,
                      # `done` because the question was answered, though the task itself ran out or flailed:
                      # the engine reports it as done, and the check verifies the answer, so it is scored
                      # as done — but a suite where half the passes are this is a different suite from one
                      # where none are, and a total cannot show it
                      "answered_anyway": bool((res.get("outputs") or {}).get("unfinished_but_answered")),
                      "harness_s": phase, "left_by_engine": left_by_engine,
                      "on_screen_before": [d["title"] or d["app"] for d in above],   # a prompt from an earlier task, still up: not a clean run
                      "left_after_sweep": [{k: v for k, v in x.items() if k in ("app", "new_app", "windows")} for x in stuck]}
        progress(f"   {'PASS' if ok else 'FAIL'} {row['status']} {row['steps']} steps {seconds}s ({why})"
                 + (f" — left behind by the engine: {left_by_engine}" if left_by_engine else ""))
        return row
    finally:
        if eval_dir:
            shutil.rmtree(eval_dir, ignore_errors=True)


def _no_home(text: str) -> str:
    return text.replace(str(Path.home()), "~")


def _rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    k, n = sum(r["passed"] for r in rows), len(rows)
    lo, hi = wilson(k, n)
    return {"passed": k, "runs": n, "rate": round(k / n, 3) if n else 0.0, "ci95": [round(lo, 3), round(hi, 3)]}


def _report(suite_path: Path, raw: bytes, rows: list[dict[str, Any]], runs: int, out_dir: Path | None) -> dict[str, Any]:
    valid = [r for r in rows if r.get("valid", True)]
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in valid:
        by_task.setdefault(r["id"], []).append(r)
    cats: dict[str, list[dict[str, Any]]] = {}
    for r in valid:
        cats.setdefault(r.get("category") or "-", []).append(r)
    secs = sorted(r["seconds"] for r in valid)
    # The headline interval is over *tasks*, not over runs. Three runs of one task are the most correlated
    # observations in the set — same task, same app, same screen — and counting them as three independent
    # trials narrowed the reported interval by about the square root of the repeat count, for nothing. A
    # task counts once, as whichever way most of its runs went.
    per_task = [sum(rs_) * 2 > len(rs_) for rs_ in ([r["passed"] for r in rs] for rs in by_task.values())]
    lo, hi = wilson(sum(per_task), len(per_task))
    rate = _rate(valid)
    # `runs` is every row and `valid` the ones that count. `**_rate(valid)` used to land after both and
    # overwrite `runs` with the valid count, so the headline subtracted the invalid rows from a number
    # they were already out of: a ×3 run with six invalid rows read "17/12 runs". And a task whose every
    # run was invalid is not a task this run says anything about, so `tasks` is the ones it does.
    summary = {"suite": str(suite_path), "suite_sha256": hashlib.sha256(raw).hexdigest()[:16], "repeat": runs,
               "tasks": len(by_task), "tasks_listed": len({r["id"] for r in rows}), "runs": len(rows), "valid": len(valid),
               "invalid": sum(r["status"] == "invalid" for r in rows), "errors": sum(r["status"] == "error" for r in rows),
               "passed": rate["passed"], "rate": rate["rate"],
               "passed_tasks": sum(per_task), "ci95": [round(lo, 3), round(hi, 3)], "ci95_over": "tasks",
               "pass_all": sum(all(r["passed"] for r in rs) for rs in by_task.values()),   # pass^N: passed every time
               "success_rate": round(sum(r["passed"] for r in valid) / (len(valid) or 1), 3),
               "median_seconds": secs[len(secs) // 2] if secs else 0, "p90_seconds": secs[int(len(secs) * 0.9)] if secs else 0,
               "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 5), "decider_calls": sum(r["decider_calls"] for r in rows),
               "categories": {c: _rate(rs) for c, rs in sorted(cats.items())},
               "tasks_leaving_things_behind": sum(bool(r.get("left_by_engine")) for r in valid),
               "runs_with_a_prompt_already_up": sum(bool(r.get("on_screen_before")) for r in valid),
               "passed_by_answering_anyway": sum(bool(r.get("answered_anyway")) and r["passed"] for r in valid),
               "runs_with_broken_invariants": sum(bool(r.get("invariants_broken")) for r in valid),
               "at": time.strftime("%Y-%m-%d %H:%M")}
    report = {"summary": summary, "rows": rows}
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # A report is meant to be committed (evals/baseline/), and a trace names the files a task opened —
        # under this user's home directory, by their user name. The redactor turns that into `~` for anything
        # sent; a report was written raw.
        (out_dir / f"{stamp}.json").write_text(_no_home(json.dumps(report, ensure_ascii=False, indent=1)), encoding="utf-8")
        s = summary
        md = [f"# {suite_path.name} — {s['at']} (sha256 {s['suite_sha256']}, ×{runs})", "",
              f"**{s['passed_tasks']}/{s['tasks']} tasks passed (95% CI {s['ci95'][0]:.0%}–{s['ci95'][1]:.0%}, over tasks — "
              f"repeats of one task are not independent trials)**; {s['passed']}/{s['valid']} runs ({s['rate']:.0%}); "
              f"passed every time: {s['pass_all']}/{len(by_task)} tasks; median {s['median_seconds']} s, p90 {s['p90_seconds']} s; "
              f"{s['decider_calls']} decisions, ${s['total_cost_usd']}; {s['invalid']} invalid, {s['errors']} errors"
              + (f" ({s['tasks_listed'] - s['tasks']} of {s['tasks_listed']} tasks never counted)" if s['tasks_listed'] > s['tasks'] else "")
              + (f"; **{s['passed_by_answering_anyway']} passed by answering without finishing**" if s["passed_by_answering_anyway"] else "")
              + (f"; **{s['runs_with_broken_invariants']} run(s) with a broken engine invariant**" if s.get("runs_with_broken_invariants") else ""), "",
              "| category | passed | rate | 95% CI |", "|---|---|---|---|"]
        md += [f"| {c} | {v['passed']}/{v['runs']} | {v['rate']:.0%} | {v['ci95'][0]:.0%}–{v['ci95'][1]:.0%} |" for c, v in s["categories"].items()]
        md += ["", "| task | runs | results | median s | note (last failure) |", "|---|---|---|---|---|"]
        for tid, rs in by_task.items():
            fails = [r for r in rs if not r["passed"]]
            med = sorted(r["seconds"] for r in rs)[len(rs) // 2]
            md.append(f"| {tid} | {sum(r['passed'] for r in rs)}/{len(rs)} | {' '.join('✅' if r['passed'] else '❌' for r in rs)} | {med} | "
                      f"{(fails[-1]['why'] if fails else '')[:90].replace('|', '/')} |")
        (out_dir / f"{stamp}.md").write_text(_no_home("\n".join(md) + "\n"), encoding="utf-8")
        report["files"] = [str(out_dir / f"{stamp}.json"), str(out_dir / f"{stamp}.md")]
    return report


# ----------------------------------------------------------------------------- comparing two runs
def mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p for paired before/after outcomes on the same tasks.

    `b` tasks went fail -> pass and `c` went pass -> fail; tasks that did not change carry no information
    about whether anything changed, which is the whole point of the test and the reason a bare "12 -> 14"
    says nothing. Under "the change did nothing", each of the b + c tasks that moved was equally likely to
    move either way, so this is a sign test on them.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


def _outcomes(report: dict[str, Any]) -> dict[str, list[bool]]:
    """Per task, whether each valid run of it passed. Invalid runs and errors are not outcomes."""
    out: dict[str, list[bool]] = {}
    for r in report.get("rows") or []:
        if r.get("valid", True) and r.get("status") not in ("error", "invalid"):
            out.setdefault(r["id"], []).append(bool(r.get("passed")))
    return out


def _categories(report: dict[str, Any]) -> dict[str, str]:
    return {r["id"]: r.get("category") or "-" for r in report.get("rows") or []}


def _majority(runs: list[bool]) -> bool:
    return sum(runs) * 2 > len(runs)


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Did anything actually change between two runs of the same suite?

    A single run of 29 tasks that goes from 12 to 14 is the kind of number that reads like progress and is
    not evidence of any: two tasks flipping one way and none the other is a different thing from five
    flipping one way and three the other, and the totals cannot tell them apart. So this pairs the tasks by
    id, names what moved in each direction, and tests the ones that moved.

    It also refuses to compare quietly across things that make a comparison meaningless: a different suite,
    or a different decider answering.
    """
    b_sum, a_sum = before.get("summary") or {}, after.get("summary") or {}
    b_runs, a_runs = _outcomes(before), _outcomes(after)
    shared = sorted(set(b_runs) & set(a_runs))

    warnings: list[str] = []
    if b_sum.get("suite_sha256") and b_sum["suite_sha256"] != a_sum.get("suite_sha256"):
        warnings.append(f"different suites ({b_sum['suite_sha256']} vs {a_sum.get('suite_sha256')}): "
                        "the tasks themselves changed, so this compares two different questions")
    deciders = lambda rep: sorted({r.get("decider") for r in rep.get("rows") or [] if r.get("decider")})  # noqa: E731
    if deciders(before) != deciders(after):
        warnings.append(f"different deciders ({deciders(before)} vs {deciders(after)}): this measures the model, not the change")
    only_before = sorted(set(b_runs) - set(a_runs))
    only_after = sorted(set(a_runs) - set(b_runs))
    if only_before or only_after:
        warnings.append(f"{len(only_before) + len(only_after)} task(s) are in only one of the runs and are left out: "
                        f"{(only_before + only_after)[:6]}")

    fixed, broke, changed_rate = [], [], []
    for tid in shared:
        was, now = _majority(b_runs[tid]), _majority(a_runs[tid])
        if was != now:
            (broke if was else fixed).append(tid)
        if len(b_runs[tid]) > 1 or len(a_runs[tid]) > 1:
            b_rate, a_rate = sum(b_runs[tid]) / len(b_runs[tid]), sum(a_runs[tid]) / len(a_runs[tid])
            if b_rate != a_rate:
                changed_rate.append({"id": tid, "before": f"{sum(b_runs[tid])}/{len(b_runs[tid])}",
                                     "after": f"{sum(a_runs[tid])}/{len(a_runs[tid])}"})

    # Per category as well as pooled. Four must-not tasks breaking inside twenty-nine is exactly what a
    # pooled test cannot see: the totals can improve while the half that matters gets worse.
    cats = _categories(after) | _categories(before)
    by_category: dict[str, dict[str, Any]] = {}
    for tid in shared:
        c = by_category.setdefault(cats.get(tid, "-"), {"tasks": 0, "fixed": 0, "broke": 0})
        c["tasks"] += 1
        was, now = _majority(b_runs[tid]), _majority(a_runs[tid])
        if was != now:
            c["broke" if was else "fixed"] += 1
    for c in by_category.values():
        c["p_mcnemar"] = round(mcnemar(c["fixed"], c["broke"]), 4)
    hurt = sorted(name for name, c in by_category.items() if c["broke"] > c["fixed"])

    p = mcnemar(len(fixed), len(broke))
    b_pass = sum(_majority(b_runs[t]) for t in shared)
    a_pass = sum(_majority(a_runs[t]) for t in shared)
    n = len(shared)
    verdict = ("nothing moved: no task changed which way it went" if not fixed and not broke else
               f"{len(fixed)} fixed, {len(broke)} broken — "
               + (f"too few to tell from chance (exact McNemar p={p:.2f}; "
                  f"{len(fixed) + len(broke)} tasks moved, and {p:.0%} of the time chance alone does at least this)"
                  if p > 0.05 else f"more than chance would give (exact McNemar p={p:.3f})"))
    if hurt:
        verdict += ". Worse in " + ", ".join(f"{n} ({by_category[n]['broke']} broken)" for n in hurt)
    return {"tasks": n, "before": {"passed": b_pass, "of": n, "ci95": [round(x, 3) for x in wilson(b_pass, n)]},
            "after": {"passed": a_pass, "of": n, "ci95": [round(x, 3) for x in wilson(a_pass, n)]},
            "fixed": fixed, "broke": broke, "p_mcnemar": round(p, 4), "verdict": verdict,
            "by_category": by_category, "worse_in": hurt,
            "rate_moved": changed_rate, "warnings": warnings,
            "repeats": {"before": b_sum.get("repeat", 1), "after": a_sum.get("repeat", 1)}}


def baseline_for(suite: Path | str) -> Path:
    """The committed baseline for a suite: `evals/baseline/<suite>.json`, a report checked into the repository.

    Reports were written and never kept, so the only run a change could be held against was whatever was
    still on this machine — which is how 12/29 -> 14/29 came to live only in a commit message. A baseline
    that is committed is one a run on any machine can be paired with, and one that is replaced on purpose.
    """
    return Path(__file__).resolve().parents[1] / "evals" / "baseline" / (Path(suite).stem + ".json")


def compare_files(before_path: Path, after_path: Path) -> dict[str, Any]:
    return compare(json.loads(before_path.read_text(encoding="utf-8")),
                   json.loads(after_path.read_text(encoding="utf-8")))


def format_compare(c: dict[str, Any]) -> str:
    lines = [f"{c['before']['passed']}/{c['tasks']} → {c['after']['passed']}/{c['tasks']} tasks",
             f"  before 95% CI {c['before']['ci95'][0]:.0%}–{c['before']['ci95'][1]:.0%}"
             f"   after 95% CI {c['after']['ci95'][0]:.0%}–{c['after']['ci95'][1]:.0%}",
             f"  {c['verdict']}"]
    if c["fixed"]:
        lines.append(f"  fixed:  {', '.join(c['fixed'])}")
    if c["broke"]:
        lines.append(f"  broke:  {', '.join(c['broke'])}")
    for name in c.get("worse_in") or []:
        v = c["by_category"][name]
        lines.append(f"  worse:  {name} — {v['broke']} broken, {v['fixed']} fixed of {v['tasks']}")
    for m in c["rate_moved"]:
        lines.append(f"  rate:   {m['id']} {m['before']} → {m['after']}")
    lines += [f"  ! {w}" for w in c["warnings"]]
    return "\n".join(lines)
