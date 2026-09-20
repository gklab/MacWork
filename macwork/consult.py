"""Working with the planner: what it is told (the brief), asking it for a route, letting it write text, and
turning its concrete suggestions into options for the decider."""

from __future__ import annotations

import logging
import re
from typing import Any

from .model import Affordance, Observation, Task
from .observe import Ctx
from .planner import Planning, PlannerError, make_planner

log = logging.getLogger(__name__)


def _where(obs: Observation, name: str) -> tuple[float, float] | None:
    """Centre of the element a planner named: exact label first, then one whose label contains the name."""
    framed = [a for a in obs.affordances if (a.target.get("frame") or ("x" in a.target and "y" in a.target))]
    hit = next((a for a in framed if a.label == name), None) or next((a for a in framed if name and name in a.label), None)
    if hit is None:
        return None
    f = hit.target.get("frame")
    return (f[0] + f[2] / 2, f[1] + f[3] / 2) if f else (float(hit.target["x"]), float(hit.target["y"]))


class ConsultMixin:
    # ------------------------------------------------------------- planning
    @property
    def planning_backend(self) -> Any:
        if self._planner is False:
            return None
        if self._planner is None:
            self._planner = make_planner(self.cfg, self.helper) or False
        return self._planner or None

    def _brief(self, task: Task, ctx: Ctx | None, obs: Observation | None, affs: list[Affordance] | None = None) -> dict[str, Any]:
        brief: dict[str, Any] = {"running_apps": [a.get("name") for a in (ctx.running if ctx else [])][:30]}
        if ctx and ctx.app:
            brief["app"] = ctx.app.get("name")
            brief["app_model"] = self.models.brief(ctx.app)
        if obs:
            brief["window"] = obs.window
            brief["screen_text"] = obs.screen_text[: int(self.cfg.get("planner.context_chars", 600))]
            skip = set(self.cfg.get("planner.context_skip_channels") or ["app", "shortcut", "file"])   # listed separately / noise
            pool = [a for a in (affs or obs.affordances) if a.channel not in skip]
            brief["actions_available"] = [a.label for a in pool][: int(self.cfg.get("planner.context_actions", 120))]
            if "open_windows" in obs.notes:
                brief["open_windows"] = obs.notes["open_windows"] or "none: the app has no window open"
        routines = [sk["goal"] for sk in self.skills.for_app((ctx.app or {}).get("bundle_id") if ctx else None)]
        if routines:
            brief["learned_routines"] = routines[:10]
        if task.steps:
            brief["done_so_far"] = [s.action for s in task.steps[-8:]]
            brief["tried_without_effect"] = sorted({s.action for s in task.steps if not s.ok or not s.events})[:20]
        return brief

    def _consult(self, task: Task, ctx: Ctx | None, obs: Observation | None, problem: str, affs: list[Affordance] | None = None) -> bool:
        """Ask the planner for (new) sub-goals. False when there is none, or it has been asked enough."""
        backend = self.planning_backend
        if backend is None or self.cfg.get("planner.when", "auto") == "never" or task.pace.replans >= int(self.cfg.get("planner.max_replans", 2)) + 1:
            return False
        planning = Planning(self.cfg, backend, self.redactor(task.id), self.audit)
        try:
            ctx_brief = self._brief(task, ctx, obs, affs)
            plan = planning.plan(task.goal, ctx_brief) if task.plan is None else \
                planning.replan(task.goal, ctx_brief, [s.action for s in task.steps], problem)
        except PlannerError as exc:
            log.info("planner: %s", exc)
            task.outputs.setdefault("planner_errors", []).append(str(exc)[:200])
            return False
        task.pace.replans += 1
        task.blocked_reason = plan.get("blocked") or ""
        dead = {k.split("|", 1)[1] for k in task.memory.no_effect}
        task.tries = [t for t in plan.get("try") or [] if self._suggestion_label(t) not in dead]   # facts beat suggestions
        if not plan["steps"] and not task.tries and not task.blocked_reason:
            return False
        if task.blocked_reason:
            return True
        task.plan, task.plan_i = (plan["steps"] or task.plan or []), 0
        for k, v in plan["inputs"].items():
            task.inputs.setdefault(k, v)
        self.audit.record("plan", task=task.id, steps=plan["steps"], problem=problem)
        return True

    def _write_answer(self, task: Task, obs: Observation | None) -> None:
        """The goal asked for information: the planner states it from what the screen (and the web) showed.
        Without a planner, the final screen text stays in outputs.result_screen for the caller to read."""
        backend = self.planning_backend
        if backend is None:
            return
        brief = self._brief(task, None, obs)
        if task.outputs.get("web"):
            brief["found_on_the_web"] = [{"title": n.get("title"), "excerpt": str(n.get("text", ""))[:800]}
                                         for n in (task.outputs["web"].get("found") or [])[:3]]
        brief["screen_text"] = (task.outputs.get("result_screen") or {}).get("text") or brief.get("screen_text", "")
        try:
            answer = Planning(self.cfg, backend, self.redactor(task.id), self.audit).answer(task.goal, brief)
        except PlannerError as exc:
            log.info("planner answer: %s", exc)
            return
        if answer and task.memory.facts and task.memory.facts.source_of(answer) is None:
            log.info("refused answer not seen anywhere: %r", answer[:60])
            task.outputs["answer_refused"] = answer[:200]
            return
        if answer:
            task.outputs["answer"] = answer

    def _fill(self, task: Task, ctx: Ctx, obs: Observation | None, a: Affordance, slot: str) -> str | None:
        backend = self.planning_backend
        if backend is None or not self.cfg.get("planner.fill_inputs", True):
            return None
        step = task.plan[task.plan_i] if task.plan and task.plan_i < len(task.plan) else task.goal
        brief = self._brief(task, ctx, obs)
        if task.memory.facts and task.memory.facts.seen:
            brief["seen_in_each_app"] = task.memory.facts.brief()   # the real values this task saw; nothing may be invented
        try:
            text = Planning(self.cfg, backend, self.redactor(task.id), self.audit).fill(
                task.goal, step, f"{a.label} — {a.slots[slot].desc}", brief) or None
        except PlannerError as exc:
            log.info("planner fill: %s", exc)
            return None
        if text and task.memory.facts:
            source = task.memory.facts.source_of(text)
            if source is None:   # a value nobody has seen: it would be the planner's own invention
                log.info("refused text not seen anywhere: %r", text[:60])
                self.audit.record("refused_text", task=task.id, text=text[:120], into=a.label)
                task.outputs.setdefault("refused_text", []).append({"into": a.label, "text": text[:120]})
                return None
            self.audit.record("filled_text", task=task.id, into=a.label, source=source)
        return text

    def _suggestion_label(self, t: dict[str, Any]) -> str:
        if t.get("keys"):
            return f"press {t['keys'].lower()} (suggested by the planner)"
        if t.get("type"):
            return f"type 「{t['type'][:40]}」 at the cursor (suggested by the planner)"
        if t.get("open_url"):
            return f"open the link 「{t['open_url'][:60]}」 (suggested by the planner)"
        if t.get("drag"):
            return f"drag 「{t['drag'][0][:40]}」 onto 「{t['drag'][1][:40]}」 (suggested by the planner)"
        return str(t.get("action") or "")

    def _suggested(self, task: Task, obs: Observation | None = None) -> list[Affordance]:
        """The planner's keystroke, typing and drag suggestions, as options beside what is on screen (its suggested
        labels are marked on the matching screen actions). The decider chooses; nothing runs unasked. A drag is
        offered only when both of its ends are found on the live screen."""
        out: list[Affordance] = []
        # modifiers are fixed; the key itself is anything one character long or a name the helper knows —
        # a whitelist of US-ANSI punctuation here rejected "cmd+ö" and every non-ASCII layout's keys
        rx = self.cfg.get("engine.key_pattern",
                          r"(?i)((cmd|shift|alt|option|opt|ctrl|control|fn)\+)+(.|f[0-9]{1,2}|return|enter|escape|esc"
                          r"|tab|space|delete|backspace|forwarddelete|up|down|left|right|home|end|pageup|pagedown)")
        for i, t in enumerate(task.tries):
            if t.get("keys") and re.fullmatch(rx, t["keys"]):
                out.append(Affordance(f"t{i}", "keys", "key", self._suggestion_label(t), {"combo": t["keys"].lower()}))
            elif t.get("type"):
                out.append(Affordance(f"t{i}", "keys", "type", self._suggestion_label(t), {"text": t["type"]}))
            elif t.get("open_url"):
                out.append(Affordance(f"t{i}", "file", "open", self._suggestion_label(t), {"path": "", "url": t["open_url"]}))
            elif t.get("drag") and obs is not None:
                a, b = (_where(obs, name) for name in t["drag"])
                if a and b:
                    out.append(Affordance(f"t{i}", "pointer", "drag", self._suggestion_label(t),
                                          {"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}))
        return out
