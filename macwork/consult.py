"""Working with the planner: what it is told (the brief), asking it for a route, letting it write text, and
turning its concrete suggestions into options for the decider."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .decider import DeciderError, noul
from .model import Affordance, Observation, Task
from .observe import Ctx, named_combo
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
        if backend is None or self.cfg.get("planner.when", "auto") == "never":
            return False
        # Two different reasons to think again, and they used to draw on one allowance. "I am not sure about
        # this screen" can be said before anything has been tried; "what I just did went wrong" comes with
        # evidence. A task spent its replans at steps 0 and 2 on the first kind, then typed a formula the app
        # rejected — the reason was written on the screen — and had none left for the one moment that called
        # for it. Whether the last step went wrong is a judgement the decider already makes every step
        # (`progress`), so a replan that follows one is counted on its own.
        last = task.steps[-1] if task.steps else None
        went_wrong = last is not None and (not last.ok or float(last.decision.get("progress_after", 1.0))
                                           < float(self.cfg.get("engine.thresholds.progress_bad", 0.2)))
        if went_wrong:
            if not self.allowance_left(task, "corrections"):
                return False
            problem = f"the last action did not do what it was for ({last.action[:80]}); what the screen says now is in the context. {problem}"
        elif not self.allowance_left(task, "replans"):
            return False
        # Asking the same question of the same screen gets the same answer, and this answer is not cheap:
        # measured on this Mac, one replan is 1.5-2.7s of local model, and one task spent 12 of its 21
        # seconds being told the same thing three times. What it was asked about is the screen and how far
        # the plan has got; when neither has moved, the engine acts on what it already has.
        # …and "what I just did went wrong" is a different question from "I am not sure about this screen",
        # even on the same screen at the same point of the plan: keyed without it, a task stuck on one
        # screen had already used the key on the first kind, and the correction — the one the allowance
        # above exists for — was refused as already asked.
        here = (self._signature(ctx.app, obs) if ctx is not None and obs is not None else "", task.plan_i, len(task.steps) > 0, went_wrong)
        if here in task.memory.consulted:
            log.info("planner: already asked about this screen")
            return False
        task.memory.consulted.add(here)
        planning = Planning(self.cfg, backend, self.redactor(task.id), self.audit, task.id)
        try:
            ctx_brief = self._brief(task, ctx, obs, affs)
            plan = planning.plan(task.goal, ctx_brief) if task.plan is None else \
                planning.replan(task.goal, ctx_brief, [s.action for s in task.steps], problem)
        except PlannerError as exc:
            log.info("planner: %s", exc)
            task.outputs.setdefault("planner_errors", []).append(str(exc)[:200])
            return False
        self.spend_allowance(task, "corrections" if went_wrong else "replans")
        task.blocked_reason = plan.get("blocked") or ""
        dead = task.memory.no_effect_handles()
        task.tries = [t for t in plan.get("try") or [] if self._suggestion_label(t) not in dead]   # facts beat suggestions
        if not plan["steps"] and not task.tries and not task.blocked_reason:
            return False
        if task.blocked_reason:
            return True
        if plan["steps"]:
            task.plan, task.plan_evidence = plan["steps"], list(plan.get("evidence") or [])
        task.plan, task.plan_i = (task.plan or []), 0
        self._plan_inputs(task, ctx, plan.get("inputs") or {})
        self.audit.record("plan", task=task.id, steps=plan["steps"], problem=problem)
        return True

    def _write_answer(self, task: Task, ctx: Ctx | None, obs: Observation | None) -> None:
        """The goal asked for information: the planner states it from what the screen (and the web) showed.
        Without a planner, the final screen text stays in outputs.result_screen for the caller to read."""
        backend = self.planning_backend
        if backend is None:
            return
        brief = self._brief(task, None, obs)
        brief["screen_text"] = (task.outputs.get("result_screen") or {}).get("text") or brief.get("screen_text", "")
        if task.memory.facts and task.memory.facts.seen:
            # what the task saw in each app, window titles included. Without it the answer was written from the
            # last screen alone: a page whose title was in the window title came back as "not in the information
            # provided", and it is also what the answer is checked against afterwards.
            brief["seen_in_each_app"] = task.memory.facts.brief()
        try:
            answer = Planning(self.cfg, backend, self.redactor(task.id), self.audit, task.id).answer(task.goal, brief)
        except PlannerError as exc:
            log.info("planner answer: %s", exc)
            return
        if not answer:
            return
        # Two questions about it, one request: does it stand (nothing in it invented — asked only when no
        # screen holds it word for word) and does it answer (rather than explain why there is no answer).
        # They were two round trips in a row, ~700 ms each, on the way out of every task that answers.
        stands, answers = self._judge_answer(task, ctx, answer,
                                             ask_stands=task.memory.facts is not None and task.memory.facts.source_of(answer) is None)
        if not stands:
            log.info("refused answer not seen anywhere: %r", answer[:60])
            task.outputs["answer_refused"] = answer[:200]
            return
        task.outputs["answer"] = answer
        # A real run ended "屏幕上没有显示今天的日期，无法得知今天是几号", which stood (nothing in it was
        # invented) and turned the task into `done`. It is reported — the caller should hear what was
        # found — and it is not an answer to the question, so it must not end the task as one.
        if not answers:
            task.outputs["answer_is_no_answer"] = True

    def _judge_answer(self, task: Task, ctx: Ctx | None, answer: str, ask_stands: bool) -> tuple[bool, bool]:
        gate = (getattr(ctx, "gate", None) if ctx is not None else None) or self.gate
        state = {"goal": task.goal, "answer": answer, "seen_in_each_app": task.memory.facts.brief() if task.memory.facts else ""}
        questions = {"answers": noul(self.cfg.question("answer_answers"))}
        if ask_stands:
            questions["stands"] = noul(self.cfg.question("answer_stands"))
        try:
            ans = gate.decide(self.redactor(task.id), state, questions, task=task.id)
        except DeciderError:
            return (not ask_stands), True   # unjudged: an untraceable answer is refused as before; an answer stands as written
        th = self.cfg.section("engine.thresholds")
        stands = True
        if ask_stands:
            got = float(ans.get("stands", {}).get("noul", 0.0))
            log.info("answer not traceable word for word; judged %.2f", got)
            stands = got >= float(th.get("answer_stands", 0.6))
        answers = float(ans.get("answers", {}).get("noul", 1.0)) >= float(th.get("answer_answers", 0.5))
        return stands, answers

    def _plan_inputs(self, task: Task, ctx: Ctx | None, inputs: dict[str, Any]) -> None:
        """Text a plan wants put into `task.inputs`.

        `inputs` is where the *caller's* text lives, and everything downstream treats it that way — it is
        typed, saved and reported as something a person asked for. `_fill` puts a planner's text through a
        provenance guard before it can be typed and records it where the injection checks look; this went
        round all of it with a `setdefault`, so a planner asked for a plan could put anything there and it
        became indistinguishable from what the caller supplied.

        The same guard, then: a value the task has already seen, or one the decider judges to be what the
        task already had, written another way. Anything else is refused and said so.
        """
        for k, v in inputs.items():
            if k in task.inputs:            # the caller's own words are never overwritten
                continue
            text = str(v)
            source = task.memory.facts.source_of(text) if task.memory.facts else None
            if source is None:
                a = Affordance("plan", "keys", "type", f"the value the plan gives for 「{k}」", {})
                if not self._text_stands(task, ctx, text, a):
                    log.info("refused planned input %r: seen nowhere", text[:60])
                    self.audit.record("refused_text", task=task.id, text=text[:120], into=f"inputs.{k}")
                    task.outputs.setdefault("refused_text", []).append({"into": f"inputs.{k}", "text": text[:120]})
                    continue
                source = "judged to be what the task already had"
            task.inputs[k] = text
            task.outputs.setdefault("planner_inputs", []).append({"into": k, "text": text, "source": source})
            self.audit.record("filled_text", task=task.id, into=f"inputs.{k}", source=source)

    def _fill(self, task: Task, ctx: Ctx, obs: Observation | None, a: Affordance, slot: str) -> str | None:
        backend = self.planning_backend
        if backend is None or not self.cfg.get("planner.fill_inputs", True):
            return None
        step = task.plan[task.plan_i] if task.plan and task.plan_i < len(task.plan) else task.goal
        brief = self._brief(task, ctx, obs)
        if task.memory.facts and task.memory.facts.seen:
            brief["seen_in_each_app"] = task.memory.facts.brief()   # the real values this task saw; nothing may be invented
        try:
            text = Planning(self.cfg, backend, self.redactor(task.id), self.audit, task.id).fill(
                task.goal, step, f"{a.label} — {a.slots[slot].desc}", brief) or None
        except PlannerError as exc:
            log.info("planner fill: %s", exc)
            return None
        if text and task.memory.facts:
            source = task.memory.facts.source_of(text)
            if source is None:
                # Not word for word — which a field often cannot take: a path typed into an address bar becomes
                # file:///…, and that one extra word was enough to end a real task on the spot. So it is judged
                # instead: the same value written another way is allowed, a value from nowhere is not.
                if not self._text_stands(task, ctx, text, a):
                    log.info("refused text not seen anywhere: %r", text[:60])
                    self.audit.record("refused_text", task=task.id, text=text[:120], into=a.label)
                    task.outputs.setdefault("refused_text", []).append({"into": a.label, "text": text[:120]})
                    return None
                source = "judged to be what the task already had"
            self.audit.record("filled_text", task=task.id, into=a.label, source=source)
        return text

    def _text_stands(self, task: Task, ctx: Ctx | None, text: str, a: Affordance) -> bool:
        """Text no source holds word for word, judged against everything the task has to draw from."""
        if ctx is None or ctx.gate is None:
            return False
        state = {"goal": task.goal, "inputs": dict(task.inputs), "text": text, "into": a.label,
                 "seen_in_each_app": task.memory.facts.brief()}
        try:
            ans = ctx.gate.decide(self.redactor(task.id), state, {"stands": noul(self.cfg.question("text_stands"))}, task=task.id)
        except DeciderError:
            return False
        stands = float(ans.get("stands", {}).get("noul", 0.0))
        log.info("text not traceable word for word; judged %.2f: %r", stands, text[:60])
        return stands >= float(self.cfg.get("engine.thresholds.text_stands", 0.75))

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
                # named by the app's own menu item where it has one: a bare combo is invisible to both the
                # word list and the classifier, which is how a suggested cmd+s was released as an edit
                named = named_combo(obs, t["keys"]) if obs is not None else None
                out.append(Affordance(f"t{i}", "keys", "key",
                                      self._suggestion_label(t) + (f" ({named})" if named else ""),
                                      {"combo": t["keys"].lower()}))
            elif t.get("type"):
                out.append(Affordance(f"t{i}", "keys", "type", self._suggestion_label(t), {"text": t["type"]}))
            elif t.get("open_url"):
                # the planner sees this Mac's home as `~` (privacy.replace) and writes it back that way — a
                # real run suggested `file://~/Library/...`, which nothing can open. Pseudonyms are restored
                # by the redactor; this replacement is one-way, so it is undone here, for a path only.
                url = str(t["open_url"])
                if url.startswith("file://~"):
                    url = "file://" + os.path.expanduser(url[len("file://"):])
                elif url.startswith("~/"):
                    url = os.path.expanduser(url)
                out.append(Affordance(f"t{i}", "file", "open", self._suggestion_label({**t, "open_url": url}), {"path": "", "url": url}))
            elif t.get("drag") and obs is not None:
                a, b = (_where(obs, name) for name in t["drag"])
                if a and b:
                    out.append(Affordance(f"t{i}", "pointer", "drag", self._suggestion_label(t),
                                          {"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}))
        return out
