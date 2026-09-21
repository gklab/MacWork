"""What gets in the way, kept apart from the work.

A window from another process — a permission prompt, an updater, a notification — used to be poured into the
same pool as everything else: its buttons were options toward the goal like any other, its text led the
screen text, and when the decider chose one of those buttons the safety floor stopped the whole task to ask
the user. Told to total a column in Numbers while another app's "allow access to the local network?" prompt
sat on screen, the engine spent its first step on that prompt's button and handed the task back before it
had touched the spreadsheet. Every fix before this one patched a gate; the structure guaranteed the failure.

A person at work handles this with standing rules, not by asking each time. Something that interrupts is one
of three things:

  the task itself   the goal is *about* it ("give WPS network access"): its controls are options like any
                    other, and the floor asks before granting anything, as it always did
  set aside         dismissing it decides nothing — a banner, a "Later", a close button: the engine does that
                    on its own, as a recorded step, and goes back to work
  the owner's       answering it *is* a decision — granting, paying, signing in, agreeing, and refusing any
                    of those just as much: it is never answered. It is left where it is, reported in
                    `outputs.left_for_you`, and the work goes on around it

Which of the three it is comes from judgements the engine already makes — whether leaving the work for it
serves the goal, and what the floor says each of its controls does — not from a list of known dialogs.

Going on around it is usually possible because most of what the engine does never touches the screen:
Accessibility actions, menu commands and keystrokes reach the app whatever floats above it. Only the pointer
and the eyes are blocked, and only where the prompt actually is — so a click target underneath one is not
offered at all (the click would land on the prompt, not on the app).
"""

from __future__ import annotations

import logging
from typing import Any

from .decider import DeciderError, noul
from .model import Affordance, Observation, Task
from .observe import Ctx

log = logging.getLogger(__name__)


def _point(a: Affordance) -> tuple[float, float] | None:
    t = a.target
    if "x" in t and "y" in t:
        return float(t["x"]), float(t["y"])
    f = t.get("frame")
    if isinstance(f, (list, tuple)) and len(f) == 4:
        return f[0] + f[2] / 2, f[1] + f[3] / 2
    return None


def _under(a: Affordance, frame: Any) -> bool:
    """Would acting on this with the pointer land on the interruption instead?"""
    if a.channel != "pointer" or not isinstance(frame, (list, tuple)) or len(frame) != 4:
        return False
    p = _point(a)
    return p is not None and frame[0] <= p[0] <= frame[0] + frame[2] and frame[1] <= p[1] <= frame[1] + frame[3]


class InterruptMixin:
    def _clear_the_way(self, task: Task, ctx: Ctx, obs: Observation,
                       affs: list[Affordance]) -> tuple[list[Affordance], Affordance | None]:
        """The options with interruptions taken out of them, and — when one can simply be set aside — the
        control that does it, to be taken now, before the goal is asked about anything."""
        found = obs.notes.get("interruptions") or []
        if not found:
            return affs, None
        known: dict[str, dict[str, Any]] = task.memory.interruptions
        tries = int(self.cfg.get("engine.interruptions.aside_tries", 2))
        aside: Affordance | None = None
        for it in found:
            key = it["key"]
            controls = [a for a in affs if a.target.get("interruption") == key]
            rec = known.get(key)
            if rec is None:
                rec = known[key] = self._size_up(task, ctx, obs, it, controls)
                log.info("interruption from %s: %s", it.get("from"), rec["kind"])
            if rec["kind"] == "task":
                continue                          # the goal is about it: its controls stay among the options
            if rec["kind"] == "aside":
                control = next((a for a in controls if a.label == rec.get("control")), None)
                if control is not None and rec["tries"] < tries:
                    if aside is None:
                        rec["tries"] += 1
                        aside = control
                else:                             # it would not go: stop pressing at it, and say so
                    rec["kind"] = "owner"
                    self._leave_for_user(task, it, "it would not go away when dismissed")
            # set aside or left alone, its controls are no way of reaching the goal
            affs = [a for a in affs if a.target.get("interruption") != key]
            if rec["kind"] == "owner":
                affs = [a for a in affs if not _under(a, it.get("frame"))]
        return affs, aside

    def _size_up(self, task: Task, ctx: Ctx, obs: Observation, it: dict[str, Any],
                 controls: list[Affordance]) -> dict[str, Any]:
        """Which of the three this interruption is. Asked once per interruption per task."""
        if self._interruption_is_the_task(task, ctx, it):
            return {"kind": "task", "tries": 0}
        way_out = self._way_to_set_aside(task, ctx, obs, controls)
        if way_out is not None:
            return {"kind": "aside", "control": way_out.label, "tries": 0}
        self._leave_for_user(task, it, "answering it is a decision only you can make")
        return {"kind": "owner", "tries": 0}

    def _interruption_is_the_task(self, task: Task, ctx: Ctx, it: dict[str, Any]) -> bool:
        """Is the goal about this prompt? Judged from the goal; the prompt's own words are only what is being
        asked about. A prompt that talks its way into "yes" gains nothing by it: its controls become options,
        and the floor still stands in front of every one of them."""
        if getattr(ctx, "gate", None) is None:
            return False
        q = noul(self.cfg.question("interruption_is_the_task"),
                 fills={"from": str(it.get("from") or "another app"), "text": str(it.get("text") or "")[:300]})
        try:
            ans = ctx.gate.decide(self.redactor(task.id), self._goal_state(task, ctx), {"about": q}, task=task.id)
        except DeciderError:
            return False        # cannot tell: leaving it alone is the side that decides nothing
        return float(ans.get("about", {}).get("noul", 0.0)) >= float(self.cfg.get("engine.thresholds.interruption_is_the_task", 0.6))

    def _way_to_set_aside(self, task: Task, ctx: Ctx, obs: Observation, controls: list[Affordance]) -> Affordance | None:
        """A control on it that the floor judges to only back out — classified in one request, by the same
        floor that classifies everything else. Refusing a permission is not backing out of the question."""
        if not controls or getattr(ctx, "gate", None) is None:
            return None
        questions, state, mapping = self._floor_questions(ctx, controls, obs.window)
        if questions:
            try:
                self._floor_answers(ctx.gate.decide(self.redactor(task.id), state, questions, task=task.id), mapping)
            except DeciderError:
                return None
        verdicts = self.cache.get("floor.verdicts") or {}
        for a in controls:
            category = (verdicts.get(self._floor_key(ctx, a)) or ("", 0.0))[0]
            if category == "back_out" and not self._floor(task.id, ctx, a, obs.window):
                return a
        return None

    def _leave_for_user(self, task: Task, it: dict[str, Any], why: str) -> None:
        left = task.outputs.setdefault("left_for_you", [])
        entry = {"from": it.get("from"), "says": str(it.get("text") or "")[:300], "why": why}
        if not any(x.get("from") == entry["from"] and x.get("says") == entry["says"] for x in left):
            left.append(entry)
            self.audit.record("left_for_user", task=task.id, **{"from": entry["from"], "why": why})
