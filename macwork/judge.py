"""What the decider judges for the loop: whether a "done" really holds, why a run did not finish, which
alternatives to offer the caller, and the moves it may choose from. What the engine is *allowed* to do lives in
policy.py."""

from __future__ import annotations

import logging
from typing import Any

from .decider import DeciderError, choice
from .model import Affordance, Observation, Task
from .observe import Ctx

log = logging.getLogger(__name__)

MOVES = ("act", "done", "wait", "rethink", "ask_user", "blocked", "impossible")   # their wording lives in questions.yaml


class JudgeMixin:
    def _moves(self) -> dict[str, str]:
        moves = self.cfg.docs["questions"].get("moves") or {}
        return {k: str(v).strip() for k, v in moves.items() if k in MOVES}

    def _verified_done(self, task: Task, decision: dict[str, Any]) -> bool:
        """The stricter look at "done" (asked alongside every step): intermediate screens fool a quick yes."""
        if not self.cfg.get("engine.verify_done", True) or "verified" not in decision:
            return True
        v = float(decision["verified"])
        th = self.cfg.section("engine.thresholds")
        if not task.steps:   # "already done" without doing anything needs strong evidence, not just no clear "no"
            return v >= float(th.get("done", 0.8))
        return v >= float(th.get("done_veto", 0.35))   # after acting, only a clear "no" overrules

    def _diagnose(self, task: Task, ctx: Ctx, obs: Observation, state: dict[str, Any], reason: str) -> dict[str, Any]:
        """Out of budget: the decider reads the last screen and says why the goal was not reached; "blocked" gets a
        second opinion (and its wording) from the planner when there is one."""
        tried = [s.action for s in task.steps][-12:]
        opts = {k: str(v).strip() for k, v in (self.cfg.docs["questions"].get("unfinished") or {}).items()}
        why = "failed"
        if len(opts) >= 2:
            try:
                ans = ctx.gate.decide(self.redactor(task.id), {**state, "history": tried, "screens_seen": list(task.memory.screen_notes.values())[-12:]},
                                      {"why": choice(self.cfg.question("why_unfinished"), opts)}, task=task.id)
                why = (ans.get("why") or {}).get("choice", "failed")
                log.info("diagnosis: %s %s", why, {k: round(v, 2) for k, v in ((ans.get("why") or {}).get("probabilities") or {}).items()})
            except DeciderError:
                pass
        screen = (obs.window, obs.screen_text[:300])
        if why == "blocked":   # the planner already had its turns during the run: here it only words the reason
            if not task.blocked_reason:
                self._consult(task, ctx, obs, f"{reason}; only the user seems able to continue: tried " + "; ".join(tried))
            return self._finish(task, "blocked", f"{reason}; " + (task.blocked_reason or self.cfg.question("blocked_reason")),
                                {"tried": tried, "screen": screen, "screens_seen": list(task.memory.screen_notes.values())[-6:]})
        if why == "impossible":
            return self._finish(task, "failed", f"{reason}; the app answered that this is not available or found nothing", {"tried": tried, "screen": screen})
        if why == "unclear":
            return self._finish(task, "failed", f"{reason}; the goal is unclear on this screen — say more precisely what is wanted", {"tried": tried, "screen": screen})
        return self._finish(task, "failed", reason, {"tried": tried, "screen": screen})

    def _alternatives(self, probs: dict[str, float], by_id: dict[str, Affordance]) -> list[dict[str, Any]]:
        k = int(self.cfg.get("engine.top_k", 3))
        top = [x for x in sorted(probs.items(), key=lambda kv: -kv[1]) if x[0] in by_id][:k]
        return [{**by_id[i].public(), "p": round(p, 3)} for i, p in top]
