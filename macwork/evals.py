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
    trace = "\n".join(result.get("steps", []) + [str(t.get("text", "")) for t in outputs.get("typed_by_planner", [])])
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
    """``{eval_dir}`` in any string of a task."""
    if isinstance(value, str):
        return value.replace("{eval_dir}", eval_dir)
    if isinstance(value, list):
        return [_sub(v, eval_dir) for v in value]
    if isinstance(value, dict):
        return {_sub(k, eval_dir): _sub(v, eval_dir) for k, v in value.items()}
    return value


def _sandbox(engine: Engine, task: dict[str, Any]) -> Path:
    root = Path(str(engine.cfg.get("evals.sandbox", "~/Library/Caches/macwork-eval"))).expanduser()
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
    key = app_hint.casefold()
    inst = next((a for a in installed_apps(engine.cfg, engine.helper)
                 if key in (a.get("name", "").casefold(), a.get("file", "").casefold())), None)
    if inst:
        import subprocess
        subprocess.run(["open", "-g", "-a", inst["path"]], capture_output=True, timeout=15, check=False)   # -g: stay in the background
        time.sleep(2.0)
        opened = engine._resolve_app(app_hint, engine.helper.call("apps.running"))
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


def _desktop(engine: Engine) -> tuple[dict[int, set[int]], set[int]]:
    """Windows on screen (per app) and running apps: what a task must leave as it found it."""
    return engine._window_ids(), {a["pid"] for a in engine.helper.call("apps.running")}


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
    return out


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
                engine._quit(task, {"pid": item["pid"], "name": item["app"]})
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
                    continue
                try:
                    _bring_forward(Ctx(engine.cfg, engine.helper, running=engine.helper.call("apps.running")), item["pid"])
                    engine.helper.call("input.key", combo="escape")
                    time.sleep(0.4)
                except NotInFront:
                    pass
        except Exception as exc:  # noqa: BLE001  (best effort; what stays is reported)
            log_harness(f"sweep {item['app']}: {exc}")
    return _leftovers(engine, before)


def log_harness(msg: str) -> None:
    import sys
    print(f"   harness: {msg}", file=sys.stderr)


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
        conf.setdefault("appmodel", {})["dir"] = tempfile.mkdtemp(prefix="macwork-eval-apps-")
        from .appmodel import AppModels
        engine.models = AppModels(engine.cfg)
        engine.cache["appmodels"] = engine.models
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
                rows.append(_run_task(engine, suite, t0_task, n, runs, progress))
    finally:
        final = sweep(engine, start)       # whatever happens, the desktop goes back to how the run found it
        if final:
            progress(f"harness: still left after the run: {final}")
    report = _report(suite_path, raw, rows, runs, out_dir)
    report["summary"]["left_after_run"] = [{k: v for k, v in x.items() if k in ("app", "new_app", "windows")} for x in final]
    return report


