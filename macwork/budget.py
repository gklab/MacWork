"""One ledger for everything a task is allowed.

Every allowance used to live where it was spent: `task.pace.looks >= int(self.cfg.get("engine.max_looks", 6))`
in one place, `task.pace.looks += 1` in two others, each with its own default and its own reading of "at the
limit". Seven counters, fourteen sites, three modules — and no single answer to "what has this task used, and
of what?", which is the first thing anyone asks of a task that stopped.

The table below is that answer. A counter is spent through `spend_allowance`, asked about through `allowance_left`, and every
count and ceiling is reported together by `ledger`. The counts themselves stay on `Pace` (persisted with the
task, reset per run in `Task.begin_run`); the step, time, decision and cost ceilings stay in `_overspent`,
which reads the same table for their names. Nothing here decides what a limit should be.
"""

from __future__ import annotations

from typing import Any

from .model import Task

# name -> (config key, default, first-of-a-kind that does not count against it)
ALLOWANCES: dict[str, tuple[str, int, int]] = {
    "looks": ("engine.max_looks", 6, 0),                  # looking into a folded group, or reading the screen
    "waits": ("engine.max_waits", 4, 0),                  # "the app is still busy": wait and look again
    "answer_tries": ("engine.max_answer_tries", 2, 0),    # done, but the question is not answered yet: look again
    "fruitless": ("engine.max_fruitless_rethinks", 2, 0), # rethinks that brought no new route (consult.FRUITLESS)
    "replans": ("planner.max_replans", 2, 1),             # plans after the first: the first is not a re-plan
    "corrections": ("planner.max_corrections", 3, 0),     # replans that followed a step judged to have gone wrong
    "done_opinions": ("planner.max_done_opinions", 2, 0), # second opinions on "done"
    "redo": ("engine.verify.max_redo", 1, 0),             # decisions discarded because the screen moved meanwhile
    "ready_waits": ("engine.max_ready_waits", 6, 0),      # looks that first waited for a starting or busy app to answer
}

# the hard ceilings, for the ledger's report; `_overspent` enforces them
CEILINGS: dict[str, tuple[str, float]] = {
    "steps": ("engine.max_steps", 12), "seconds": ("engine.budget_s", 90),
    "total_steps": ("engine.total_steps", 48), "total_seconds": ("engine.total_budget_s", 600),
    "decisions": ("engine.max_decisions", 120), "cost_usd": ("engine.max_cost_usd", 0.5),
}


class BudgetMixin:
    def allowance(self, name: str) -> int:
        key, default, free = ALLOWANCES[name]
        return int(self.cfg.get(key, default)) + free

    def allowance_left(self, task: Task, name: str) -> bool:
        """Is there any of this allowance left?"""
        return int(getattr(task.pace, name)) < self.allowance(name)

    def spend_allowance(self, task: Task, name: str) -> bool:
        """Use one of this allowance, if any is left. False — and nothing spent — when there is none."""
        if not self.allowance_left(task, name):
            return False
        setattr(task.pace, name, int(getattr(task.pace, name)) + 1)
        return True

    def ledger(self, task: Task) -> dict[str, Any]:
        """Everything this run has used, against everything it is allowed: the one table for "why did it stop"."""
        e = self.cfg.section("engine")
        out: dict[str, Any] = {name: {"used": int(getattr(task.pace, name)), "of": self.allowance(name)} for name in ALLOWANCES}
        decider = getattr(self, "_decider", None)
        out["steps"] = {"used": len(task.steps) - task.run_step0, "of": int(e.get("max_steps", CEILINGS["steps"][1]))}
        out["total_steps"] = {"used": len(task.steps), "of": int(e.get("total_steps", CEILINGS["total_steps"][1]))}
        out["seconds"] = {"used": round(task.working_s - task.spent_s, 1), "of": float(e.get("budget_s", CEILINGS["seconds"][1]))}
        out["total_seconds"] = {"used": round(task.working_s, 1), "of": float(e.get("total_budget_s", CEILINGS["total_seconds"][1]))}
        # The planner's time, this run's like `seconds`, in a line of its own: on the two key tasks of 09-23 the loop
        # waited on it for 28.8 of 44.3 s and 27.3 of about 35 s, and inside `seconds` none of that showed.
        # `waited` is part of `seconds`; `beside` ran while the loop looked and acted.
        use = task.planner_use or {}
        out["planner_seconds"] = {"waited": round((int(use.get("waited_ms", 0)) - task.run_waited_ms0) / 1000, 1),
                                  "beside": round((int(use.get("beside_ms", 0)) - task.run_beside_ms0) / 1000, 1)}
        if decider is not None:
            out["decisions"] = {"used": decider.calls - task.run_calls0, "of": int(e.get("max_decisions", CEILINGS["decisions"][1]))}
            out["cost_usd"] = {"used": round(decider.cost_usd - task.run_cost0, 5), "of": float(e.get("max_cost_usd", CEILINGS["cost_usd"][1]))}
        return out
