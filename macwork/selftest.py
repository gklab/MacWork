"""Run, for real, the paths the unit tests only fake: every configured planner answering a plan, learning an app
ahead of time (and leaving it as found), and a learned routine being replayed (and saving time).
Everything is harmless: exploring only shows and navigates, the routine is a calculation, and routines and app
models go to temporary folders so the user's own are untouched.
"""

from __future__ import annotations

import tempfile
import time
from typing import Any, Callable

from .config import Config
from .planner import probe_planners


def _windows(eng: Any, app: str) -> list[str] | None:
    a = eng._resolve_app(app, eng.helper.call("apps.running"))
    if not a:
        return None
    nodes = eng.helper.call("ax.snapshot", pid=a["pid"], scope="windows", max_depth=0, max_nodes=30, actions=False).get("nodes", [])
    return sorted(str(n.get("title") or "") for n in nodes)


def _open(eng: Any, app: str) -> None:
    import subprocess
    inst = eng._installed_named(app)
    if inst:
        subprocess.run(["open", "-a", inst["path"]], capture_output=True, timeout=15, check=False)
        time.sleep(1.5)


def _clear(eng: Any, app: str) -> None:
    """Escape twice in the app (clears a calculator's display), only once it is verifiably in front."""
    from .act import NotInFront, _bring_forward
    from .observe import Ctx
    a = eng._resolve_app(app, eng.helper.call("apps.running"))
    if not a:
        return
    try:
        _bring_forward(Ctx(eng.cfg, eng.helper, running=eng.helper.call("apps.running")), a["pid"])
    except NotInFront:
        return
    for _ in range(2):
        eng.helper.call("input.key", combo="escape")
        time.sleep(0.2)


def run(cfg: Config, progress: Callable[[str], None], learn: bool = True, replay: bool = True) -> dict[str, Any]:
    from .engine import Engine

    st = cfg.section("selftest")
    out: dict[str, Any] = {}
    tmp = tempfile.mkdtemp(prefix="macwork-selftest-")
    conf = cfg.docs["config"]
    conf.setdefault("appmodel", {})["dir"] = f"{tmp}/apps"
    conf.setdefault("skills", {})["dir"] = f"{tmp}/skills"
    conf.setdefault("engine", {})["tidy_background"] = False
    eng = Engine(cfg)

    progress("planners")
    out["planners"] = probe_planners(cfg, eng.helper)

    # a stock app named the way the Mac reports it; "计算器" only resolved on a Chinese-language Mac
    app = str(st.get("app", "Calculator"))
    if learn:
        progress(f"learn {app}")
        conf.setdefault("learn", {}).update({"max_actions": int(st.get("learn_actions", 6)), "budget_s": int(st.get("learn_budget_s", 90))})
        _open(eng, app)                          # running and in front, as a user would have it (not via a task: it would tidy)
        before = _windows(eng, app)
        res = eng.learn(app, progress=lambda m: progress(f"   {m}"))
        after = _windows(eng, app)
        out["learn"] = {**res, "left_as_found": before == after, "windows_before": before, "windows_after": after,
                        "ok": bool(res.get("tried")) and before == after}

    if replay:
        goal = str(st.get("routine_goal", f"compute 7 x 6 in {app}"))
        progress(f"routine: {goal} (twice)")
        conf["observe"]["providers"] = list(dict.fromkeys(conf["observe"]["providers"] + ["skills"]))
        conf["skills"]["record"] = True
        runs = []
        for i in range(2):
            _open(eng, app)
            _clear(eng, app)                     # the previous result must not make the goal look done already
            t0 = time.monotonic()
            r = eng.do(goal, {}, app, progress=lambda m, i=i: progress(f"   #{i + 1} {m}"))
            runs.append({"status": r["status"], "steps": r["steps"], "seconds": round(time.monotonic() - t0, 1),
                         "decisions": r["decider"]["calls"], "routines": (r.get("outputs") or {}).get("routines"),
                         "learned": (r.get("outputs") or {}).get("learned_routine")})
        replayed = bool(runs[1]["routines"]) and all(x.get("ok") for x in runs[1]["routines"])
        out["replay"] = {"runs": runs, "recorded_first": bool(runs[0]["learned"]), "replayed_second": replayed,
                         "faster": runs[1]["seconds"] < runs[0]["seconds"],
                         "ok": runs[0]["status"] == runs[1]["status"] == "done" and bool(runs[0]["learned"]) and replayed}
    out["ok"] = all(v.get("ok", True) for v in out.values() if isinstance(v, dict)) and \
        any(p["status"] == "ok" for p in out["planners"])
    return out