def _run_task(engine: Engine, suite: dict[str, Any], task: dict[str, Any], n: int, runs: int,
              progress: Callable[[str], None]) -> dict[str, Any]:
    eval_dir = str(_sandbox(engine, task)) if suite.get("sandbox") else ""
    t = _sub(task, eval_dir) if eval_dir else task
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
        if _locked(engine):   # nothing on screen can be judged or operated: says nothing about ability
            progress("   ERROR the screen is locked (not counted)")
            return base | _error_row(t, "the screen is locked")
        lap("launch")
        cleanup(engine, t, "setup")
        before = _desktop(engine)          # after setup: what the task itself must leave as it found it
        lap("setup")
        expected = _expected(t)
        if expected == ["done"] and any(k in (t.get("check") or {}) for k in SCREEN_CHECKS):
            pre_ok, _ = check(engine, t, {"status": "done"}, only=SCREEN_CHECKS)
            if pre_ok:   # already true before doing anything: this run cannot tell us anything
                progress("   INVALID already satisfied before the task started (clean up and rerun)")
                return base | {"status": "invalid", "passed": False, "valid": False, "why": "already satisfied before the task started",
                               "reason": "", "steps": 0, "seconds": 0.0, "decider_calls": 0, "cost_usd": 0.0, "planned": False, "trace": []}
        lap("pre_check")
        t0 = time.monotonic()
        try:
            res = engine.do(t["goal"], t.get("inputs") or {}, t.get("app"), progress=lambda m: progress(f"   → {m}"))
        except Exception as exc:  # noqa: BLE001  (one broken task must not stop the suite)
            res = {"status": "error", "reason": str(exc)[:200], "steps": [], "decider": {"calls": 0, "cost_usd": 0.0}}
        seconds = round(time.monotonic() - t0, 1)
        lap("task")
        time.sleep(float(suite.get("settle_s", 0.8)))
        if _locked(engine) or "screen is locked" in str(res.get("reason", "")):
            progress("   ERROR the screen was locked during the task (not counted)")
            cleanup(engine, t)
            return base | _error_row(t, "the screen was locked during the task")
        if str(res.get("reason", "")).startswith("decider:"):   # the service was unreachable: says nothing about ability
            cleanup(engine, t)
            progress(f"   ERROR {res['reason'][:120]}")
            return base | _error_row(t, res["reason"][:120])
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
                      "steps": len(res.get("steps", [])), "seconds": seconds, "decider_calls": res.get("decider", {}).get("calls", 0),
                      "cost_usd": res.get("decider", {}).get("cost_usd", 0.0), "planned": bool(res.get("plan")),
                      "trace": res.get("steps", []), "answer": (res.get("outputs") or {}).get("answer"), "tidy": tidy,
                      "harness_s": phase, "left_by_engine": left_by_engine,
                      "left_after_sweep": [{k: v for k, v in x.items() if k in ("app", "new_app", "windows")} for x in stuck]}
        progress(f"   {'PASS' if ok else 'FAIL'} {row['status']} {row['steps']} steps {seconds}s ({why})"
                 + (f" — left behind by the engine: {left_by_engine}" if left_by_engine else ""))
        return row
    finally:
        if eval_dir:
            shutil.rmtree(eval_dir, ignore_errors=True)


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
    summary = {"suite": str(suite_path), "suite_sha256": hashlib.sha256(raw).hexdigest()[:16], "repeat": runs,
               "tasks": len({r["id"] for r in rows}), "runs": len(rows), "valid": len(valid),
               "invalid": sum(r["status"] == "invalid" for r in rows), "errors": sum(r["status"] == "error" for r in rows),
               **_rate(valid),
               "pass_all": sum(all(r["passed"] for r in rs) for rs in by_task.values()),   # pass^N: passed every time
               "success_rate": round(sum(r["passed"] for r in valid) / (len(valid) or 1), 3),
               "median_seconds": secs[len(secs) // 2] if secs else 0, "p90_seconds": secs[int(len(secs) * 0.9)] if secs else 0,
               "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 5), "decider_calls": sum(r["decider_calls"] for r in rows),
               "categories": {c: _rate(rs) for c, rs in sorted(cats.items())},
               "tasks_leaving_things_behind": sum(bool(r.get("left_by_engine")) for r in valid),
               "at": time.strftime("%Y-%m-%d %H:%M")}
    report = {"summary": summary, "rows": rows}
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out_dir / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        s = summary
        md = [f"# {suite_path.name} — {s['at']} (sha256 {s['suite_sha256']}, ×{runs})", "",
              f"**{s['passed']}/{s['runs'] - s['invalid'] - s['errors']} runs passed ({s['rate']:.0%}, 95% CI {s['ci95'][0]:.0%}–{s['ci95'][1]:.0%})**; "
              f"passed every time: {s['pass_all']}/{len(by_task)} tasks; median {s['median_seconds']} s, p90 {s['p90_seconds']} s; "
              f"{s['decider_calls']} decisions, ${s['total_cost_usd']}; {s['invalid']} invalid, {s['errors']} errors", "",
              "| category | passed | rate | 95% CI |", "|---|---|---|---|"]
        md += [f"| {c} | {v['passed']}/{v['runs']} | {v['rate']:.0%} | {v['ci95'][0]:.0%}–{v['ci95'][1]:.0%} |" for c, v in s["categories"].items()]
        md += ["", "| task | runs | results | median s | note (last failure) |", "|---|---|---|---|---|"]
        for tid, rs in by_task.items():
            fails = [r for r in rs if not r["passed"]]
            med = sorted(r["seconds"] for r in rs)[len(rs) // 2]
            md.append(f"| {tid} | {sum(r['passed'] for r in rs)}/{len(rs)} | {' '.join('✅' if r['passed'] else '❌' for r in rs)} | {med} | "
                      f"{(fails[-1]['why'] if fails else '')[:90].replace('|', '/')} |")
        (out_dir / f"{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        report["files"] = [str(out_dir / f"{stamp}.json"), str(out_dir / f"{stamp}.md")]
    return report
