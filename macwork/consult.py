"""Working with the planner: what it is told (the brief), asking it for a route, letting it write text, and
turning its concrete suggestions into options for the decider."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Any

from .decider import DeciderError, noul
from .model import Affordance, Observation, Task
from .observe import Ctx, named_combo
from .onscreen import unreadable
from .planner import Planning, PlannerError, make_planner

log = logging.getLogger(__name__)

JUDGED = "judged to be what the task already had"   # the source of a text the decider let through


def _where(obs: Observation, name: str) -> tuple[float, float] | None:
    """Centre of the element a planner named: exact label first, then one whose label contains the name."""
    framed = [a for a in obs.affordances if (a.target.get("frame") or ("x" in a.target and "y" in a.target))]
    hit = next((a for a in framed if a.label == name), None) or next((a for a in framed if name and name in a.label), None)
    if hit is None:
        return None
    f = hit.target.get("frame")
    return (f[0] + f[2] / 2, f[1] + f[3] / 2) if f else (float(hit.target["x"]), float(hit.target["y"]))


def planner_down(task: Task) -> bool:
    """The planner was asked, never answered, and was out of reach or refused at least once: whatever the task
    did after that, it did without the planner it was built to have."""
    use = task.planner_use or {}
    errors = use.get("errors") or {}
    return int(use.get("calls", 0)) >= 1 and int(use.get("answered", 0)) == 0 \
        and int(errors.get("unreachable", 0)) + int(errors.get("refused", 0)) >= 1


class ConsultMixin:
    # ------------------------------------------------------------- planning
    @property
    def planning_backend(self) -> Any:
        if self._planner is False:
            return None
        if self._planner is None:
            self._planner = make_planner(self.cfg, self.helper) or False
        return self._planner or None

    def _planning(self, task: Task) -> Planning | None:
        """The one way a task reaches its planner: every ask redacted for this task, audited under its id, and
        counted and timed into `task.planner_use`. There were four hand-built ones and none of them counted."""
        backend = self.planning_backend
        if backend is None:
            return None
        return Planning(self.cfg, backend, self.redactor(task.id), self.audit, task.id, usage=task.planner_use)

    def _brief(self, task: Task, ctx: Ctx | None, obs: Observation | None, affs: list[Affordance] | None = None,
               acting: bool = True) -> dict[str, Any]:
        """What the planner is told.

        `acting=False` is for a question about what the screen already shows — is the goal done, what is the
        answer, what goes into this field. The actions on offer, the app's declared commands and the other
        running apps are what a route is made of: prompt to be read and nothing such a question needs. On this
        Mac's local model every thousand tokens of prompt is about three seconds, and the second opinion on
        "done" is asked at the end of nearly every task. What fields hold and which menu items are checked is
        told instead, since the action labels were where that used to be read.
        """
        brief: dict[str, Any] = {"running_apps": [a.get("name") for a in (ctx.running if ctx else [])][:30]} if acting else {}
        if ctx and ctx.app:
            brief["app"] = ctx.app.get("name")
            if acting:
                brief["app_model"] = self.models.brief(ctx.app)
        if obs:
            brief["window"] = obs.window
            brief["screen_text"] = obs.screen_text[: int(self.cfg.get("planner.context_chars", 600))]
            seen = self._evidence(obs)
            if acting:
                skip = set(self.cfg.get("planner.context_skip_channels") or ["app", "shortcut", "file"])   # listed separately / noise
                # …except the apps the goal names, first. Shown only the apps that happened to be running, a
                # planner routed a lookup the goal put in 「词典」 through the person's own browser and terminal,
                # because nothing it was shown said 词典 is an app on this Mac.
                named = [a for a in (affs or obs.affordances) if a.target.get("named")]
                pool = named + [a for a in (affs or obs.affordances) if a.channel not in skip]
                brief["actions_available"] = [a.label for a in pool][: int(self.cfg.get("planner.context_actions", 120))]
            else:
                brief.update({k: v for k, v in seen.items() if k != "controls_on_screen"})
            # What the app has open, and whether it answered at all, in the decider's words for either kind of
            # question. The route prompts logged at 07:24:35 and 07:25:18 on 09-23 said only "open_windows:
            # none: the app has no window open" of an app that was starting, and both plans began by opening
            # its window.
            brief.update({k: seen[k] for k in ("open_windows", "app_not_answering") if k in seen})
        if acting:
            routines = [sk["goal"] for sk in self.skills.for_app((ctx.app or {}).get("bundle_id") if ctx else None)]
            if routines:
                brief["learned_routines"] = routines[:10]
        if task.steps:
            brief["done_so_far"] = [s.action for s in task.steps[-8:]]
            if acting:
                brief["tried_without_effect"] = sorted({s.action for s in task.steps if not s.ok or not s.events})[:20]
        return brief

    def _may_still_wait(self, task: Task, ctx: Ctx | None, obs: Observation) -> bool:
        """This look read nothing of the app, and waiting may still change that: it is still starting, the next
        look may wait for it (`ready_waits` left, and it has not stayed silent through one), or the decider may
        (`waits` left). Once none of that holds, the planner may be asked with the app's silence in its brief:
        for an app that never answers, a way around it is the only way on."""
        why = unreadable(obs)
        if not why:
            return False
        pid = (ctx.app or {}).get("pid") if ctx is not None else None
        ready_wait = pid is not None and pid not in ctx.scope.silent and self.allowance_left(task, "ready_waits")
        return why == "starting" or ready_wait or self.allowance_left(task, "waits")

    def _consult(self, task: Task, ctx: Ctx | None, obs: Observation | None, problem: str, affs: list[Affordance] | None = None) -> bool:
        """Ask the planner for (new) sub-goals. False when there is none, or it has been asked enough."""
        backend = self.planning_backend
        if backend is None or self.cfg.get("planner.when", "auto") == "never":
            return False
        if obs is not None and self._may_still_wait(task, ctx, obs):
            # Before anything is spent or remembered as asked: the same question can be asked once the app
            # answers. 53 plans since 22ce357 were made right after a look the app had not answered.
            log.info("planner: not asked while the app cannot be read and can still be waited for")
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
        planning = self._planning(task)
        try:
            ctx_brief = self._brief(task, ctx, obs, affs)
            # What the floor stops for, in policy's words. The planner did not know, and wrote routes through a
            # terminal for a chip model, a file count, a deletion and a Safari version — each one a stop to ask
            # the user (a command is `execute`) on a task that had a route through the apps' own windows.
            # Beside the instructions rather than in the context: it is the same on every call, so it belongs
            # in the part of the prompt a local server keeps.
            asks_first = list(self.floor_categories().values())
            plan = planning.plan(task.goal, ctx_brief, asks_first) if task.plan is None else \
                planning.replan(task.goal, ctx_brief, [s.action for s in task.steps], problem, asks_first)
        except PlannerError as exc:
            log.info("planner: %s", exc)
            task.outputs.setdefault("planner_errors", []).append(str(exc)[:200])
            return False
        self.spend_allowance(task, "corrections" if went_wrong else "replans")
        # Asked again, the planner may give the plan it gave last time. A real task was handed the same two
        # sub-goals three times running, 25-40 s apiece on this Mac's local model, and each time looked again
        # instead of acting. The same answer is no new route — which is what `max_fruitless_rethinks` counts,
        # and past it the task says why it is stuck instead of asking a fourth time.
        route = list(plan["steps"]) + [self._suggestion_label(t) for t in plan.get("try") or []]
        if route and route == task.memory.last_route and not plan.get("blocked"):
            log.info("planner: the same route as last time")
            return False
        task.memory.last_route = route
        task.blocked_reason = plan.get("blocked") or ""
        # Every try the planner gave. What did nothing is a fact about one exact screen, applied where the options
        # are built (`Memory.withheld_reason`): a suggestion that did nothing on this screen is withheld on it and
        # offered on any other. Dropped here, it was dropped from every screen.
        task.tries = list(plan.get("try") or [])
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
        brief = self._brief(task, None, obs, acting=False)
        brief["screen_text"] = (task.outputs.get("result_screen") or {}).get("text") or brief.get("screen_text", "")
        if task.memory.facts and task.memory.facts.seen:
            # what the task saw in each app, window titles included. Without it the answer was written from the
            # last screen alone: a page whose title was in the window title came back as "not in the information
            # provided", and it is also what the answer is checked against afterwards.
            brief["seen_in_each_app"] = task.memory.facts.brief()
        try:
            answer = self._planning(task).answer(task.goal, brief)
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
        """Text a plan gives, kept as the planner's (`Memory.admitted`) once `_admit_text` lets it through.

        It went into `task.inputs`, where the *caller's* text lives, and everything downstream took it for the
        caller's: the decider was shown it under state['inputs'] (344 looks in 28 tasks in the audit), beside an
        instruction that only goal and inputs come from the user; `source_of` counted it as "the caller's
        inputs", so any later text made of it traced to the user; and under the key 'text' it filled every text
        slot — 450bbbdad38a typed the plan's 'hello' at the cursor where the goal wanted HELLO WORLD. It is still
        recorded where the injection checks look (`outputs.planner_inputs`) and in the audit. What leaving
        `task.inputs` costs was measured: in 19 tasks with a planner file or query input, the file options it
        produced appeared in 8 and were never chosen.
        """
        for k, v in inputs.items():
            if k in task.inputs:            # the caller gave this one: the plan's value for it is not taken
                continue
            text = str(v)
            got = self._admit_text(task, ctx, text, f"the value the plan gives for 「{k}」")
            if got is None:
                log.info("refused planned input %r: seen nowhere", text[:60])
                self._refuse_text(task, text, f"inputs.{k}")
                continue
            if self._hold_text(task, text, got, k):      # recorded once, however many plans give it again
                task.outputs.setdefault("planner_inputs", []).append({"into": k, "text": text, "source": got[0]})
                self.audit.record("filled_text", task=task.id, into=f"inputs.{k}", source=got[0])

    def _fill(self, task: Task, ctx: Ctx, obs: Observation | None, a: Affordance, slot: str) -> str | None:
        backend = self.planning_backend
        if backend is None or not self.cfg.get("planner.fill_inputs", True):
            return None
        step = task.plan[task.plan_i] if task.plan and task.plan_i < len(task.plan) else task.goal
        brief = self._brief(task, ctx, obs, acting=False)
        if task.memory.facts and task.memory.facts.seen:
            brief["seen_in_each_app"] = task.memory.facts.brief()   # the real values this task saw; nothing may be invented
        try:
            text = self._planning(task).fill(task.goal, step, f"{a.label} — {a.slots[slot].desc}", brief) or None
        except PlannerError as exc:
            log.info("planner fill: %s", exc)
            return None
        if not text:
            return None
        # Not word for word — which a field often cannot take: a path typed into an address bar becomes
        # file:///…, and that one extra word was enough to end a real task on the spot. So it is judged
        # instead: the same value written another way is allowed, a value from nowhere is not.
        got = self._admit_text(task, ctx, text, a.label)
        if got is None:
            log.info("refused text not seen anywhere: %r", text[:60])
            self._refuse_text(task, text, a.label)
            return None
        self._hold_text(task, text, got, slot)
        self.audit.record("filled_text", task=task.id, into=a.label, source=got[0])
        return text

    def _admit_text(self, task: Task, ctx: Ctx | None, text: str, into: str) -> tuple[str, str] | None:
        """Where text a planner wrote for `into` comes from, as (source, how), or None: it may not be written.

        The one guard for plan inputs and fills, in order: a place the task may draw from holds it word for word
        (`Facts.source_of`: "traced"); it was let through before (`Memory.admitted`); the decider already gave a
        verdict on it with the task where it is now, no step taken since (`Memory.judged`); else the decider
        judges it (`_text_stands`: "judged"). Since plan inputs left `task.inputs`, the skip that kept a replan
        from judging the same input again no longer covers them, and every judgement costs a request whose
        verdict, near the cut, can come out the other way. Only a verdict the decider gave is remembered: with
        none to be had it is refused this time, and asked about next time.
        """
        facts = task.memory.facts
        source = facts.source_of(text) if facts is not None else None
        if source is not None:
            return source, "traced"
        held = task.memory.admitted.get(text)
        if held:
            return str(held.get("source") or JUDGED), str(held.get("how") or "judged")
        said = task.memory.judged.get(text)
        if said and len(said) == 2 and said[1] == len(task.steps):
            stands: bool | None = bool(said[0])
        else:
            stands = self._text_stands(task, ctx, text, into)
            if stands is not None:
                task.memory.judged[text] = [stands, len(task.steps)]
        return (JUDGED, "judged") if stands else None

    def _hold_text(self, task: Task, text: str, got: tuple[str, str], key: str) -> bool:
        """Keep a planner's admitted text as the planner's, with where it came from. True when it is new."""
        had = task.memory.admitted.get(text)
        if had is None or (got[1] == "traced" and had.get("how") != "traced"):
            task.memory.admitted[text] = {"source": got[0], "how": got[1], "key": key}
        return had is None

    def _refuse_text(self, task: Task, text: str, into: str) -> None:
        self.audit.record("refused_text", task=task.id, text=text[:120], into=into)
        task.outputs.setdefault("refused_text", []).append({"into": into, "text": text[:120]})

    def _text_stands(self, task: Task, ctx: Ctx | None, text: str, into: str) -> bool | None:
        """Text no source holds word for word, judged against everything the task has to draw from. None when
        there was no decider to ask, or it did not answer: no verdict, which the caller takes as a refusal."""
        if ctx is None or ctx.gate is None:
            return None
        state = {"goal": task.goal, "inputs": dict(task.inputs), "text": text, "into": into,
                 "seen_in_each_app": task.memory.facts.brief() if task.memory.facts is not None else {}}
        try:
            ans = ctx.gate.decide(self.redactor(task.id), state, {"stands": noul(self.cfg.question("text_stands"))}, task=task.id)
        except DeciderError:
            return None
        stands = float(ans.get("stands", {}).get("noul", 0.0))
        log.info("text not traceable word for word; judged %.2f: %r", stands, text[:60])
        return stands >= float(self.cfg.get("engine.thresholds.text_stands", 0.75))

    def _traced(self, task: Task, obs: Observation | None, text: str) -> str | None:
        """What holds this text word for word — the goal, the caller's inputs or what an app showed, the screen
        being looked at now included (`_look` records it in the facts only as the look ends) — or None."""
        facts = task.memory.facts
        if facts is None:
            return None
        if obs is not None:
            facts = replace(facts, seen=dict(facts.seen))
            facts.record((obs.app or {}).get("name"), obs.window, obs.screen_text, len(task.steps))
        return facts.source_of(text)

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

    @staticmethod
    def _move_said(t: dict[str, Any]) -> str:
        """A move as the decider is told of it, by what kind of move it is. Every move that was not text or an
        action was said as a key press: a link or a drag read 'press None', on 42 looks in 16 tasks in the audit,
        and a list of nothing but links and drags was not said at all (3 looks in 3 tasks)."""
        if t.get("keys"):
            return f"press {t['keys']}"
        if t.get("type"):
            return f"type {t['type']}"
        if t.get("open_url"):
            return f"open {t['open_url']}"
        if t.get("drag"):
            return f"drag {t['drag'][0]} onto {t['drag'][1]}"
        return str(t.get("action") or "")

    def _suggested(self, task: Task, obs: Observation | None = None) -> list[Affordance]:
        """The planner's keystroke, typing and drag suggestions, as options beside what is on screen (its suggested
        labels are marked on the matching screen actions). The decider chooses; nothing runs unasked. A drag is
        offered only when both of its ends are found on the live screen, and text to type only once the goal, the
        caller's inputs or a screen the task saw holds it word for word."""
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
                # Only text the goal, the caller's inputs or a screen the task saw holds word for word, checked
                # again on every look (no request): until then the move waits, and 391 is offered once a screen
                # has shown 391. A move's text went to the keyboard with no check at all. 3ac98732a3d2 typed the
                # planner's '17×23=391' before any screen had shown 391, and 7ffdee1b4ce3 refused 391 as a plan
                # input and offered 'type 「391」' two seconds later; of the 139 typing moves chosen in the audit,
                # 19 (in 9 tasks) typed text nothing the task had seen held — 391 before Calculator showed it, the
                # planner's own definition of a word, a sysctl command. The decider's judgement is no substitute
                # here: it let that definition through at 0.76 in 8b1a7646fc9f, when all the task had looked at
                # was Finder with no window open, and scored 391 at 0.74 in 54d03f49a1e0, against a cut of 0.75.
                # Text it lets through is never a move on offer: it goes only into a slot a plan's fill gives it
                # for (`_fill`), and typing it there is judged by the floor again with the text in its label. The
                # verdict is kept (`Memory.admitted`): the same text is not judged again, for that slot or another.
                source = self._traced(task, obs, t["type"])
                if source is not None:
                    out.append(Affordance(f"t{i}", "keys", "type", self._suggestion_label(t),
                                          {"text": t["type"], "source": source}))
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
