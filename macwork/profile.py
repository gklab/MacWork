"""Where a task's time goes, read from the tasks this Mac has actually run.

Every change to the engine's speed or its caution was made without a way to see what it did, and the first
real eval run had to overturn three of them. This reads the task store — nothing is run, nothing is driven —
and reports the two factors wall-clock time is made of:

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


def format_profile(p: dict[str, Any]) -> str:
    if not p["tasks"]:
        return "no stored tasks to read (engine.persist is off, or nothing has run yet)"
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
