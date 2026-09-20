"""The decision loop, as four phases per step:

    look     observe the screen, keep the facts, fit the options into one choice, build the state
    ask      one decider request (action, move, risk, progress, done) — and check the screen did not move meanwhile
    judge    turn the answers into one of: act on an affordance / look again / finish
    perform  run the affordance, wait for the UI, record the step

Each phase returns ``AGAIN`` (decide again), a finished task result (a dict), or its product. The engine holds no
strategy: which move to make is the decider's answer; the handlers below only carry it out.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .appmodel import signature
from .contract import kept
from .decider import DeciderError, choice, noul
from .privacy import RedactionError
from .act import Outcome
from .model import Affordance, Observation, Step, Task
from .observe import Ctx, arrange, group_of, observe
from .skills import Skills

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class _Again:
    def __repr__(self) -> str:
        return "AGAIN"


AGAIN = _Again()   # decide again (nothing was done): a look inside a group, a wait, a new plan, a redo


@dataclass
class Look:
    """Everything one step knows about the screen and what it asked."""
    ctx: Ctx
    obs: Observation
    sig: str
    locked: bool
    affs: list[Affordance]                    # every usable action (the planner sees these)
    flat: list[Affordance]                    # offered as options
    folded: dict[str, tuple[str, list[Affordance]]]
    groups: dict[str, str]                    # option id -> folded group key
    options: dict[str, str]
    state: dict[str, Any]
    questions: dict[str, Any]
    fp_seen: int | None                       # window fingerprint when looked at (speculation check)
    dead_before: set[str]
    timing: dict[str, int] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    floor_map: dict[str, str] = field(default_factory=dict)      # question id -> the verdict key it answers
    floor_questions: dict[str, Any] = field(default_factory=dict)
    floor_state: dict[str, Any] = field(default_factory=dict)    # no screen text: the floor is judged on the action alone

    @property
    def by_id(self) -> dict[str, Affordance]:
        return {a.id: a for a in self.flat}


class LoopMixin:
    # ------------------------------------------------------------------ the loop
    def _loop(self, task: Task, progress: Progress) -> dict[str, Any]:
        e = self.cfg.section("engine")
        deadline = task.run_started + float(e.get("budget_s", 90))   # this run's share, for the yield wait
        last_look: Look | None = None             # the last screen, for the final diagnosis
        if task.plan is None and self.cfg.get("planner.when", "auto") == "always":
            self._consult(task, None, None, "")
        while True:
            if task.id in self._cancelled:
                return self._finish(task, "cancelled", "cancelled by the caller")
            spent = self._overspent(task, e)
            if spent:
                # A run's budget running out is not the task failing. If the task has somewhere left to go —
                # it is making progress and has not used its whole-task ceiling — it is handed back for the
                # caller to continue, with everything it has learned kept. Making max_steps bigger instead
                # would mean a task that goes wrong runs for longer before anyone notices.
                if self._can_continue(task, e, spent):
                    return self._finish(task, "need_continue", spent,
                                        {"continue_with": "mac_resume", "steps_taken": len(task.steps),
                                         "of_at_most": int(e.get("total_steps", 48))})
                if last_look is not None:         # out of budget for good: still say *why*
                    return self._diagnose(task, last_look.ctx, last_look.obs, last_look.state, reason=spent)
                return self._finish(task, "failed", spent)
            self._yield_to_user(task, deadline)
            ctx = self._step_context(task)
            if task.held is not None:             # confirmed / chosen / given input by the caller: no new decision
                chosen, look = task.held, None
            else:
                look = self._look(task, ctx)
                if isinstance(look, dict):
                    return look
                last_look = look
                ans = self._ask(task, look)
                if isinstance(ans, dict) and "task_id" in ans:
                    return ans
                if ans is AGAIN:
                    continue
                chosen = self._judge(task, look, ans, progress)
                if isinstance(chosen, dict):
                    return chosen
                if chosen is AGAIN:
                    continue
            task.held = None
            done = self._perform(task, ctx, look, chosen, progress)
            if done is not None:
                return done

    def _overspent(self, task: Task, e: dict[str, Any]) -> str:
        """What ran out, if anything: the budget for this run, the ceilings for the whole task, or the money.

        Steps and seconds are counted per run so that the caller's thinking time between a ``need_confirm`` and
        its answer is not charged to the task. The ``total_`` ceilings and the decider ceilings are what keep a
        task that is resumed again and again from running (and costing) without end.
        """
        if len(task.steps) - task.run_step0 >= int(e.get("max_steps", 12)):
            return "step budget used up"
        if time.monotonic() - task.run_started > float(e.get("budget_s", 90)):
            return "time budget used up"
        if len(task.steps) >= int(e.get("total_steps", 48)):
            return "this task has taken all the steps it is allowed over all its turns"
        if task.working_s > float(e.get("total_budget_s", 600)):
            return "this task has taken all the time it is allowed over all its turns"
        decider = self._decider
        if decider is not None:
            if decider.calls - task.run_calls0 >= int(e.get("max_decisions", 120)):
                return "this run asked the decider as many times as it is allowed"
            spent = decider.cost_usd - task.run_cost0
            ceiling = float(e.get("max_cost_usd", 0.5))
            if ceiling > 0 and spent >= ceiling:
                return f"this run has spent its budget of ${ceiling:.2f}"
        return ""

    def _can_continue(self, task: Task, e: dict[str, Any], spent: str) -> bool:
        """Is there anything left to continue *with*? Only this run's share is gone, the task has done
        something, and the last thing it did had an effect — a task going nowhere should stop going."""
        if not bool(e.get("checkpoint", True)) or not task.steps:
            return False
        if "over all its turns" in spent or "budget of $" in spent or "as many times" in spent:
            return False                                   # a whole-task ceiling: that is the end of it
        last = task.steps[-1]
        if not (last.ok and last.events):
            return False
        # whether the task is getting somewhere is the decider's judgement, not a count of UI events
        progress = last.decision.get("progress")
        return progress is None or float(progress) >= float(self.cfg.get("engine.thresholds.progress_bad", 0.2))

    def _step_context(self, task: Task) -> Ctx:
        ctx = self._ctx(task.goal, task.inputs, task.target or task.app, task.id)
        ctx.gate = self.gate
        self._note_opened(task, ctx.running)
        if ctx.app is None and isinstance(task.app, str) and task.app and not task.pace.launched and task.held is None:
            task.pace.launched = True                  # the caller said where to work: open it (once), as a recorded step
            inst = self._installed_named(task.app)
            if inst:
                task.held = Affordance("launch", "app", "open", f"open app {inst['name']}",
                                       {"path": inst["path"], "bundle_id": inst.get("bundle_id"), "name": inst["name"]})
        if ctx.app and ctx.app.get("bundle_id"):
            task.apps.setdefault(ctx.app["bundle_id"], ctx.app)
            task.start_app = task.start_app or ctx.app["bundle_id"]
        return ctx

    # ------------------------------------------------------------------ look
    def _look(self, task: Task, ctx: Ctx) -> Look | dict[str, Any]:
        e = self.cfg.section("engine")
        t_obs = time.monotonic()
        locked = bool(self.helper.call("session.state").get("screen_locked"))
        obs = observe(ctx)
        timing = {"observe": round((time.monotonic() - t_obs) * 1000)}
        sig = self._signature(ctx.app, obs)
        dead_before = set(task.memory.no_effect)
        self._learn_from_prev(task, ctx.app, sig, obs)
        window_key = f"{(ctx.app or {}).get('pid')}|{obs.window}"
        fp_seen = self._fingerprint(ctx) if (task.steps and task.pace.redo < int(self.cfg.get("engine.verify.max_redo", 1))
                                             and window_key not in self.cache.setdefault("ambient", set())) else None

        affs = [a for a in obs.affordances + self._suggested(task, obs) if not self._denied(a, ctx.app)]
        for label, times in self._taken_here(task, sig).items():           # done from this very screen already
            if times >= int(self.cfg.get("engine.max_repeats", 4)):         # enough of that one: it has had its turns
                task.memory.no_effect.add(f"{sig}|{label}")
        # …and the shape a stuck task really has is not "again" but "again, and back where I was": a real run
        # opened the same 「文件 ▸ 打开…」 six times, escaping each time, and every escape left a screen just
        # different enough that a per-screen count started over. What matters is that the action keeps leading
        # somewhere this task has already been, so that is what is counted — wherever it is taken from.
        going_nowhere = int(self.cfg.get("engine.max_circles", 3))
        for label in set(task.memory.circles):
            if task.memory.circles.count(label) >= going_nowhere:
                task.memory.declined.add(label)
        dead_here = {a.label for a in affs if f"{sig}|{a.label}" in task.memory.no_effect}
        affs = [a for a in affs if a.label not in dead_here and a.label not in task.memory.declined]   # facts: did nothing / not asked for
        if locked:                                # nothing on screen can be operated; keep what works without UI
            affs = [a for a in affs if a.channel in (e.get("locked_channels") or [])]
            if not affs:
                return self._finish(task, "failed", "the screen is locked")

        suggested = {t.get("action") for t in task.tries if t.get("action")}
        pinned = task.memory.expanded | {group_of(a)[0] for a in affs if a.label in suggested or a.id.startswith("t")}
        flat, folded = arrange(affs, int(e.get("max_options", 200)) - 1, pinned, fold_over=int(e.get("fold_groups_over", 60)))
        left_out = len(affs) - len(flat) - sum(len(v[1]) for v in folded.values())
        if left_out > 0:
            look_note = f"{left_out} more actions did not fit and are not listed"
        options = {"done": "done: the goal is accomplished, stop"} | \
            {a.id: a.describe() + (" — suggested by the planner" if a.label in suggested else "") for a in flat}
        groups = {f"g{i}": k for i, k in enumerate(folded)}
        options |= {g: folded[k][0] for g, k in groups.items()}
        if len(options) < 2:                      # nothing left to do here that has not been tried
            options["none"] = "none: nothing available here helps"

        state = self._state(task, ctx, obs, sig, flat, dead_here, suggested, locked)
        if left_out > 0:
            state["not_all_actions_listed"] = look_note
        questions = {"action": choice(self.cfg.question("action"), options),
                     "move": choice(self.cfg.question("move"), self._moves()),
                     "risky_screen": noul(self.cfg.question("risky_screen"))}
        if task.plan and task.plan_i < len(task.plan):
            questions["step_done"] = noul(self.cfg.question("step_done"))
        if task.steps:
            questions["progress"] = noul(self.cfg.question("progress"))
        if self.cfg.get("engine.verify_done", True):
            questions["verified"] = noul(self.cfg.question("done_verify"))   # judged in parallel: no extra round trip
        if task.wants_answer is None:              # once per task: is the goal a question whose answer must come back?
            questions["wants_answer"] = noul(self.cfg.question("wants_answer"))
        floor_q, floor_state, floor_map = self._floor_questions(ctx, flat, obs.window)
        task.outputs["result_screen"] = {"app": state["app"], "window": obs.window, "text": obs.screen_text[: int(e.get("result_chars", 800))]}
        task.memory.facts.record(state["app"], obs.window, obs.screen_text, len(task.steps))   # only what was seen may be written
        if task.memory.facts.seen:
            state["seen_in_each_app"] = task.memory.facts.brief()
        look = Look(ctx, obs, sig, locked, affs, flat, folded, groups, options, state, questions, fp_seen, dead_before, timing)
        look.floor_map, look.floor_questions, look.floor_state = floor_map, floor_q, floor_state
        return look

    def _state(self, task: Task, ctx: Ctx, obs: Observation, sig: str, flat: list[Affordance], dead_here: set[str],
               suggested: set[str | None], locked: bool) -> dict[str, Any]:
        """What the decider is shown. goal and inputs come from the user; everything else is what the Mac shows."""
        hist = self._history(task)
        state: dict[str, Any] = {"goal": task.goal, "inputs": dict(task.inputs), "screen_locked": locked,
                                 "app": (ctx.app or {}).get("name"), "window": obs.window, "screen_text": obs.screen_text,
                                 "history": hist, **self._evidence(obs)}
        if dead_here:
            state["tried_here_without_effect"] = sorted(dead_here)[:20]
        if sig in task.memory.screens_seen and task.steps and task.steps[-1].before and task.steps[-1].before.split(":")[0] != sig:
            task.memory.circles.append(task.steps[-1].action)   # the last step only led back to a screen already seen
        task.memory.screens_seen.add(sig)
        if sig not in task.memory.screen_notes:          # every distinct screen, briefly: the evidence for a final diagnosis
            task.memory.screen_notes[sig] = f"{state['app']} — {obs.window or '(no window)'}: {obs.screen_text[:200]}"
        if task.memory.circles:
            state["went_in_circles"] = task.memory.circles[-6:]
        last, run = self._run_length(task)
        if run >= int(self.cfg.get("engine.repeat_notice", 3)):
            state["done_over_and_over"] = f"{last} — {run} times in a row now, with the goal still not reached"
        if suggested or any(t.get("keys") or t.get("type") for t in task.tries):
            state["planner_suggests"] = [t.get("action") or f"press {t.get('keys')}" if not t.get("type") else f"type {t['type']}"
                                         for t in task.tries][:8]
        learned = self.models.hints(ctx.app, sig, set(a.label for a in flat))
        if learned:
            state["learned"] = learned
        if task.plan and task.plan_i < len(task.plan):
            state["plan"] = task.plan
            state["current_step"] = task.plan[task.plan_i]
        if task.steps:
            state["last_action"] = hist[-1]
        return state

    def _run_length(self, task: Task) -> tuple[str, int]:
        """The action just taken, and how many times in a row it has now been taken. Scrolling a list is a
        legitimate repeat; scrolling it eleven times is a task going nowhere, and neither the circle check
        (every scroll shows a new screen) nor the no-effect check (the decider kept calling it progress)
        can see it. The count is a plain fact about what was done, so the decider is simply told."""
        if not task.steps:
            return "", 0
        last = task.steps[-1].action
        run = 0
        for st in reversed(task.steps):
            if st.action != last:
                break
            run += 1
        return last, run

    def _taken_here(self, task: Task, sig: str) -> dict[str, int]:
        """How many times each action has already been taken *from this very screen*.

        Counting only consecutive repeats misses the shape a stuck task really has: a real run opened "Go to
        Folder" and pressed Return, over and over, alternating between two screens and getting nowhere. Doing
        the same thing from the same screen again says the last time changed nothing — while a scroll that
        moves down a list leaves a different screen each time and is not counted against itself.
        """
        out: dict[str, int] = {}
        for st in task.steps:
            if st.before and st.before.split(":")[0] == sig:
                out[st.action] = out.get(st.action, 0) + 1
        return out

    def _history(self, task: Task) -> list[str]:
        return [f"{s.action} -> {'ok' if s.ok else 'failed'}" + (f" (ui: {', '.join(s.events[:4])})" if s.events else " (no ui change)")
                for s in task.steps[-int(self.cfg.get("engine.history", 6)):]]

    # ------------------------------------------------------------------ ask
    def _ask(self, task: Task, look: Look) -> dict[str, Any] | _Again:
        t_dec = time.monotonic()
        floor: dict[str, Any] = {}
        classified = threading.Event()

        def classify() -> None:   # its own request, sent at the same time: no screen text in it, no waiting for it
            try:
                answers = look.ctx.gate.decide(self.redactor(task.id), look.floor_state, look.floor_questions, task=task.id)
            except (DeciderError, RedactionError) as exc:
                log.info("floor classification: %s", exc)
                return
            floor.update(answers)      # all at once, so the main thread never reads a half-filled verdict set
            classified.set()
        side = threading.Thread(target=classify, name="floor", daemon=True) if look.floor_questions else None
        if side:
            side.start()
        try:
            ans = look.ctx.gate.decide(self.redactor(task.id), look.state, look.questions, task=task.id)
        except DeciderError as exc:
            return self._finish(task, "failed", f"decider: {exc}")
        finally:
            if side:
                side.join(timeout=float(self.cfg.get("decider.timeout_s", 10)) + 2)
                if classified.is_set():
                    self._floor_answers(floor, look.floor_map)
                else:   # it never came back: those actions stay gated by their words, which is the safe side
                    log.info("floor classification did not return in time for step %d", len(task.steps))
        look.timing["decide"] = round((time.monotonic() - t_dec) * 1000)
        look.timing["decide_net"] = round(self.decider.last_ms)
        if look.fp_seen is not None and self._fingerprint(look.ctx) not in (look.fp_seen, None) and not self._moves_by_itself(look.ctx, look.obs):
            # the screen kept changing while the decision was made on it: look again (nothing is recorded)
            task.memory.no_effect = look.dead_before
            task.pace.redo += 1
            last = task.steps[-1]
            task.prev = task.prev or {"sig": last.before.split(":")[0] if last.before else None, "label": last.action,
                                      "ok": last.ok, "events": last.events, "app": look.ctx.app, "screen": None}
            log.info("step %d: screen moved while deciding, looking again", len(task.steps))
            return AGAIN
        return ans

    # ------------------------------------------------------------------ judge
    def _judge(self, task: Task, look: Look, ans: dict[str, Any], progress: Progress) -> Affordance | _Again | dict[str, Any]:
        """The decider chose an action and a kind of move; carry out the move."""
        th = self.cfg.section("engine.thresholds")
        act = ans.get("action", {})
        key = act.get("choice", "done")
        probs = act.get("probabilities") or {}
        move = (ans.get("move") or {}).get("choice", "act")
        risky_screen = float(ans.get("risky_screen", {}).get("noul", 0.0))
        d = look.decision = {"choice": key, "confidence": round(float(act.get("confidence", probs.get(key, 0.0))), 3), "move": move,
                             "risky_screen": round(risky_screen, 3), "ms": round(self.decider.last_ms), "timing": look.timing}
        for k in ("progress", "step_done", "verified"):
            if k in ans:
                d[k] = round(float(ans[k].get("noul", 0.0)), 3)
        if "wants_answer" in ans:
            task.wants_answer = float(ans["wants_answer"].get("noul", 0.0))
        log.info("step %d %s", len(task.steps), d)
        last = task.steps[-1] if task.steps else None
        if last and last.before and d.get("progress", 1.0) < float(th.get("progress_bad", 0.2)):
            task.memory.no_effect.add(f"{last.before.split(':')[0]}|{last.action}")   # the decider saw no effect: a fact for that screen

        ranked = lambda: [k for k, _p in sorted(probs.items(), key=lambda kv: -kv[1]) if k in look.by_id]  # noqa: E731
        if key in look.groups:                    # see inside a group first, like opening a menu to read it
            if self._open_group(task, look, key, progress):
                return AGAIN
            key = next(iter(ranked()), "done")
        if move == "done" or key == "done":
            r = self._on_done(task, look, move, key, probs, progress)
            if not isinstance(r, tuple):
                return r
            move, key, probs = r
        if move == "wait" and task.pace.waits < int(self.cfg.get("engine.max_waits", 4)):
            task.pace.waits += 1
            progress("wait for the app")
            time.sleep(float(self.cfg.get("engine.wait_s", 1.5)))
            return AGAIN
        if task.steps and task.plan and d.get("step_done", 0.0) >= float(th.get("done", 0.8)):
            task.plan_i += 1                      # a sub-goal is done; only "done" ends the task
            progress(f"sub-goal done: {task.plan[task.plan_i - 1]}")
            return AGAIN
        if look.locked and move in ("blocked", "impossible", "rethink", "ask_user"):
            return self._finish(task, "blocked", "the screen is locked: unlock the Mac and run the task again")
        if move in ("blocked", "impossible"):
            return self._on_blocked(task, look, move)
        if move == "ask_user":
            alts = self._alternatives(probs, look.by_id)
            if len(alts) >= 2:
                task.options = {x["id"]: look.by_id[x["id"]] for x in alts}
                return self._finish(task, "ambiguous", "several different outcomes fit the goal", {"choose_one_of": alts})
        if move == "rethink":
            r = self._on_rethink(task, look)
            if r is not None:
                return r
        return self._pick(task, look, key, move, ranked, risky_screen, progress)

    def _open_group(self, task: Task, look: Look, key: str, progress: Progress) -> bool:
        if task.pace.looks >= int(self.cfg.get("engine.max_looks", 6)):
            return False
        task.memory.expanded.add(look.groups[key])
        task.pace.looks += 1
        progress(f"look into {look.folded[look.groups[key]][0].split(' (')[0].removeprefix('look into ')}")
        return True

    def _on_done(self, task: Task, look: Look, move: str, key: str, probs: dict[str, float], progress: Progress):
        """The decider says done. Final, so: on a settled screen, and confirmed by the stricter question; if not
        confirmed, the action is decided again with "done" off the table (a runner-up is no decision)."""
        if task.steps and not task.pace.settled_done and self._still_moving(look.ctx):
            task.pace.settled_done = True
            log.info("step %d: settling before judging done", len(task.steps))
            return AGAIN
        if self._verified_done(task, look.decision):
            if (task.wants_answer or 0.0) >= float(self.cfg.get("engine.thresholds.wants_answer", 0.5)):
                self._write_answer(task, look.ctx, look.obs)
                # A goal that asks for something to be reported is not accomplished until there is something to
                # report. A real run opened a page and called itself done in one step, before the page was ever
                # on screen to be read; look again instead of handing back "it could not be found".
                if not task.outputs.get("answer") and task.pace.answer_tries < int(self.cfg.get("engine.max_answer_tries", 2)):
                    task.pace.answer_tries += 1
                    log.info("the goal asks for an answer and there is none yet: looking again")
                    return AGAIN
            return self._finish(task, "done", "goal judged accomplished" if task.steps else "already accomplished")
        move = "act" if move == "done" else move
        if key == "done":
            again = {k: v for k, v in look.options.items() if k != "done"}
            if len(again) < 2:
                return self._finish(task, "failed", "judged done but the evidence is missing")
            try:
                ans2 = look.ctx.gate.decide(self.redactor(task.id), {**look.state, "not_done_yet": "the screen does not show the goal accomplished"},
                                            {"action": choice(self.cfg.question("action"), again)}, task=task.id)
            except DeciderError as exc:
                return self._finish(task, "failed", f"decider: {exc}")
            key = (ans2.get("action") or {}).get("choice", "")
            probs = (ans2.get("action") or {}).get("probabilities") or {}
            look.decision["choice"], look.decision["redecided"] = key, True
            if key in look.groups:
                if self._open_group(task, look, key, progress):
                    return AGAIN
                key = ""
        return move, key, probs

    def _on_blocked(self, task: Task, look: Look, move: str) -> _Again | dict[str, Any]:
        """Before giving up, a second opinion: the planner may know another way in (it never signs in for the user)."""
        tried = [s.action for s in task.steps]
        problem = ("only the user seems able to continue here" if move == "blocked" else "the goal looks unreachable here") + \
            (": tried " + "; ".join(tried[-6:]) if tried else "")
        if self._consult(task, look.ctx, look.obs, problem, look.affs) and not task.blocked_reason:
            return AGAIN
        if task.blocked_reason or move == "blocked":
            return self._finish(task, "blocked", task.blocked_reason or self.cfg.question("blocked_reason"),
                                {"screen": (look.obs.window, look.obs.screen_text[:300]), "tried": tried[-8:]})
        return self._finish(task, "failed", "judged unreachable on this Mac", {"tried": tried[-8:]})

    def _on_rethink(self, task: Task, look: Look) -> _Again | dict[str, Any] | None:
        """Another route, from the planner. None: no new route this time (the chosen action is taken instead)."""
        tried = [s.action for s in task.steps]
        problem = "the actions on screen do not lead toward the goal" + (": tried " + "; ".join(tried[-6:]) if tried else "")
        if self._consult(task, look.ctx, look.obs, problem, look.affs):
            task.pace.fruitless = 0
            if task.blocked_reason:
                return self._finish(task, "blocked", task.blocked_reason,
                                    {"screen": (look.obs.window, look.obs.screen_text[:300]), "tried": tried[-8:]})
            return AGAIN
        task.pace.fruitless += 1                       # no new route to be had: after a second time, say why instead of wandering
        # …but not before the screen itself has been given a fair try. A real task gave up after two steps,
        # with the very action it needed sitting in the options, because the planner had nothing to add. The
        # planner having no route is not the same as there being none.
        enough = len(task.steps) >= int(self.cfg.get("engine.min_steps_before_giving_up", 4))
        untried = any(a.label not in {st.action for st in task.steps} for a in look.flat)
        if task.pace.fruitless >= int(self.cfg.get("engine.max_fruitless_rethinks", 2)) and task.steps \
                and (enough or not untried):
            return self._diagnose(task, look.ctx, look.obs, look.state, reason="no route to the goal was found")
        return None

    def _pick(self, task: Task, look: Look, key: str, move: str, ranked: Callable[[], list[str]], risky_screen: float,
              progress: Progress) -> Affordance | _Again | dict[str, Any]:
        """The action to take, through the gates: reading the screen on request, floor actions the goal never asked
        for (dropped), and what the user must confirm."""
        by_id = look.by_id
        if key not in by_id:
            key = next(iter(ranked()), "")
            if not key:                           # nothing to act on: a different route, if there is one, else say so
                if move != "rethink" and self._consult(task, look.ctx, look.obs, "nothing left to try on this screen", look.affs) \
                        and not task.blocked_reason:
                    return AGAIN
                return self._finish(task, "failed", "no action left to take on this screen", {"tried": [s.action for s in task.steps][-8:]})
        chosen = by_id[key]
        if (chosen.channel == "app" or chosen.verb == "activate") and task.steps and not task.pace.settled_leave and self._still_moving(look.ctx):
            task.pace.settled_leave = True             # the app being left may still be working (a result appearing)
            log.info("step %d: letting the app finish before leaving it", len(task.steps))
            return AGAIN
        if chosen.channel == "vision":            # read the unlabeled controls of this window, then decide again
            look.ctx.cache.setdefault("vision.wanted", {}).setdefault(look.ctx.task, set()).add(chosen.target["key"])
            task.pace.looks += 1
            progress("read the screen")
            if task.pace.looks <= int(self.cfg.get("engine.max_looks", 6)):
                return AGAIN
            chosen = next((by_id[k] for k in ranked() if by_id[k].channel != "vision"), None)
            if chosen is None:
                return self._finish(task, "failed", "no action left to take on this screen")
        redactor = self.redactor(task.id)
        if chosen.channel in ("app", "shortcut", "web") and not self._serves_goal(task, look.ctx, chosen):
            task.memory.declined.add(chosen.label)       # leaving for something the goal gives no reason for (e.g. text on a page asked)
            progress(f"not what the goal is about, skipped: {chosen.label}")
            return AGAIN
        floor = self._floor(task.id, look.ctx, chosen, look.obs.window)
        if floor and not self._goal_calls_for(task, look.ctx, redactor, look.state, chosen):
            task.memory.declined.add(chosen.label)       # quit, delete, send… the goal never asked for: not worth the user's attention
            progress(f"not asked for, skipped: {chosen.label}")
            return AGAIN
        harmless = lambda a: self._backs_out(task, look.ctx, redactor, look.state, a)  # noqa: E731
        if self._needs_confirm(chosen, risky_screen, task.approved, harmless, floor=floor):
            task.held = chosen
            return self._finish(task, "need_confirm", "this action is irreversible or outward-facing",
                                {"confirm": chosen.public(), "because": floor or ["the screen asks to confirm something"]})
        return chosen

    # ------------------------------------------------------------------ perform
    def _perform(self, task: Task, ctx: Ctx, look: Look | None, chosen: Affordance, progress: Progress) -> dict[str, Any] | None:
        if chosen.channel == "skill":
            self._replay(task, chosen.target["skill"], progress)
            return None
        obs = look.obs if look else None
        params = {k: task.inputs[k] for k in chosen.slots if k in task.inputs}
        for carried in ("text", "url"):   # a planner suggestion carries its own text or link
            if carried in chosen.target and chosen.id.startswith("t"):
                params[carried] = chosen.target[carried]
        missing = {k: s.desc for k, s in chosen.slots.items() if s.required and k not in params}
        for k in list(missing):                   # Jev writes no text: the planner may, before we bother the caller
            text = self._fill(task, ctx, obs, chosen, k)
            if text:
                params[k] = text
                task.outputs.setdefault("typed_by_planner", []).append({"into": chosen.label, "text": text})
                missing.pop(k)
        if missing:
            task.held = chosen
            return self._finish(task, "need_input", "this step needs text only the caller can provide",
                                {"affordance": chosen.public(), "inputs": missing})
        # a value the caller or planner supplied can change what the action does: typed text can be a command,
        # and a link can be a mailto: or an app's own "do this" URL. Judge the action with that value in it.
        text = str(params.get("text") or params.get("url") or "")
        if text and (chosen.verb in ("type", "type_submit") or "url" in params):
            doing = "typing" if params.get("text") else "opening"
            typed = Affordance(chosen.id, chosen.channel, chosen.verb, f"{chosen.label} — {doing} 「{text[:120]}」", chosen.target,
                               context=chosen.context)
            floor = self._floor(task.id, ctx, typed, obs.window if obs else None)
            if floor and chosen.label not in task.approved:
                if not self._goal_calls_for(task, ctx, self.redactor(task.id), {}, typed):
                    task.memory.declined.add(chosen.label)
                    progress(f"not asked for, skipped: {typed.label[:80]}")
                    return None
                task.held = chosen
                return self._finish(task, "need_confirm", "this would run or change something outside the goal's app",
                                    {"confirm": {**chosen.public(), "text": text[:200]}, "because": floor})
        task.tries = [t for t in task.tries if self._suggestion_label(t) != chosen.label]
        task.pace.redo = 0
        progress(chosen.label)
        t0 = time.monotonic()
        # Re-read the floor's verdict for *this* action from its cache (ask=False: no round trip). It was
        # judged when it was chosen, but other actions have been judged since — backing out, the planner's
        # suggestions — and the last one to be judged is not necessarily this one.
        self.cache.pop("floor.last_category", None)
        self._floor(task.id, ctx, chosen, obs.window if obs else None, ask=False)
        out, events = self._execute(ctx, chosen, params)
        held, why = kept(ctx, chosen, params, out, events)   # did the action keep its promise?
        if held is False:
            out = Outcome(False, watch_pid=out.watch_pid, target=out.target, output=out.output, error=why, wait=False)
            log.info("step %d did not keep its promise: %s", len(task.steps), why)
        decision = look.decision if look else {}
        if held is not None:
            decision = {**decision, "verified_effect": bool(held)}
        if decision.get("timing") is not None:
            decision["timing"].update(getattr(self, "last_timing", {}))
        sig = look.sig if look else None
        before = f"{sig}:{hash(obs.screen_text)}" if (sig and obs is not None) else None
        # What the floor judged this action to be, taken from the judgement it already made rather than
        # asked again. Anything but navigating or entering text altered something, and `revert` works back
        # through those — a word list of "which verbs are destructive" would hold in two languages at most.
        effect = str(self.cache.get("floor.last_category") or "")
        task.steps.append(Step(len(task.steps), chosen.label, chosen.id, out.ok, events, decision,
                               round((time.monotonic() - t0) * 1000), out.error, chosen.channel, chosen.verb,
                               chosen.context, sorted(params), before, key=chosen.key, effect=effect))
        if out.ok and effect not in ("", "navigate", "enter") and ctx.app:
            task.changed.append({"n": len(task.steps) - 1, "action": chosen.label, "effect": effect,
                                 "app": {k: ctx.app.get(k) for k in ("pid", "name", "bundle_id")}})
        log.info("did  %d %s %s", len(task.steps) - 1, chosen.label[:48], {**(decision.get("timing") or {}),
                 "step_total": round((time.monotonic() - t0) * 1000)})
        task.prev = {"sig": sig, "label": chosen.label, "ok": out.ok, "events": events, "app": ctx.app,
                     "screen": obs.screen_text if obs else None}
        task.updated = time.time()
        if out.output:
            task.outputs.update(out.output)
        read = (out.output or {}).get("file_read")
        if read and read.get("text"):     # what a file said is as much a fact as what a screen showed
            task.memory.facts.record(read["path"].rsplit("/", 1)[-1], read.get("kind") or "", read["text"], len(task.steps))
        page = (out.output or {}).get("read_window")
        if page and page.get("text"):     # and so is everything a window said, not only the part on screen
            task.memory.facts.record(page.get("app") or "a window", "read in full", page["text"], len(task.steps))
        ran = (out.output or {}).get("shortcut_result")
        if ran and ran.get("text"):       # and what one of the person's own Shortcuts handed back
            task.memory.facts.record(ran["name"], "a shortcut's result", ran["text"], len(task.steps))
        if out.target:
            task.target = out.target
        if out.final:
            return self._finish(task, "done" if out.ok else "failed", out.error or "")
        return None

    # ------------------------------------------------------------------ facts
    def _evidence(self, obs: Observation) -> dict[str, Any]:
        """What is on screen, for every question (each is judged on the state alone, not on the other questions'
        options): the controls visible now and which menu items carry a check mark (the current mode/toggles)."""
        ev = self.cfg.section("engine.evidence")
        n, width = int(ev.get("controls", 60)), int(ev.get("label_chars", 60))
        seen: list[str] = []
        for a in obs.affordances:
            if a.channel in ("window", "pointer") and a.verb in ("press", "click", "type", "select") and len(seen) < n:
                lab = a.label[:width]
                if lab not in seen:
                    seen.append(lab)
        out: dict[str, Any] = {"controls_on_screen": seen} if seen else {}
        if obs.notes.get("checked"):
            out["checked_menu_items"] = obs.notes["checked"][: int(ev.get("checked", 20))]
        if obs.notes.get("fields"):
            out["field_contents"] = obs.notes["fields"][: int(ev.get("fields", 12))]
        if "open_windows" in obs.notes:           # an empty list is a fact too: the app is running with no window open
            out["open_windows"] = obs.notes["open_windows"][:10] or "none: the app has no window open"
        if obs.notes.get("covered_by"):           # a prompt from another process sits over the app (e.g. a permission request)
            out["covered_by"] = obs.notes["covered_by"]
        if obs.notes.get("clipboard"):            # what a "paste" would put there, by shape (contents stay here)
            out["on_the_clipboard"] = obs.notes["clipboard"]
        if obs.notes.get("window_not_answering"):
            out["app_not_answering"] = "this app is not answering Accessibility; nothing of its window can be read"
        return out

    def _signature(self, app: dict[str, Any] | None, obs: Observation) -> str:
        """Keyed on what each action *is*, not on what it is called: a screen keeps its identity across a
        change of interface language, and so does everything learned about it."""
        parts = [a.identity() for a in obs.affordances if a.channel in ("window", "menu", "pointer")]
        return signature(app, obs.window, parts, by_window=bool(self.cfg.get("appmodel.signature_window", True)))

    def _learn_from_prev(self, task: Task, app: dict[str, Any] | None, sig: str, obs: Observation) -> None:
        """Now that we see where the last step led, remember it (and remember steps that did nothing)."""
        self.models.see(app, sig, obs.window, [a.label for a in obs.affordances if a.channel == "window"][: int(self.cfg.get("appmodel.sample", 12))])
        prev, task.prev = task.prev, None
        if not prev or not prev.get("sig"):
            return
        events = [x for x in prev.get("events", []) if not x.startswith("wait failed")]
        # an effect is a new screen, a UI event, or different text on screen (a calculator's display, a field)
        changed = sig != prev["sig"] or bool(events) or (prev.get("screen") is not None and prev["screen"] != obs.screen_text)
        self.models.record(prev.get("app"), prev["sig"], prev["label"], sig, bool(prev.get("ok")), changed)
        if not (changed and prev.get("ok")):      # nothing happened: never offered again from this screen in this task
            task.memory.no_effect.add(f"{prev['sig']}|{prev['label']}")

    # ------------------------------------------------------------------ routines
    def _replay(self, task: Task, skill: dict[str, Any], progress: Progress) -> bool:
        """Replay a learned routine, looking every step up again on the live screen; stop at the first miss."""
        done = 0
        ok = True
        budget = self.cfg.section("engine")
        for st in skill["steps"]:
            # a routine is not a free pass: its steps count against the same budgets, and it stops when cancelled
            if task.id in self._cancelled or self._overspent(task, budget):
                ok = False
                break
            ctx = self._ctx(task.goal, task.inputs, task.target or task.app, task.id)
            ctx.gate = self.gate
            a = Skills.find(st, observe(ctx))
            params = {k: task.inputs[k] for k in (a.slots if a else {}) if k in task.inputs}
            gated = self._floor(task.id, ctx, a, ctx.app and ctx.app.get("window")) if a is not None else []
            if (a is None or self._denied(a, ctx.app) or self._needs_confirm(a, 0.0, task.approved, floor=gated)
                    or any(s.required and k not in params for k, s in a.slots.items())):
                ok = False
                break
            progress(f"routine: {a.label}")
            t0 = time.monotonic()
            out, events = self._execute(ctx, a, params)
            task.steps.append(Step(len(task.steps), a.label, a.id, out.ok, events, {"replay": skill["id"]},
                                   round((time.monotonic() - t0) * 1000), out.error, a.channel, a.verb, a.context,
                                   sorted(params), key=a.key))
            if out.target:
                task.target = out.target
            if not out.ok:
                ok = False
                break
            done += 1
        self.skills.bump(skill["id"], ok)
        task.outputs.setdefault("routines", []).append({"id": skill["id"], "goal": skill["goal"], "steps_done": done, "of": len(skill["steps"]), "ok": ok})
        return ok

