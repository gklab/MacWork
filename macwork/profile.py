"""Where a task's time goes, read from the tasks this Mac has actually run.

Every change to the engine's speed or its caution was made without a way to see what it did, and the first
real eval run had to overturn three of them. This reads what the engine recorded — nothing is run, nothing is
driven. The audit's `step` records (one per step, numbers only: loop._step_record) are the default: they say
how long each step took from end to end, which stage it was spent in, the planner's share and the slowest
providers. The task store is the older view (`--store`), and a report's rows keep each task's totals for when
the audit has rotated. The store's view reports the two factors wall-clock time is made of:

  cost per step   what each stage of a step costs (observe, decide, act, wait), so an optimisation is
                  aimed at the stage that is actually large. Measured here first: deciding is two-thirds
                  of a step and almost all of that is one network round trip, so shaving observe or act
                  polishes the remaining third.
  steps per task  how many of the steps were wasted — the engine records a step that only led back to a
                  screen it had already seen — and how many sat on a screen that did not change shape,
                  which is the ceiling on what deciding in sequences instead of single actions can save.

Numbers before and after, from the same command, or the change is not known to have done anything.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any


def _tasks(db_path: Path, limit: int) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in con.execute("pragma table_info(tasks)")]
        if "state" not in cols:
            return []
        rows = con.execute(f"select state from tasks order by rowid desc limit {int(limit)}").fetchall()
    finally:
        con.close()
    out = []
    for (text,) in rows:
        try:
            out.append(json.loads(text))
        except (TypeError, ValueError):
            continue
    return out


def _runs(flags: list[bool]) -> list[int]:
    """Lengths of consecutive True stretches."""
    out, cur = [], 0
    for f in flags:
        if f:
            cur += 1
        elif cur:
            out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def profile(db_path: Path, limit: int = 50) -> dict[str, Any]:
    tasks = _tasks(db_path, limit)
    stages: dict[str, list[float]] = {}
    steps = same = circles = failed = 0
    batchable = 0                      # steps inside a stretch of >= 3 on one unchanged screen
    per_task: list[int] = []
    for state in tasks:
        ss = state.get("steps") or []
        per_task.append(len(ss))
        steps += len(ss)
        failed += sum(1 for s in ss if not s.get("ok", True))
        circles += len((state.get("memory") or {}).get("circles") or [])
        sigs = [(s.get("before") or "").split(":")[0] for s in ss]
        cont = [bool(a) and a == b for a, b in zip(sigs, sigs[1:])]
        same += sum(cont)
        batchable += sum(n for n in _runs(cont) if n >= 2)      # n continuations = n+1 steps on one screen
        for s in ss:
            for k, v in ((s.get("decision") or {}).get("timing") or {}).items():
                if isinstance(v, (int, float)):
                    stages.setdefault(k, []).append(float(v))

    def summary(v: list[float]) -> dict[str, float]:
        v = sorted(v)
        return {"median_ms": round(statistics.median(v)), "p90_ms": round(v[int(len(v) * 0.9)]), "n": len(v)}

    table = {k: summary(v) for k, v in stages.items() if v}
    core = [k for k in ("observe", "decide", "act", "wait") if k in table]
    whole = sum(table[k]["median_ms"] for k in core) or 1
    return {
        "tasks": len(tasks), "steps": steps,
        "steps_per_task_median": statistics.median(per_task) if per_task else 0,
        "stages": table,
        "share_of_a_step": {k: round(table[k]["median_ms"] / whole, 2) for k in core},
        "wasted": {"circling_steps": circles, "failed_steps": failed,
                   "share": round((circles + failed) / steps, 2) if steps else 0.0},
        "same_screen": {"continuations": same, "share": round(same / steps, 2) if steps else 0.0,
                        "in_stretches_of_3_or_more": batchable,
                        "note": "the ceiling on what deciding in sequences can save: only these steps could "
                                "share a round trip with the one before"},
    }


STAGES = ("observe", "decide", "act", "wait", "planner")


def audit_files(path: Path | None, keep: int = 3) -> list[Path]:
    """The audit and its rotations (audit.<n>.jsonl, oldest first), those that exist."""
    if path is None:
        return []
    return [f for f in [path.with_suffix(f".{n}.jsonl") for n in range(keep, 0, -1)] + [path] if f.exists()]


def _step_records(paths: list[Path], task_ids: set[str] | None) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"kind": "step"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "step" and (task_ids is None or rec.get("task") in task_ids):
                out.append(rec)
    return out


def _spread(values: list[float]) -> dict[str, float]:
    v = sorted(values)
    return {"median_ms": round(statistics.median(v)), "p90_ms": round(v[min(len(v) - 1, int(len(v) * 0.9))]), "n": len(v)}


def _shares(totals: dict[str, float], wall: float) -> dict[str, float]:
    """Each stage's share of task time, from sums — the planner is bursty, and a median step hides it — and what
    no stage accounts for (looks that ended no step, settling, the loop itself)."""
    if wall <= 0:
        return {}
    out = {k: round(totals.get(k, 0.0) / wall, 3) for k in STAGES}
    out["unattributed"] = round(max(0.0, wall - sum(totals.get(k, 0.0) for k in STAGES)) / wall, 3)
    return out


def profile_audit(paths: list[Path], task_ids: set[str] | None = None) -> dict[str, Any]:
    """From the audit's step records (optionally only these tasks'): steps and looks per step, each stage's
    median and p90 per step, each stage's share of all task time, and the slowest providers."""
    records = _step_records(paths, task_ids)
    steps = [r for r in records if "end" not in r]
    wall = float(sum(int(r.get("wall_ms") or 0) for r in records))
    totals = {k: float(sum(int(r.get(f"{k}_ms") or 0) for r in records)) for k in STAGES}
    providers: dict[str, list[int]] = {}
    for r in steps:
        for name, ms in (r.get("providers") or {}).items():
            providers.setdefault(name, []).append(int(ms))
    slowest = sorted(({"provider": k, "total_ms": sum(v), "median_ms": round(statistics.median(v)), "looks": len(v)}
                      for k, v in providers.items()), key=lambda x: -x["total_ms"])[:8]
    return {"source": "audit", "tasks": len({r.get("task") for r in records}), "steps": len(steps),
            "looks_per_step": round(sum(int(r.get("looks") or 0) for r in records) / len(steps), 2) if steps else 0.0,
            "task_seconds": round(wall / 1000, 1),
            "stages": {k: _spread([float(r.get(f"{k}_ms") or 0) for r in steps]) for k in STAGES if steps},
            "share_of_task_time": _shares(totals, wall), "slowest_providers": slowest,
            "planner_calls": sum(int(r.get("planner_calls") or 0) for r in records),
            "decisions": sum(int(r.get("decisions") or 0) for r in records)}


def profile_report(report: dict[str, Any]) -> dict[str, Any]:
    """From an eval report's rows alone (each keeps its task's totals, `timing`): for a run whose audit has
    rotated away. Per step only on average — the rows keep sums, not steps."""
    timed = [r.get("timing") or {} for r in report.get("rows") or [] if r.get("timing")]
    steps = sum(int(t.get("steps") or 0) for t in timed)
    wall = float(sum(int(t.get("wall_ms") or 0) for t in timed))
    totals = {k: float(sum(int(t.get(f"{k}_ms") or 0) for t in timed)) for k in STAGES}
    return {"source": "report", "tasks": len(timed), "steps": steps,
            "looks_per_step": round(sum(int(t.get("looks") or 0) for t in timed) / steps, 2) if steps else 0.0,
            "task_seconds": round(wall / 1000, 1),
            "stages": {k: {"mean_ms": round(totals[k] / steps), "n": steps} for k in STAGES if steps},
            "share_of_task_time": _shares(totals, wall), "slowest_providers": [],
            "planner_calls": sum(int(t.get("planner_calls") or 0) for t in timed),
            "decisions": sum(int(t.get("decisions") or 0) for t in timed)}


def format_timing(p: dict[str, Any]) -> str:
    if not p["steps"]:
        return (f"no step records to read in the {p['source']}: steps are recorded there since the engine wrote one "
                "per step (audit.enabled); an older run is read with --store while the store still holds it")
    lines = [f"{p['tasks']} tasks, {p['steps']} steps, {p['looks_per_step']} looks per step, {p['task_seconds']} s of task time "
             f"(from the {p['source']}); {p['planner_calls']} planner calls, {p['decisions']} decisions", "",
             f"{'stage':12} {'median':>9} {'p90':>9}   share of task time"]
    share = p["share_of_task_time"]
    for k in STAGES:
        v = p["stages"].get(k) or {}
        mid = f"{v['median_ms']:>6} ms {v['p90_ms']:>6} ms" if "median_ms" in v else f"{'mean ' + str(v.get('mean_ms', 0)):>9} ms {'':>9}"
        lines.append(f"{k:12} {mid}   {share.get(k, 0.0):.0%}")
    lines.append(f"{'unattributed':12} {'':>21}   {share.get('unattributed', 0.0):.0%}   (looks that ended no step, settling, the loop)")
    if p["slowest_providers"]:
        lines += ["", "slowest providers (per look, of those over 5 ms): "
                  + ", ".join(f"{x['provider']} {x['median_ms']} ms × {x['looks']}" for x in p["slowest_providers"][:5])]
    return "\n".join(lines)


def format_profile(p: dict[str, Any]) -> str:
    if not p["tasks"]:
        return ("no stored tasks to read: the store keeps a task engine.tasks_ttl_s after it ends (1800 s by default), "
                "and nothing at all when engine.persist is off. The audit keeps every step: `macwork profile` reads it")
    lines = [f"{p['tasks']} tasks, {p['steps']} steps (median {p['steps_per_task_median']} per task)", "",
             f"{'stage':14} {'median':>8} {'p90':>8}   share of a step"]
    for k, v in sorted(p["stages"].items(), key=lambda kv: -kv[1]["median_ms"]):
        share = p["share_of_a_step"].get(k)
        lines.append(f"{k:14} {v['median_ms']:>6} ms {v['p90_ms']:>6} ms   "
                     + (f"{share:.0%}" if share is not None else "(inside another stage)"))
    w, s = p["wasted"], p["same_screen"]
    lines += ["", f"wasted steps   {w['circling_steps']} circling + {w['failed_steps']} failed = {w['share']:.0%} of all steps",
              f"same screen    {s['continuations']} steps followed one on an unchanged screen ({s['share']:.0%}); "
              f"{s['in_stretches_of_3_or_more']} of them in stretches of three or more"]
    return "\n".join(lines)
