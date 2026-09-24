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
from typing import Any, Callable, NamedTuple

from .appmodel import signature
from .consult import FRUITLESS, Route
from .contract import DEFERRED, kept, kept_by_change, promise
from .invariants import check_look, check_options, check_step
from .decider import DeciderError, choice, noul
from .privacy import RedactionError
from .act import Outcome
from .onscreen import unreadable
from . import sight
from .model import Affordance, Change, Observation, Step, Task
from .observe import SCREEN, Ctx, arrange, declare_keys, get_provider, observe, page_anchor, page_members, read_the_change
from .skills import Skills

log = logging.getLogger(__name__)


from .model import NOTHING_CHANGED, exact_state  # noqa: E402,F401  (kept importable from here: the history and its tests name them)


def _visible(line: str) -> str:
    """A line without the marks nobody can see. A calculator's display is full of U+200E, and a value that
    gained one has not changed."""
    import unicodedata
    return "".join(ch for ch in line if unicodedata.category(ch) != "Cf").strip()


def change_between(before_text: str, after_text: str, before_window: str | None = None, after_window: str | None = None,
                   before_app: str | None = None, after_app: str | None = None,
                   picture: dict[str, Any] | None = None) -> Change:
    """What the last action did to the screen, as facts (see `model.Change`).

    The history said `button 「2」 -> ok`, and ok means the button went down — not that the display went
    from 12× to 12×2, which is the one thing that would have shown the mistake. The engine holds the screen
    before and the screen after at the moment it looks again; this is only the subtraction.

    Lines, not characters: a screen is a set of things that are there, and what an action does is make
    some of them appear, go away or turn into something else.
    """
    def lines(text: str) -> list[str]:
        return list(dict.fromkeys(v for v in (_visible(x) for x in (text or "").split("\n")) if v))

    before, after = lines(before_text), lines(after_text)
    return Change(app_before=before_app, app_after=after_app, window_before=before_window, window_after=after_window,
                  appeared=[x for x in after if x not in before], gone=[x for x in before if x not in after],
                  picture_share=(picture["share"] if picture and picture.get("cells") else None),
                  picture_where=(picture.get("where") if picture and picture.get("cells") else None),
                  picture_region=(picture.get("region") if picture and picture.get("cells") else None))


def what_changed(before_text: str, after_text: str, before_window: str | None = None, after_window: str | None = None,
                 before_app: str | None = None, after_app: str | None = None, limit: int = 200,
                 picture: dict[str, Any] | None = None) -> str:
    """The sentence for the history. The facts are `change_between`; this only renders them."""
    return change_between(before_text, after_text, before_window, after_window, before_app, after_app, picture).describe(limit)


Progress = Callable[[str], None]


class Spent(NamedTuple):
    """What ran out. `whole_task` is a ceiling over all of a task's runs — steps, working time, calls or
    money — past which nothing continues; the rest is this run's share, which a resumed run gets afresh.

    It was a sentence, and `_can_continue` read the sentence back for three phrases to tell the two kinds
    apart — so the wording of a message was the budget's semantics, and changing one changed the other."""
    text: str
    whole_task: bool = False


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
    aside: Affordance | None = None           # a control that sets an interruption aside: taken before the goal is asked about
    pinned: set[str] = field(default_factory=set)   # ids offered on their own for the planner: in no group's pages
    moves: dict[str, int] = field(default_factory=dict)   # option id -> the planner's try that names it
                                                          # (ConsultMixin._named_by_planner)
    consulted: bool = False                   # the planner was asked at this look: nothing asks it again here

    @property
    def by_id(self) -> dict[str, Affordance]:
        return {a.id: a for a in self.flat}


class LoopMixin:
    # ------------------------------------------------------------------ the loop
    def _loop(self, task: Task, progress: Progress) -> dict[str, Any]:
        e = self.cfg.section("engine")
        deadline = task.run_started + float(e.get("budget_s", 90))   # this run's share, for the yield wait
        last_look: Look | None = None             # the last screen, for the final diagnosis
        while True:
            if task.id in self._cancelled:
                return self._finish(task, "cancelled", "cancelled by the caller")
            spent = self._overspent(task, e)
            if spent is not None:
                # A run's budget running out is not the task failing. If the task has somewhere left to go —
                # it is making progress and has not used its whole-task ceiling — it is handed back for the
                # caller to continue, with everything it has learned kept. Making max_steps bigger instead
                # would mean a task that goes wrong runs for longer before anyone notices.
                if self._can_continue(task, e, spent):
                    return self._finish(task, "need_continue", spent.text,
                                        {"continue_with": "mac_resume", "steps_taken": len(task.steps),
                                         "of_at_most": int(e.get("total_steps", 48))}, cause="budget")
                if last_look is not None:         # out of budget for good: still say *why*
                    return self._diagnose(task, last_look.ctx, last_look.obs, last_look.state, reason=spent.text, cause="budget")
                return self._finish(task, "failed", spent.text, cause="budget")
            self._yield_to_user(task, deadline)
            ctx = self._step_context(task)
            if task.held is not None:             # confirmed / chosen / given input by the caller: no new decision
                chosen, look = task.held, None
            else:
                look = self._look(task, ctx)
                if isinstance(look, dict):
                    return look
                if look is AGAIN:                 # the run's time ran out while the planner was asked
                    continue
                last_look = look
                check_look(task, look.affs, look.sig, look.ctx.app)
                check_options(task, look.affs, look.flat, look.folded)
                if task.plan is None and not task.steps and self.cfg.get("planner.when", "auto") == "always":
                    # The plan that sets the whole trajectory was written before the first look — with no
                    # app, no screen and no running apps in the brief, while every later plan saw all three.
                    # It is written after the first look now, and the look is taken again with the plan in
                    # the state (one observation, once per task). Too late for this run, the loop's own
                    # budget check says what happens next, as for any route that came too late.
                    got = self._consult(task, look.ctx, look.obs, "", look.affs, kind="first")
                    if got or got is Route.LATE:
                        continue
                if look.aside is not None:        # something in the way that can simply be dismissed: do that first.
                    # No decision is asked for it — the floor already judged that it only backs out, which is
                    # the one judgement this needs — and the goal is asked about nothing until the way is clear.
                    progress(f"set aside: {look.aside.label}")
                    done = self._perform(task, ctx, look, look.aside, progress)
                    if done is not None:
                        return done
                    continue
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

    def _overspent(self, task: Task, e: dict[str, Any]) -> Spent | None:
        """What ran out, if anything: the budget for this run, the ceilings for the whole task, or the money.

        Steps and seconds are counted per run so that the caller's thinking time between a ``need_confirm`` and
        its answer is not charged to the task. The ``total_`` ceilings and the decider ceilings are what keep a
        task that is resumed again and again from running (and costing) without end.
        """
        if len(task.steps) - task.run_step0 >= int(e.get("max_steps", 12)):
            return Spent("step budget used up")
        if time.monotonic() - task.run_started > float(e.get("budget_s", 90)):
            return Spent("time budget used up")
        if len(task.steps) >= int(e.get("total_steps", 48)):
            return Spent("this task has taken all the steps it is allowed over all its turns", whole_task=True)
        if task.working_s > float(e.get("total_budget_s", 600)):
            return Spent("this task has taken all the time it is allowed over all its turns", whole_task=True)
        decider = self._decider
        if decider is not None:
            if decider.calls - task.run_calls0 >= int(e.get("max_decisions", 120)):
                return Spent("this run asked the decider as many times as it is allowed", whole_task=True)
            spent = decider.cost_usd - task.run_cost0
            ceiling = float(e.get("max_cost_usd", 0.5))
            if ceiling > 0 and spent >= ceiling:
                return Spent(f"this run has spent its budget of ${ceiling:.2f}", whole_task=True)
        return None

    # ------------------------------------------------------------------ choices the task took back
    def _note_return(self, task: Task, here: str, glance: dict[str, Any] | None = None) -> None:
        """The task is on a screen it has acted from before: what it chose there led back here.

        From a real run of "12×12": on `12×` the decider chose 「2」, saw `12×2`, pressed Clear, was back on
        `12×` — and chose 「2」 again, because a System-1 model gives the same answer to the same state and
        nothing in the state said it had been here. `「2」 -> ok` in the history means the button went
        down, not that it was the right one.

        What is to blame is the action that *left* this state, not the one that came back: the existing
        circle count records the returning step, which here is the correction. And the state is the exact
        one — structure plus what the screen says — because a calculator's structure never changes.

        A step that stayed put is `no_effect`, recorded elsewhere; a retraction is a detour of two or more.
        """
        if task.memory.retracted_at == len(task.steps):
            return                                   # this look was already noted; no step since
        task.memory.retracted_at = len(task.steps)
        left = [s for s in task.steps if s.before == here and s.ok]
        if not left or left[-1] is task.steps[-1]:
            return
        since = task.steps[task.steps.index(left[-1]):]
        if any(s.unseen and s.picture is None for s in since):
            return      # "back in the same state" is a claim about the screen, and these steps do not show on it
        # Same structure and same text, and a different picture, is a different state: in a window that draws
        # itself the text never changes and the picture is the only thing that does. (A calculator back on
        # `12×` looks the same as it did, so this does not get in the way of what the rule was made for.)
        pictures = self.scope(task.id).pictures
        looks = sight.compare(pictures.get(here), glance, int(self.cfg.get("observe.sight.tolerance", 2)))
        if looks and looks["cells"]:
            return
        task.memory.note_withdrawn(here, left[-1].handle)
        log.info("back on a screen already acted from: %r was chosen here and did not hold", left[-1].action[:48])

    def _retracted_here(self, task: Task, here: str) -> list[str]:
        return list(dict.fromkeys(task.memory.retracted.get(here) or []))

    def _withdrawn_here(self, task: Task, here: str) -> set[str]:
        """Taken back from this exact state often enough that it is no longer offered *from here*.

        Told once, withdrawn at `engine.max_retractions` (2). Only from this state: on `12×` a second 「2」
        is a mistake, and on `12×1` it is the answer.
        """
        return task.memory.withdrawn_from(here, int(self.cfg.get("engine.max_retractions", 2)))

    def _can_continue(self, task: Task, e: dict[str, Any], spent: Spent) -> bool:
        """Is there anything left to continue *with*? Only this run's share is gone, the task has done
        something, and the last thing it did had an effect — a task going nowhere should stop going."""
        if not bool(e.get("checkpoint", True)) or not task.steps:
            return False
        if spent.whole_task:
            return False                                   # a whole-task ceiling: that is the end of it
        last = task.steps[-1]
        # An effect is not only a visible one. Reading a file, reading a window in full, running a
        # Shortcut and taking the clipboard all return `wait=False` and raise no Accessibility event — so
        # a long task that ended its turn on a *read* was told it had got nowhere and scored failed, one
        # line above a comment saying this is not a count of UI events.
        if not (last.ok and (last.events or last.produced)):
            return False
        # whether the task is getting somewhere is the decider's judgement, not a count of UI events
        progress = last.decision.get("progress")
        return progress is None or float(progress) >= float(self.cfg.get("engine.thresholds.progress_bad", 0.2))

    def _step_context(self, task: Task) -> Ctx:
        # Where a task that names no app begins: in the app in front (engine.start_in_front_app), or in none.
        # For the first look only — once it has done something, a look follows the app it works in, and after
        # an action that says nothing about where it went (a key), the app in front. An eval run begins in
        # none: 13 of the 29 v2 tasks name no app, and each began wherever the task before it left the screen.
        first = not task.steps and not task.target and not task.app
        front = not first or bool(self.cfg.get("engine.start_in_front_app", True))
        ctx = self._ctx(task.goal, task.inputs, task.target or task.app, task.id, front=front)
        ctx.gate = self.gate
        self._note_opened(task, ctx.running)
        host = ctx.app is not None and self._host_app(ctx.app)
        if host:                                       # e.g. an open that landed in the terminal running the engine
            ctx.app = None
        if ctx.app is None and not host and isinstance(task.app, str) and task.app and not task.pace.launched and task.held is None:
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
    def _look(self, task: Task, ctx: Ctx) -> Look | dict[str, Any] | _Again:
        e = self.cfg.section("engine")
        ctx.scope.looks += 1                      # how many looks this step took (see _step_record)
        locked = bool(self.helper.call("session.state").get("screen_locked"))
        waited = None if locked else self._await_app(task, ctx)   # an app starting or busy is not read blind
        t_obs = time.monotonic()
        obs = observe(ctx)
        timing = {"observe": round((time.monotonic() - t_obs) * 1000)}
        if waited is not None:
            timing["ready_wait"] = waited["ms"]
        sig = self._signature(ctx.app, obs)
        dead_before = set(task.memory.no_effect)
        self._learn_from_prev(task, ctx.app, sig, obs)
        last = task.steps[-1] if task.steps else None
        if last is not None and last.change.get("picture_only") and ctx.app and obs.window is not None \
                and str(self.cfg.get("observe.vision.mode", "auto")) != "never":
            # The last action changed the picture and not a word of the tree: an About panel drawn by the
            # app itself, a popover the toolkit does not describe. A real task opened one (a Qt app's
            # 关于), was told "the picture changed (3% of it, at the bottom left); no text on screen did",
            # and had nothing to read it with. What the tree cannot say, the screen can: read it now.
            key = f"{ctx.app['pid']}|{obs.window}"
            wanted = ctx.scope.vision_wanted
            # Where it changed (a step recorded before that was measured has no region, and is read as it was).
            # The tree's fingerprint is the same as before the step, so a read keyed on it is the read from
            # before the step: this look's reads are keyed on the actions taken and good while the window looks
            # as it did (observe._ocr `drawn`), and the one read serves both the window's reading below — which
            # names its unlabeled controls, on this look and later ones — and what the step drew
            # (observe.read_the_change).
            region = last.change.get("picture_region")
            if region:
                obs.notes["_picture_changed"] = True
            if key not in wanted:
                wanted.add(key)
                sight_provider = get_provider("vision")
                # …unless this look's own reading of it already did: a window read on every look (a canvas) went
                # through that reading twice, and every text in it was offered and said twice
                if sight_provider is not None and "vision_boxes" not in obs.notes:
                    try:
                        sight_provider(ctx, obs)
                    except Exception as exc:  # noqa: BLE001  (a look must not fail because the screen could not be read)
                        obs.notes["vision_error"] = str(exc)[:200]
            if region:
                try:
                    read_the_change(ctx, obs, region)
                except Exception as exc:  # noqa: BLE001  (the same)
                    obs.notes["vision_error"] = str(exc)[:200]
                obs.notes.pop("_picture_changed", None)
        here = exact_state(sig, obs.screen_text)
        self._note_return(task, here, obs.notes.get("glance"))
        self._judge_led_back(task, sig)
        if obs.notes.get("glance"):        # what this state looked like the first time it was seen
            seen_as = self.scope(task.id).pictures
            if len(seen_as) < int(self.cfg.get("engine.max_pictures", 200)):
                seen_as.setdefault(here, obs.notes["glance"])
        window_key = f"{(ctx.app or {}).get('pid')}|{obs.window}"
        fp_seen = self._fingerprint(ctx) if (task.steps and self.allowance_left(task, "redo")
                                             and window_key not in self.cache.setdefault("ambient", set())) else None
        # A route asked for beside the loop is taken in once it has landed, before anything else is asked of the
        # planner here: landing on the look where a stretch that got nowhere is found, it would be replaced by the
        # answer to that stretch's question, about steps taken before the route existed, before the decider saw it.
        merged = self._merge_beside(task, ctx)

        # One rule for a task going nowhere, in place of three counts (the same action four times from one
        # screen, an action leading back three times, three actions on one unchanged screen): a step got the
        # task nowhere when it could not be done, broke its promise, was judged no progress, or only led
        # back to a screen already seen. A stretch of them (`engine.max_no_progress`) means the actions in it
        # are not offered again from where they were taken, the planner is asked for a route from here, and
        # the decider is told. Each of the three counts was a guess at what "stuck" looks like from one
        # angle; this is what it is.
        stuck = self._no_progress_run(task)
        mark = f"{len(task.steps)}:{len(stuck)}"      # this stretch: steps taken, and how many of them got nowhere
        if merged:                                    # the route that just landed is tried before it is asked again
            task.memory.stuck_at = mark
        consulted = False
        if len(stuck) >= int(self.cfg.get("engine.max_no_progress", 3)):
            for st in stuck:
                if st.before:
                    task.memory.note_no_progress(st.before.split(":")[0], st.handle)
            # Asked once per stretch, not once per look: the same stretch seen again after a look into a group, a
            # wait or a sub-goal ticked off is the question already asked, and a stretch one step longer is a new
            # one. Its answer spends nothing and ends nothing; what a rethink vote brings is counted there.
            if task.memory.stuck_at != mark:
                where = ""
                if task.plan and task.plan_i < len(task.plan):   # the planner is told which sub-goal they failed to reach
                    where = f"; the plan is still at sub-goal {task.plan_i + 1} 「{str(task.plan[task.plan_i])[:60]}」, so a different way to it is needed, not the same one again"
                got = self._consult(task, ctx, obs, f"the last {len(stuck)} actions got the task nowhere: "
                                    + "; ".join(st.action[:40] for st in stuck[-3:]) + where, obs.affordances, kind="stuck")
                if got is Route.LATE:
                    return AGAIN                  # the run's time is up: the loop's own budget check says what next
                if got is not Route.UNREADABLE:   # a look nobody could read asks again once the app answers
                    task.memory.stuck_at = mark
                consulted = got not in (Route.NONE, Route.UNREADABLE, Route.SPENT, Route.ASKED)
        offered_by_planner = self._suggested(task, obs)
        declare_keys(ctx, obs, offered_by_planner)   # a suggested key goes where this look's keys go
        affs = [a for a in obs.affordances + offered_by_planner if not self._denied(a, ctx.app)]
        affs, aside = self._clear_the_way(task, ctx, obs, affs)   # what is in the way is dealt with before the goal is
        named = self._named_by_planner(task, affs)   # of what is on screen, withheld here or not
        # Keyed on what an action *is* (its identity, else its steady name), not on what it says right now.
        # Facts about this screen — did nothing here, got nowhere from here, could not be done here, came back
        # from here — take the action out, and so does "not what the goal asked for"; each is said to the
        # decider under its own reason, so an option that vanished does not read as never having been there.
        limit = int(self.cfg.get("engine.max_retractions", 2))
        reasons = {a.id: task.memory.withheld_reason(sig, here, a.handle(), self.approval_key(a), limit) for a in affs}
        withheld_here: dict[str, set[str]] = {}          # the decider is told why, by name (see `_state`)
        for a in affs:
            if reasons[a.id] in ("no_effect", "no_progress", "failed", "withdrawn"):
                withheld_here.setdefault(reasons[a.id], set()).add(a.label)
        # The memory is keyed on handles and the decider reads names: what is on offer now by its label, a step
        # taken before by the label it was taken under.
        names = {st.handle: st.action for st in task.steps} | {a.handle(): a.label for a in affs}
        withheld = [a for a in affs if reasons[a.id] == "declined"]
        affs = [a for a in affs if reasons[a.id] is None]
        # An action that is complete is not an option. A real run read a file, was handed all of it, and was
        # offered "read the text of" the same file on each of the next nine steps — and took it three times.
        # The identity is the source's (path, size, modified), so a file that has changed can be read again.
        affs = [a for a in affs if not (a.yields and a.yields in task.memory.yielded)]
        if locked:                                # nothing on screen can be operated; keep what works without UI
            affs = [a for a in affs if a.channel in (e.get("locked_channels") or [])]
            if not affs:
                return self._finish(task, "failed", "the screen is locked", cause="screen_locked")

        offered_ids = {a.id for a in affs}
        moves = {k: i for k, i in named.items() if k in offered_ids}
        took_back = set(self._retracted_here(task, here))
        # What the planner suggested is offered on its own, wherever it lives: its own options and the actions
        # it named, by id, never their whole groups. A named action pinned its group: in a real task 'type into
        # 文本输入区' pinned TextEdit's whole area of 63 controls, and an id starting with "t" also caught
        # read-all's t{n}, which pinned the app's area. Pinned groups went first in the order they were found,
        # so the planner's own options, appended after every provider, were what the budget cut: in that task,
        # on each of the 11 looks that had one.
        pinned = {a.id for a in offered_by_planner} | set(moves)
        opened = self._opened_here(task, ctx, obs)
        fold_over = int(e.get("fold_groups_over", 60))   # also the least of an opened group shown at a time
        flat, folded = arrange(affs, int(e.get("max_options", 200)) - 1, {opened["group"]: opened["start"]} if opened else {},
                               fold_over=fold_over, pinned=pinned, min_page=fold_over)
        left_out = len(affs) - len(flat) - sum(len(v[1]) for v in folded.values())
        if left_out > 0:
            look_note = f"{left_out} more actions did not fit and are not listed"
        options = {"done": "done: the goal is accomplished, stop"} | \
            {a.id: a.describe() + (" — suggested by the planner" if a.id in moves else "")
             + (" — chosen from this exact screen before, and the task then came back here" if a.handle() in took_back else "")
             for a in flat}
        groups = {f"g{i}": k for i, k in enumerate(folded)}
        options |= {g: folded[k][0] for g, k in groups.items()}
        if len(options) < 2:                      # nothing left to do here that has not been tried
            options["none"] = "none: nothing available here helps"

        state = self._state(task, ctx, obs, sig, flat, withheld_here, locked)
        if len(stuck) >= int(self.cfg.get("engine.max_no_progress", 3)):
            state["no_progress"] = f"the last {len(stuck)} actions got the task nowhere: what was tried is not the way"
        if withheld:   # an option that vanishes without a word reads as never having been there
            state["not_offered_because_the_goal_never_asked"] = sorted({a.label for a in withheld})[:8]
        if took_back:   # by name: the handles went out as they were, 'ax|AXButton|One' among them (aff49b2812ff)
            state["chosen_from_this_exact_screen_before_then_came_back"] = sorted({names.get(h, h) for h in took_back})[:8]
        if left_out > 0:
            state["not_all_actions_listed"] = look_note
        questions = {"action": choice(self.cfg.question("action"), options),
                     "move": choice(self.cfg.question("move"), self._moves()),
                     "risky_screen": noul(self.cfg.question("risky_screen"))}
        if task.plan and task.plan_i < len(task.plan):
            questions["step_done"] = noul(self.cfg.question("step_done"))
            expected = self._expected_evidence(task)
            if expected:   # the planner said what the screen shows once this step is done: is it there?
                questions["step_evidence"] = noul(self.cfg.question("step_evidence"), fills={"evidence": expected})
        if task.steps:
            # "Did it have its intended visible effect" was answered yes for every click that changed the
            # screen — a New that opened a template store nine times running. Where the planner said what the
            # last step was taken to reach, progress is whether it brought the screen nearer to that; the
            # generic question is kept for a step taken with no plan to measure against. What the step was
            # taken toward (`toward`, kept with its decision), not where the plan is now: once a sub-goal is
            # ticked off, the next one's evidence, asked about the step that finished the last one, reads as
            # no progress.
            expected = str(task.steps[-1].decision.get("toward") or "")
            questions["progress"] = noul(self.cfg.question("progress_toward"), fills={"evidence": expected}) if expected \
                else noul(self.cfg.question("progress"))
        if self.cfg.get("engine.verify_done", True):
            questions["verified"] = noul(self.cfg.question("done_verify"))   # judged in parallel: no extra round trip
        if task.wants_answer is None:              # once per task: is the goal a question whose answer must come back?
            questions["wants_answer"] = noul(self.cfg.question("wants_answer"))
        # Not asked here, and the reason is worth keeping: extra questions are free (the decider answers four
        # in the time it answers one — 417ms against 580ms, measured), so it was asked what it would do *after*
        # the action it chose, to take that step without a second round trip. Against what was actually chosen
        # next, the prediction was right 3 times in 28 (11%), and no more often when it said it was sure. A
        # step taken on that is a step taken at random, so the round trip stays.
        floor_q, floor_state, floor_map = self._floor_questions(ctx, flat, obs.window)
        task.outputs["result_screen"] = {"app": state["app"], "window": obs.window, "text": obs.screen_text[: int(e.get("result_chars", 800))]}
        task.memory.facts.record(state["app"], obs.window, obs.screen_text, len(task.steps))   # only what was seen may be written
        if task.memory.facts.seen:
            state["seen_in_each_app"] = task.memory.facts.brief()
        look = Look(ctx, obs, sig, locked, affs, flat, folded, groups, options, state, questions, fp_seen, dead_before, timing)
        look.floor_map, look.floor_questions, look.floor_state = floor_map, floor_q, floor_state
        look.aside, look.pinned = aside, pinned
        look.moves, look.consulted = moves, consulted
        return look

    def _judge_led_back(self, task: Task, sig: str) -> None:
        """Did the last step only lead back to a screen the task had seen before the step was taken?

        Judged from when each screen was first seen, so a second look at the screen the step itself led to —
        after a look into a group, a sub-goal ticked off, a plan — cannot make the step a circle. It was judged
        against every screen ever seen, on every look, and appended each time: on 09-22..23, 79 of the 109
        circle entries were added on looks with no step in between, 46 of them the first flag their step ever
        got, and three steps counted as getting nowhere only through such flags made a task replan as stuck
        (92971e3348c4). A later look that finds the screen gone back to an earlier one still counts."""
        last = task.steps[-1] if task.steps else None
        if last is not None and last.before:
            last.led_back = sig != last.before.split(":")[0] and task.memory.first_seen.get(sig, last.n + 1) <= last.n
            task.memory.circles = [st.handle for st in task.steps if st.led_back]
        task.memory.first_seen.setdefault(sig, len(task.steps))

    def _state(self, task: Task, ctx: Ctx, obs: Observation, sig: str, flat: list[Affordance],
               withheld_here: dict[str, set[str]], locked: bool) -> dict[str, Any]:
        """What the decider is shown. goal and inputs come from the user; everything else is what the Mac shows."""
        hist = self._history(task)
        state: dict[str, Any] = {"goal": task.goal, "screen_locked": locked,
                                 # what a provider turned into options is already in front of the decider as options
                                 "inputs": {k: v for k, v in task.inputs.items() if k not in (obs.notes.get("inputs_used") or [])},
                                 "app": (ctx.app or {}).get("name"), "window": obs.window, "screen_text": obs.screen_text,
                                 "history": hist, **self._evidence(obs)}
        # Why each action is left out, said as what it is. All four went out as "tried here without effect",
        # and three of them are not that: one that failed could not be carried out, and one that got nowhere or
        # was taken back may well have changed the screen.
        for reason, key in (("no_effect", "tried_here_without_effect"), ("no_progress", "got_nowhere_from_here"),
                            ("failed", "could_not_be_done_here"), ("withdrawn", "taken_back_from_here")):
            if withheld_here.get(reason):
                state[key] = sorted(withheld_here[reason])[:20]
        if sig not in task.memory.screen_notes:          # every distinct screen, briefly: the evidence for a final diagnosis
            task.memory.screen_notes[sig] = f"{state['app']} — {obs.window or '(no window)'}: {obs.screen_text[:200]}"
        left = [f"{x.get('from')}: {str(x.get('says'))[:80]}" for x in (task.outputs.get("left_for_you") or [])]
        if left:     # on screen, and not this task's to answer: the user has been told
            state["left_for_the_user_to_answer"] = left[:4]
        circling = [s.action for s in task.steps if s.led_back]   # `_judge_led_back`, by the names the steps were taken under
        if circling:
            state["went_in_circles"] = circling[-6:]
        if task.outputs.get("planner_thinks_blocked"):
            state["planner_thinks"] = f"only the user can continue: {task.outputs['planner_thinks_blocked'][:200]} — an opinion, to weigh against the screen"
        last, run = self._run_length(task)
        if run >= int(self.cfg.get("engine.repeat_notice", 3)):
            state["done_over_and_over"] = f"{last} — {run} times in a row now, with the goal still not reached"
        if task.tries:
            state["planner_suggests"] = [self._move_said(t) for t in task.tries][:8]
        learned = self.models.hints(ctx.app, sig, set(a.label for a in flat))
        if learned:
            state["learned"] = learned
        if task.plan and task.plan_i < len(task.plan):
            state["plan"] = task.plan
            state["current_step"] = task.plan[task.plan_i]
            if self._expected_evidence(task):
                state["current_step_expects"] = self._expected_evidence(task)
        if task.steps:
            state["last_action"] = hist[-1]
        return state

    def _expected_evidence(self, task: Task) -> str:
        """What the planner said the screen shows once the current sub-goal is done, or "" when it said nothing."""
        if not task.plan or task.plan_i >= len(task.plan) or task.plan_i >= len(task.plan_evidence or []):
            return ""
        return str(task.plan_evidence[task.plan_i] or "")

    def _answer_before_ending(self, task: Task, look: "Look | None") -> None:
        """Say what the task found out, even when it did not finish.

        The answer was only ever written on the way out through "done". A task asked which item cost the most
        read the file, had every figure in its facts, then spent 39 steps in the sort options and ended
        `failed` with an empty answer — having known it all along. What was learned does not stop being true
        because the task ran out of steps, and a caller that asked a question is owed whatever there is.
        """
        if task.outputs.get("answer") or (task.wants_answer or 0.0) < float(self.cfg.get("engine.thresholds.wants_answer", 0.5)):
            return
        if not (task.memory.facts and task.memory.facts.seen):
            return
        try:
            self._write_answer(task, look.ctx if look else None, look.obs if look else None)
        except Exception as exc:  # noqa: BLE001  (an ending must not fail because the answer could not be written)
            log.info("answer on ending: %s", exc)

    def _run_length(self, task: Task) -> tuple[str, int]:
        """The action just taken, and how many times in a row it has now been taken. Scrolling a list is a
        legitimate repeat; scrolling it eleven times is a task going nowhere, and neither the circle check
        (every scroll shows a new screen) nor the no-effect check (every scroll changes what the screen shows)
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

    def _no_progress_run(self, task: Task) -> list[Step]:
        """The trailing steps that got the task nowhere (`_nowhere`: could not be done, broke their promise, were
        judged no progress by the decider, or only led back to a screen already seen). Oldest first. The same
        judgement, of the last step alone, is what a rethink waits for its route on (`_went_wrong`)."""
        run: list[Step] = []
        for st in reversed(task.steps):
            if st is task.steps[-1] and "progress_after" not in st.decision and st.ok and st.kept is not False and not st.led_back:
                # the newest step is judged on the look that follows it, and this runs during that look: it is
                # not yet known either way, so it neither counts nor ends the run. Counted as "somewhere", it
                # ended every run at length zero, and no task was ever found to be going nowhere by this rule.
                continue
            if not self._nowhere(st):
                break
            run.append(st)
        return list(reversed(run))

    def _taken_here(self, task: Task, sig: str) -> dict[str, int]:
        """How many times each action has already been taken *from this very screen*.

        Counting only consecutive repeats misses the shape a stuck task really has: a real run opened "Go to
        Folder" and pressed Return, over and over, alternating between two screens and getting nowhere. Doing
        the same thing from the same screen again says the last time changed nothing — while a scroll that
        moves down a list leaves a different screen each time and is not counted against itself.
        """
        out: dict[str, int] = {}
        for st in task.steps:
            # a step whose effect cannot be seen leaves "the same screen" behind every time: walking forward
            # four times is not being stuck
            # — unless it *was* seen, by the picture, and the picture did not move: that one counts.
            # (Only for such steps. An ordinary step that changes the picture still counts as before: the
            # stuck shape this rule exists for — open a dialog, escape, open it again — changes the picture
            # every time.)
            if st.before and st.before.split(":")[0] == sig and not (st.unseen and (st.picture is None or st.picture > 0)):
                out[st.handle] = out.get(st.handle, 0) + 1
        return out

    def _history(self, task: Task) -> list[str]:
        """What was done and what it *did* — not only that it was carried out (see `what_changed`)."""
        def line(s: Step) -> str:
            if not s.ok:
                return f"{s.action} -> failed" + (f": {s.error[:100]}" if s.error else "")
            reacted = f" (ui: {', '.join(s.events[:4])})" if s.events else ""
            if s.outcome and s.outcome != NOTHING_CHANGED:
                return f"{s.action} -> {s.outcome}"
            if s.outcome and s.events:
                # the app did react, only not in any text the engine can read — saying "nothing changed"
                # here would be telling the decider something false
                return f"{s.action} -> ok{reacted}, no text on screen changed"
            if s.unseen:     # "(no ui change)" would be read as "it did nothing", and nobody knows that
                return f"{s.action} -> carried out; what it did does not show in anything the engine can read"
            return f"{s.action} -> ok" + (reacted or " (no ui change)")
        return [line(s) for s in task.steps[-int(self.cfg.get("engine.history", 6)):]]

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
            self._floor_answers(floor, look.floor_map)   # recorded by whoever gets the answer, early or late
            classified.set()
        side = threading.Thread(target=classify, name="floor", daemon=True) if look.floor_questions else None
        if side:
            side.start()
        try:
            ans = look.ctx.gate.decide(self.redactor(task.id), look.state, look.questions, task=task.id)
        except DeciderError as exc:
            return self._finish(task, "failed", f"decider: {exc}", cause="decider_unreachable")
        finally:
            # The step does not wait this out. Classifying more actions in that one request made it the
            # slower of the two and the step sat on it: +191 ms a step, measured. A verdict that lands late
            # is not wasted — verdicts are cached per action, so it is there for the next step — and an
            # action chosen before its verdict arrives is classified on its own, as it always was. The short
            # grace is only to catch the common case where it is about to land anyway.
            if side:
                side.join(timeout=float(self.cfg.get("engine.floor_grace_s", 0.15)))
                if not classified.is_set():
                    log.debug("floor classification still out at step %d", len(task.steps))
        look.timing["decide"] = round((time.monotonic() - t_dec) * 1000)
        look.timing["decide_net"] = round(self.decider.last_ms)
        if look.fp_seen is not None and self._fingerprint(look.ctx) not in (look.fp_seen, None) and not self._moves_by_itself(look.ctx, look.obs):
            # the screen kept changing while the decision was made on it: look again (nothing is recorded)
            task.memory.no_effect = look.dead_before
            self.spend_allowance(task, "redo")
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
                             "risky_screen": round(risky_screen, 3), "ms": round(self.decider.last_ms), "timing": look.timing,
                             "toward": self._expected_evidence(task)}   # what a step taken from this look is taken to reach
        for k in ("progress", "step_done", "step_evidence", "verified"):
            if k in ans:
                d[k] = round(float(ans[k].get("noul", 0.0)), 3)
        if "wants_answer" in ans:
            task.wants_answer = float(ans["wants_answer"].get("noul", 0.0))
        log.info("step %d %s", len(task.steps), d)
        last = task.steps[-1] if task.steps else None
        if last is not None and "progress" in d:
            # How a step turned out is only known on the next look. It is a judgement, not a measurement: it
            # feeds the no-progress rule and what the planner is told went wrong, and nothing is withheld on it
            # alone. Below `progress_bad` it was also stored as "no effect" under the screen's structure, and
            # once progress was asked toward the plan's evidence (1245d7f), 6 of the 9 steps judged that low had
            # visibly changed the screen: each was recorded as having done nothing there.
            last.decision["progress_after"] = d["progress"]

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
        if move == "wait" and self.spend_allowance(task, "waits"):
            progress("wait for the app")
            time.sleep(float(self.cfg.get("engine.wait_s", 1.5)))
            return AGAIN
        # A sub-goal advanced on one number, and consecutive confident looks walked a whole plan without a
        # step being taken. Where the planner said what the screen shows once the step is done, the screen
        # has to be judged to show it too: the expected evidence against the observed screen.
        evidence_ok = "step_evidence" not in d or d["step_evidence"] >= float(th.get("step_evidence", 0.6))
        blind = unreadable(look.obs)              # the app answered nothing on this look: it shows no evidence
        if task.steps and task.plan and d.get("step_done", 0.0) >= float(th.get("done", 0.8)) and evidence_ok and not blind:
            task.plan_i += 1                      # a sub-goal is done; only "done" ends the task
            progress(f"sub-goal done: {task.plan[task.plan_i - 1]}")
            return AGAIN
        if look.locked and move in ("blocked", "impossible", "rethink", "ask_user"):
            return self._finish(task, "blocked", "the screen is locked: unlock the Mac and run the task again", cause="screen_locked")
        if blind and move in ("rethink", "ask_user", "blocked", "impossible"):
            # A vote about the route, the user or the goal, made on a look the app did not answer, was made on
            # nothing. Since 22ce357, 144 of the 158 such looks voted rethink and none voted wait, and 12 of
            # the 67 no_route endings came on one. While the decider has waits left, one is spent here: wait
            # and look again. Waiting never acts. Once they are gone, a rethink takes the chosen option, as
            # one that finds no new route always did, without asking a planner about a screen nobody could
            # read or counting it fruitless; the others keep their handling, with an ending that says why.
            if self.spend_allowance(task, "waits"):
                progress("wait for the app to answer")
                time.sleep(float(self.cfg.get("engine.wait_s", 1.5)))
                return AGAIN
            if move == "rethink":
                return self._pick(task, look, key, move, ranked, risky_screen, progress)
        if move in ("blocked", "impossible"):
            return self._on_blocked(task, look, move)
        if move == "ask_user":
            alts = self._alternatives(probs, look.by_id)
            if len(alts) >= 2:
                task.options = {x["id"]: look.by_id[x["id"]] for x in alts}
                reason, cause = self._ending(look.obs, "several different outcomes fit the goal")
                return self._finish(task, "ambiguous", reason, {"choose_one_of": alts}, cause=cause)
            # The decider says the user must choose, and there is no second option to choose between: the
            # goal itself is unclear here. This fell through and acted, so "ask the user" with one candidate
            # meant "do it anyway". The planner gets its say first (it may know a route, or that only the
            # user can continue); failing that, the caller is asked to say more, and the answer comes back
            # as an input — the one channel besides the goal that comes from the user. Waited for: there is
            # nothing to act on meanwhile.
            got = self._rethink_now(task, look)
            if got or got is Route.LATE:
                return AGAIN
            reason, cause = self._ending(look.obs, "the goal can be read more than one way on this screen: say more precisely what is wanted")
            return self._finish(task, "need_input", reason,
                                {"inputs": {"clarification": "what the goal means here, in a sentence"},
                                 "screen": (look.obs.window, look.obs.screen_text[:300])}, cause=cause)
        if move == "rethink":
            r = self._on_rethink(task, look, key, risky_screen)
            if r is not None:
                return r
        return self._pick(task, look, key, move, ranked, risky_screen, progress)

    def _open_group(self, task: Task, look: Look, key: str, progress: Progress) -> bool:
        """Open a folded group, or the next page of one, for the looks that follow (`_opened_here`). It
        replaces whatever was open: one group is open at a time, like a menu."""
        if not self.spend_allowance(task, "looks"):
            return False
        group = look.groups[key]
        label, members = look.folded[group]
        start = page_anchor(page_members(look.affs, group, look.pinned), members[0]) if members else None
        look.ctx.scope.opened = {"group": group, "start": start, "where": ((look.ctx.app or {}).get("pid"), look.obs.window),
                                 "steps": len(task.steps)}
        progress(label.split(" (")[0])
        return True

    def _opened_here(self, task: Task, ctx: Ctx, obs: Observation) -> dict[str, Any] | None:
        """The group the decider opened, while it still applies: in the same app and window, and, for any group
        but the rest of the screen in front, with no step taken since. Otherwise it is closed.

        The rest of the screen stays open across steps on the same window. It is where the work is, not a
        menu: closed after every step, a form whose fields sit in its tail would cost a decider request and
        one of the run's `engine.max_looks` looks for each field."""
        opened = ctx.scope.opened
        if opened and (opened["where"] != ((ctx.app or {}).get("pid"), obs.window)
                       or (opened["group"] != SCREEN and opened["steps"] != len(task.steps))):
            ctx.scope.opened = opened = None
        return opened

    def _on_done(self, task: Task, look: Look, move: str, key: str, probs: dict[str, float], progress: Progress):
        """The decider says done. Final, so: on a settled screen, and confirmed by the stricter question; if not
        confirmed, the action is decided again with "done" off the table (a runner-up is no decision)."""
        if task.steps and not task.pace.settled_done and self._still_moving(look.ctx):
            task.pace.settled_done = True
            log.info("step %d: settling before judging done", len(task.steps))
            return AGAIN
        verified = self._verified_done(task, look.decision)
        # a goal that asks for information is judged by its answer instead (written, then checked twice)
        wants = (task.wants_answer or 0.0) >= float(self.cfg.get("engine.thresholds.wants_answer", 0.5))
        doubt = self._second_opinion_on_done(task, look) if verified and task.steps and not wants else ""
        if verified and not doubt:
            if (task.wants_answer or 0.0) >= float(self.cfg.get("engine.thresholds.wants_answer", 0.5)):
                self._write_answer(task, look.ctx, look.obs)
                # A goal that asks for something to be reported is not accomplished until there is something to
                # report. A real run opened a page and called itself done in one step, before the page was ever
                # on screen to be read; look again instead of handing back "it could not be found".
                if not task.outputs.get("answer") and self.spend_allowance(task, "answer_tries"):
                    log.info("the goal asks for an answer and there is none yet: looking again")
                    return AGAIN
            return self._finish(task, "done", "goal judged accomplished" if task.steps else "already accomplished")
        move = "act" if move == "done" else move
        if key == "done":
            again = {k: v for k, v in look.options.items() if k != "done"}
            if len(again) < 2:
                return self._finish(task, "failed", "judged done but the evidence is missing", cause="evidence_missing")
            try:
                ans2 = look.ctx.gate.decide(self.redactor(task.id), {**look.state, "not_done_yet": doubt or "the screen does not show the goal accomplished"},
                                            {"action": choice(self.cfg.question("action"), again)}, task=task.id)
            except DeciderError as exc:
                return self._finish(task, "failed", f"decider: {exc}", cause="decider_unreachable")
            key = (ans2.get("action") or {}).get("choice", "")
            probs = (ans2.get("action") or {}).get("probabilities") or {}
            look.decision["choice"], look.decision["redecided"] = key, True
            if key in look.groups:
                if self._open_group(task, look, key, progress):
                    return AGAIN
                key = ""
        return move, key, probs

    def _second_opinion_on_done(self, task: Task, look: Look) -> str:
        """Why the planner thinks the goal is not done, or "" when it agrees or is not asked.

        The decider judged "done" in the same request that chose the step, on the same screen, with a bar
        set low on purpose after acting. Real runs ended done with the Open dialog up instead of Settings,
        with 144 still on the calculator's display, with a sign-in that never happened. A different model
        reading the same screen is a second, independent judgement — the same rule as for "blocked" — and
        it costs one planner call at the end of a task, not one per step.
        """
        backend = self.planning_backend
        if backend is None or not self.cfg.get("planner.second_opinion_on_done", True):
            return ""
        if not self.spend_allowance(task, "done_opinions"):
            return ""
        try:     # waited for with no deadline: it is what a "done" is checked against, owed before the task ends
            brief = self._brief(task, look.ctx, look.obs, look.affs, acting=False)
            agrees, why = self._planner_wait(task, self._planner_call(task, lambda planning, stop: planning.judge_done(task.goal, brief)))
        except Exception as exc:  # noqa: BLE001  (an opinion that could not be had is no opinion; it never fails the task)
            log.info("second opinion on done: %s", exc)
            return ""
        if agrees:
            return ""
        doubt = f"a second look says the goal is not done yet: {why}"[:240]
        task.outputs.setdefault("done_doubted", []).append(why[:200])
        log.info("step %d: %s", len(task.steps), doubt)
        return doubt

    def _on_blocked(self, task: Task, look: Look, move: str) -> _Again | dict[str, Any]:
        """Before giving up, a second opinion: the planner may know another way in (it never signs in for the user)."""
        tried = [s.action for s in task.steps]
        problem = ("only the user seems able to continue here" if move == "blocked" else "the goal looks unreachable here") + \
            (": tried " + "; ".join(tried[-6:]) if tried else "")
        got = self._consult(task, look.ctx, look.obs, problem, look.affs, kind=move)
        if got is Route.LATE:
            return AGAIN      # the run's time ran out first: the loop's own budget check says what happens next
        if got and not task.blocked_reason:
            return AGAIN
        if task.blocked_reason or move == "blocked":
            reason, cause = self._ending(look.obs, task.blocked_reason or task.outputs.get("planner_thinks_blocked")
                                         or self.cfg.question("blocked_reason"), "needs_user")
            return self._finish(task, "blocked", reason, {"screen": (look.obs.window, look.obs.screen_text[:300]), "tried": tried[-8:]},
                                cause=cause)
        reason, cause = self._ending(look.obs, "judged unreachable on this Mac", "unreachable")
        return self._finish(task, "failed", reason, {"tried": tried[-8:]}, cause=cause)

    def _on_rethink(self, task: Task, look: Look, key: str, risky_screen: float) -> _Again | dict[str, Any] | None:
        """The decider wants another route. Whether the loop waits for one is a question of evidence.

        A rethink vote stopped the task for a planner call, and when a route came the action chosen with it was
        dropped. On 09-22..23 (62 tasks, held-out goals left out), 23 rethink looks chose one of the planner's own
        suggestions and 7 of them waited for a call, 57 s in all — among them both of 09-23's key tasks, whose
        replans no longer held the 「type 17」 and 「type serendipity」 they had chosen. 96 chose an action on offer
        with nothing gone wrong and waited for 60 calls, 575 s, and after 24 of those the next look chose the same
        action again. On 20 of the 96 the floor's last verdict on the chosen action stopped for it, and no screen
        was judged risky (0.09 at most). So:

        * the planner's own suggestion is taken as it stands, with no question asked and nothing counted;
        * with nothing gone wrong and an action on offer the floor lets through, on a screen not judged risky, the
          route is asked for beside the loop, the action goes through every gate as before, and the route is taken
          in at a later look (`_consult_beside`, `_merge_beside`);
        * otherwise the route is waited for (`_rethink_now`): a step that got nowhere, nothing to act on, an action
          the floor stops for or a screen judged risky — taken, it would stop at the confirmation rather than be
          thought over — a planner that cannot be asked beside the loop (`planner.beside` off, or one that answers
          through the helper), a call still on its way, the planner already asked at this look, or no fruitless
          rethink left, when what this one brings decides whether the task ends here (the ending below reads a
          count a pending answer would leave untouched).

        A rethink that asked and brought nothing new, or had no planner or allowance to ask, counts toward
        `engine.max_fruitless_rethinks` (`consult.FRUITLESS`); one refused as asked already on this exact screen,
        or still on its way, does not. The ending is the one it always was. None: the chosen action is taken."""
        chosen = look.by_id.get(key)
        if chosen is not None and self._planners_own(look, chosen):
            return None
        if self._merge_beside(task, look.ctx):
            # The route asked for beside the loop landed while the decider was deciding: the decision is made again
            # with it, and it is tried before the stretch is asked about (`_look`). Left where it was, the next
            # question would have stopped the call it came in on, and the answer would have been lost with it.
            task.memory.stuck_at = f"{len(task.steps)}:{len(self._no_progress_run(task))}"
            return AGAIN
        th = float(self.cfg.get("engine.thresholds.risky_screen", 0.6))
        # the floor's verdict last: it may be a request, and it is the one `_pick` reads next, from the same cache
        beside = (chosen is not None and not self._went_wrong(task) and self._may_ask_beside()
                  and self.scope(task.id).planning is None and not look.consulted and self.allowance_left(task, "fruitless")
                  and risky_screen < th and not self._floor(task.id, look.ctx, chosen, look.obs.window))
        if beside:
            got = self._consult_beside(task, look.ctx, look.obs, self._rethink_problem(task), look.affs)
        else:
            got = self._rethink_now(task, look)
            if got or got is Route.LATE:
                return AGAIN
        # (A look nobody could read never gets here: `_judge` waits on it, or takes the chosen option, first.)
        if got in FRUITLESS:
            self.spend_allowance(task, "fruitless")      # no new route to be had: after a second time, say why instead of wandering
        # …but not before the screen itself has been given a fair try. A real task gave up after two steps,
        # with the very action it needed sitting in the options, because the planner had nothing to add. The
        # planner having no route is not the same as there being none.
        enough = len(task.steps) >= int(self.cfg.get("engine.min_steps_before_giving_up", 4))
        untried = any(a.label not in {st.action for st in task.steps} for a in look.flat)
        if not self.allowance_left(task, "fruitless") and task.steps \
                and (enough or not untried):
            return self._diagnose(task, look.ctx, look.obs, look.state, reason="no route to the goal was found", cause="no_route")
        return None

    def _rethink_now(self, task: Task, look: Look) -> Route:
        """A route, waited for, never past the run's time. NEW, BLOCKED (the planner's `blocked` becomes an opinion
        the decider sees) and LATE mean deciding again; anything else, that the last answer stands. The planner is
        asked once a look: when the no-progress rule has asked it here, that answer stands (ASKED)."""
        if look.consulted:
            return Route.ASKED
        look.consulted = True
        got = self._consult(task, look.ctx, look.obs, self._rethink_problem(task), look.affs)
        if got is Route.BLOCKED:
            self._planner_opinion(task)
        return got

    @staticmethod
    def _rethink_problem(task: Task) -> str:
        tried = [s.action for s in task.steps]
        return "the actions on screen do not lead toward the goal" + (": tried " + "; ".join(tried[-6:]) if tried else "")

    def _pick(self, task: Task, look: Look, key: str, move: str, ranked: Callable[[], list[str]], risky_screen: float,
              progress: Progress) -> Affordance | _Again | dict[str, Any]:
        """The action to take, through the gates: reading the screen on request, floor actions the goal never asked
        for (dropped), and what the user must confirm."""
        by_id = look.by_id
        if key not in by_id:
            key = next(iter(ranked()), "")
            if not key:                           # nothing to act on: a different route, if there is one, else say so
                if move != "rethink" and not look.consulted:
                    look.consulted = True
                    got = self._consult(task, look.ctx, look.obs, "nothing left to try on this screen", look.affs, kind="nothing_left")
                    if got is Route.LATE or (got and not task.blocked_reason):
                        return AGAIN
                reason, cause = self._ending(look.obs, "no action left to take on this screen", "no_actions")
                return self._finish(task, "failed", reason, {"tried": [s.action for s in task.steps][-8:]}, cause=cause)
        chosen = by_id[key]
        if (chosen.channel == "app" or chosen.verb == "activate") and task.steps and not task.pace.settled_leave and self._still_moving(look.ctx):
            task.pace.settled_leave = True             # the app being left may still be working (a result appearing)
            log.info("step %d: letting the app finish before leaving it", len(task.steps))
            return AGAIN
        if chosen.channel == "vision":            # read the unlabeled controls of this window, then decide again
            look.ctx.scope.vision_wanted.add(chosen.target["key"])
            progress("read the screen")
            if self.spend_allowance(task, "looks"):
                return AGAIN
            chosen = next((by_id[k] for k in ranked() if by_id[k].channel != "vision"), None)
            if chosen is None:
                return self._finish(task, "failed", "no action left to take on this screen", cause="no_actions")
        redactor = self.redactor(task.id)
        leaving_for = self._leaves_for(look.ctx, chosen)
        if leaving_for is not None and not self._serves_goal(task, look.ctx, chosen, leaving_for):
            # leaving for something the goal gives no reason for (e.g. text on a page asked). By its handle: a menu
            # item carries an identity, and a label it was never offered under would withhold nothing
            task.memory.decline(chosen.handle())
            progress(f"not what the goal is about, skipped: {chosen.label}")
            return AGAIN
        floor = self._floor(task.id, look.ctx, chosen, look.obs.window)
        backs_out: bool | None = None

        def harmless(a: Affordance) -> bool:      # asked once, then reused by both gates below
            nonlocal backs_out
            if backs_out is None:
                backs_out = self._backs_out(task, look.ctx, redactor, look.state, a)
            return backs_out

        # "Does the goal call for this?" cannot be the last word on getting out of the way of a dialog: no
        # goal ever asks to close the panel an app put up on its way to the thing that was asked for. A task
        # told to total a column in Numbers met the Open dialog, chose its close button, had it dropped as
        # "not asked for", and then typed the column's name at the cursor instead. So an action that only
        # backs out of where it is stands even when the goal never mentioned it — it still has to pass the
        # confirmation gate below, which is where the user hears about anything that is not merely backing out.
        if floor and not self._goal_calls_for(task, look.ctx, redactor, look.state, chosen) and not harmless(chosen):
            task.memory.decline(self.approval_key(chosen))   # quit, delete, send… the goal never asked for: not worth the user's attention
            progress(f"not asked for, skipped: {chosen.label}")
            return AGAIN
        if self._needs_confirm(chosen, risky_screen, task.approved, harmless, floor=floor):
            because = floor or ["the screen asks to confirm something"]
            granted, offer = self.standing(task.id, look.ctx, chosen, because)
            if granted:
                progress(f"allowed by a standing grant: {chosen.label}")
                return chosen
            task.held = chosen
            return self._finish(task, "need_confirm", "this action is irreversible or outward-facing",
                                self._confirm_pending(chosen.public(), because, offer))
        return chosen

    # ------------------------------------------------------------------ perform
    def _perform(self, task: Task, ctx: Ctx, look: Look | None, chosen: Affordance, progress: Progress) -> dict[str, Any] | None:
        if chosen.channel == "skill":
            self._replay(task, chosen.target["skill"], progress)
            return None
        obs = look.obs if look else None
        # A held action skipped the floor entirely, because `_floor` lives in `_judge` and a held action
        # does not go through it. That is right for one of the three ways an action gets held — the caller
        # has just confirmed *that* action, and asking again is asking twice — and wrong for the other two:
        # resuming from `ambiguous` says which option, never that it is safe, and resuming from
        # `need_input` supplies text for an action nobody has classified.
        if look is None and self.approval_key(chosen) not in task.approved:
            gated = self._floor(task.id, ctx, chosen, (obs.window if obs else None) or (task.target or {}).get("window"))
            granted, offer = self.standing(task.id, ctx, chosen, gated) if gated else (False, None)
            if gated and not granted:
                task.held = chosen
                return self._finish(task, "need_confirm", "this action is irreversible or outward-facing",
                                    self._confirm_pending(chosen.public(), gated, offer))
        params = {k: task.inputs[k] for k in chosen.slots if k in task.inputs}
        for carried in ("text", "url"):   # a planner suggestion carries its own text or link
            if carried in chosen.target and "try" in chosen.target:
                params[carried] = chosen.target[carried]
        missing = {k: s.desc for k, s in chosen.slots.items() if s.required and k not in params}
        for k in list(missing):                   # Jev writes no text: the planner may, before we bother the caller
            text = self._fill(task, ctx, obs, chosen, k)
            if text:
                params[k] = text
                task.outputs.setdefault("typed_by_planner", []).append(
                    {"into": chosen.label, "text": text, "source": (task.memory.admitted.get(text) or {}).get("source", "")})
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
                               context=chosen.context, facts=dict(chosen.facts))   # judged where it will be typed
            floor = self._floor(task.id, ctx, typed, obs.window if obs else None)
            # keyed on the action *with its text*, not on the base label: "type at the cursor" is the same
            # label every time, so one confirmation used to release everything typed after it
            if floor and self.approval_key(typed) not in task.approved:
                if not self._goal_calls_for(task, ctx, self.redactor(task.id), {}, typed):
                    task.memory.decline(self.approval_key(chosen))
                    progress(f"not asked for, skipped: {typed.label[:80]}")
                    return None
                granted, offer = self.standing(task.id, ctx, typed, floor)    # …and so is a standing grant
                if not granted:
                    task.held = chosen
                    task.confirm_key = self.approval_key(typed)   # the question was about the text, so is the yes
                    return self._finish(task, "need_confirm", "this would run or change something outside the goal's app",
                                        self._confirm_pending({**chosen.public(), "text": text[:200]}, floor, offer))
        if "text" in chosen.target and "try" in chosen.target:
            # Where the injection checks look: `evals.check` builds its trace from the step labels and
            # typed_by_planner. Text typed from a move reached it only through its label, which keeps 40
            # characters of it, and 24 of the 139 typing moves chosen in the audit typed more than that. Written
            # here, once the floor has passed the move with its text in it: text it held was not typed.
            task.outputs.setdefault("typed_by_planner", []).append(
                {"into": chosen.label, "text": chosen.target["text"], "source": chosen.target.get("source", "")})
        # by the try it is, not its label: a key suggestion is labelled with the menu item it turns out to be
        # ("press cmd+shift+g (suggested by the planner) (menu Go ▸ Go to Folder…)"), so matching on the
        # bare suggestion never removed it and a real task pressed cmd+shift+g on four steps out of eight. Nor
        # by an id: "read all the text" is t<n>, as the suggestions were, and taking it took one of them away.
        # An option on screen a move names is that move (`Look.moves`), whatever it is labelled now
        taken = look.moves.get(chosen.id) if look is not None else None
        taken = chosen.target.get("try") if taken is None else taken
        task.tries = [t for i, t in enumerate(task.tries) if taken != i and self._suggestion_label(t) != chosen.label]
        task.pace.redo = 0
        progress(chosen.label)
        t0 = time.monotonic()
        # Re-read the floor's verdict for *this* action from its cache (ask=False: no round trip). It was
        # judged when it was chosen, but other actions have been judged since — backing out, the planner's
        # suggestions — and the last one to be judged is not necessarily this one.
        self.cache.pop("floor.last_category", None)
        self._floor(task.id, ctx, chosen, obs.window if obs else None, ask=False)
        self.spend(task, chosen)      # a confirmation covers this run of it, not the next one
        self.last_timing = {}         # a channel that raises sets none: the last step's act and wait are not this one's
        out, events = self._execute(ctx, chosen, params)
        promised = promise(chosen, params)
        held, why = kept(ctx, chosen, params, out, events)   # the promises checked now; the rest at the next look
        if held is False:
            out = Outcome(False, watch_pid=out.watch_pid, target=out.target, output=out.output, error=why, wait=False,
                          typed=out.typed)
            log.info("step %d did not keep its promise: %s", len(task.steps), why)
        decision = look.decision if look else {}
        if held is not None:
            decision = {**decision, "verified_effect": bool(held)}
        if decision.get("timing") is not None:
            decision["timing"].update(getattr(self, "last_timing", {}))
        sig = look.sig if look else None
        before = exact_state(sig, obs.screen_text) if (sig and obs is not None) else None
        # What the floor judged this action to be, taken from the judgement it already made rather than
        # asked again. Anything but navigating or entering text altered something, and `revert` works back
        # through those — a word list of "which verbs are destructive" would hold in two languages at most.
        effect = str(self.cache.get("floor.last_category") or "")
        task.steps.append(Step(len(task.steps), chosen.label, chosen.id, out.ok, events, decision,
                               round((time.monotonic() - t0) * 1000), out.error, chosen.channel, chosen.verb,
                               chosen.context, sorted(params), before, key=chosen.key, effect=effect,
                               produced=bool(out.output), unseen=bool(out.unseen),
                               promise=promised, kept=held, kept_why=why))
        if out.ok and effect not in ("", "navigate", "enter") and ctx.app:
            task.changed.append({"n": len(task.steps) - 1, "action": chosen.label, "effect": effect,
                                 "app": {k: ctx.app.get(k) for k in ("pid", "name", "bundle_id")}})
        log.info("did  %d %s %s", len(task.steps) - 1, chosen.label[:48], {**(decision.get("timing") or {}),
                 "step_total": round((time.monotonic() - t0) * 1000)})
        self._step_record(task, obs, chosen, out.ok, {**(decision.get("timing") or {}), **getattr(self, "last_timing", {})},
                          typed=out.typed, kept=held)
        check_step(task, task.steps[-1], had_look=look is not None)
        task.prev = {"sig": sig, "label": chosen.label, "handle": chosen.handle(), "ok": out.ok, "events": events, "app": ctx.app,
                     "screen": obs.screen_text if obs else None, "window": obs.window if obs else None,
                     "unseen": bool(out.unseen), "glance": (obs.notes.get("glance") if obs else None)}
        task.updated = time.time()
        if out.output:
            task.outputs.update(out.output)
            if out.ok and chosen.yields:
                task.memory.yielded.add(chosen.yields)
        read = (out.output or {}).get("file_read")
        if read and read.get("text"):     # what a file said is as much a fact as what a screen showed
            task.memory.facts.record(read["path"].rsplit("/", 1)[-1], read.get("kind") or "", read["text"], len(task.steps))
        page = (out.output or {}).get("read_window")
        if page and page.get("text"):     # and so is everything a window said, not only the part on screen
            # "read in full" is a claim, and for a window whose tree holds only its chrome it is a false
            # one: what comes back is the toolbar. Record what it was, so the decider is not told the
            # document has been read when the address bar has.
            how = "its tree holds only the window's own controls, not its content" if page.get("thin") else "read in full"
            task.memory.facts.record(page.get("app") or "a window", how, page["text"], len(task.steps))
        ran = (out.output or {}).get("shortcut_result")
        if ran and ran.get("text"):       # and what one of the person's own Shortcuts handed back
            task.memory.facts.record(ran["name"], "a shortcut's result", ran["text"], len(task.steps))
        if out.target:
            task.target = out.target
        if out.final:
            return self._finish(task, "done" if out.ok else "failed", out.error or "")
        return None

    # ------------------------------------------------------------------ where the time went
    def _step_record(self, task: Task, obs: Observation | None, chosen: Affordance, ok: bool, timing: dict[str, Any],
                     typed: dict[str, Any] | None = None, kept: bool | None = None) -> None:
        """One audit record per step, numbers and enums only: which step, how many looks it took, its wall time
        since the last record (or the run's start), each stage's ms from the step's own timing, each provider
        of the deciding look that took 5 ms or more, and what the planner and the decider were asked meanwhile.

        `macwork profile` read the stage timings of the task store, which keeps a task 30 minutes after it ends
        (engine.tasks_ttl_s): the store it read had no tasks left, and planner and provider time had never been
        recorded anywhere. The audit keeps them, rotated, and each report row keeps its task's totals.

        A step that typed keys (`typed`, the helper's reply) also says how the keyboard was handed back and
        whether the text was then read back where it went (`kept`): typed_landed read, settled, unreadable,
        timeout or no_switch, typed_handback_ms, typed_switched, typed_kept. A helper older than protocol 0.2.0
        says none of the first three, and they are null. Never the text, and never the reason."""
        providers = [str(x) for x in self.cfg.get("observe.providers") or []]
        notes = obs.notes if obs is not None else {}
        rec: dict[str, Any] = {"n": len(task.steps) - 1, "channel": chosen.channel, "verb": chosen.verb, "ok": bool(ok),
                               "observe_ms": int(timing.get("observe") or 0), "decide_ms": int(timing.get("decide") or 0),
                               "decide_net_ms": int(timing.get("decide_net") or 0), "act_ms": int(timing.get("act") or 0),
                               "wait_ms": int(timing.get("wait") or 0), "ready_wait_ms": int(timing.get("ready_wait") or 0),
                               "providers": {name: int(notes[f"{name}_ms"]) for name in providers
                                             if isinstance(notes.get(f"{name}_ms"), (int, float)) and notes[f"{name}_ms"] >= 5}}
        if typed is not None:
            # 21 of 265 typing steps on 09-20..23 left the end of their text composing in an input method, which
            # nothing recorded: the next look's candidate panel was the only trace. The hand-back is the fix,
            # and this is how its cost and any step it did not save are counted afterwards.
            landed, ms, switched = typed.get("landed"), typed.get("handback_ms"), typed.get("switched")
            rec.update(typed_landed=landed if isinstance(landed, str) else None,
                       typed_handback_ms=int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
                       typed_switched=switched if isinstance(switched, bool) else None,
                       typed_kept=kept)
        self._clock_out(task, rec)

    def _clock_out(self, task: Task, rec: dict[str, Any]) -> None:
        """Close the stretch of time since the last record: add what is known of it (looks, wall, the planner's
        and the decider's share), write it, keep the task's totals, and start the next stretch."""
        scope = self.scope(task.id)
        if scope.last_step_at is None:            # not inside a run: nothing was clocked
            return
        now = time.monotonic()
        calls = int(getattr(self._decider, "calls", 0) or 0) if self._decider is not None else 0
        mark = self._planner_mark(task)
        # How much of the planner's time the loop sat waiting on (planner_waited_ms) and how much ran beside it
        # (planner_beside_ms): planner_ms is when its answers came back, and says neither.
        rec = {"task": task.id, "observe_ms": 0, "decide_ms": 0, "decide_net_ms": 0, "act_ms": 0, "wait_ms": 0,
               "ready_wait_ms": 0, **rec,
               "looks": scope.looks, "wall_ms": round((now - scope.last_step_at) * 1000),
               "planner_ms": mark[1] - scope.planner_mark[1],
               "planner_calls": mark[0] - scope.planner_mark[0],
               "planner_waited_ms": mark[2] - scope.planner_mark[2],
               "planner_beside_ms": mark[3] - scope.planner_mark[3],
               "decisions": calls - scope.decisions_mark}
        self.audit.record("step", **rec)
        totals = task.timing
        totals["steps"] = int(totals.get("steps", 0)) + ("end" not in rec)
        for key in ("looks", "wall_ms", "observe_ms", "decide_ms", "decide_net_ms", "act_ms", "wait_ms", "ready_wait_ms",
                    "planner_ms", "planner_calls", "planner_waited_ms", "planner_beside_ms", "decisions"):
            totals[key] = int(totals.get(key, 0)) + int(rec.get(key) or 0)
        scope.looks, scope.last_step_at = 0, now
        scope.planner_mark, scope.decisions_mark = mark, calls

    @staticmethod
    def _planner_mark(task: Task) -> tuple[int, int, int, int]:
        """The task's planner counts now: calls, ms, waited_ms, beside_ms (`planner_use`)."""
        use = task.planner_use or {}
        return (int(use.get("calls", 0)), int(use.get("ms", 0)), int(use.get("waited_ms", 0)), int(use.get("beside_ms", 0)))

    def _start_clock(self, task: Task) -> None:
        """A run starts its own clock: the caller's pause between runs is not step time."""
        scope = self.scope(task.id)
        scope.looks, scope.last_step_at = 0, time.monotonic()
        scope.planner_mark = self._planner_mark(task)
        scope.decisions_mark = int(getattr(self._decider, "calls", 0) or 0) if self._decider is not None else 0

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
            out["open_windows"] = obs.notes["open_windows"][:10] or \
                ("none yet: the app is still starting" if obs.notes.get("app_launching") else "none: the app has no window open")
        if obs.notes.get("covered_by"):           # a prompt from another process sits over the app (e.g. a permission request)
            out["covered_by"] = obs.notes["covered_by"]
        if obs.notes.get("clipboard"):            # what a "paste" would put there, by shape (contents stay here)
            out["on_the_clipboard"] = obs.notes["clipboard"]
        if obs.notes.get("screen_text_cut"):      # the pool has a cap; what fell past it is not "not there"
            out["screen_text_cut_short_by"] = f"{obs.notes['screen_text_cut']} characters"
        if obs.notes.get("ax_trusted") is False:
            out["accessibility"] = "not granted to the helper: no window can be read until it is (macwork doctor --ask)"
        if obs.notes.get("typing_withheld"):
            out["typing"] = obs.notes["typing_withheld"]
        if obs.notes.get("window_not_answering"):
            # Said beside open_windows, it read as two facts that contradict each other: "none: the app has no
            # window open" and "nothing of its window can be read". Whether it is starting or busy is the
            # helper's to say, and whether a window of it is on screen the window server's (observe.windows).
            said = ("it is still starting" if unreadable(obs) == "starting" else "it is busy") + \
                ": it does not answer Accessibility yet, so its controls and menus cannot be read"
            if "window_stand_in" in obs.notes:    # the window server was asked
                stand = obs.notes["window_stand_in"]
                said += (f"; its window 「{stand['title']}」 is on screen" if stand and stand.get("title")
                         else "; a window of it is on screen" if stand else "; none of its windows is on screen yet")
            out["app_not_answering"] = said
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
        # …and so are the picture before and the picture after. Only within one app: a glance is of the
        # app's window, and across a switch there is no "same picture" to have changed.
        seen = None
        if prev and (prev.get("app") or {}).get("pid") == (app or {}).get("pid"):
            seen = sight.compare(prev.get("glance"), obs.notes.get("glance"), int(self.cfg.get("observe.sight.tolerance", 2)))
        if prev and task.steps and seen is not None:
            task.steps[-1].picture = round(seen["share"], 4)
        if prev and task.steps and prev.get("screen") is not None and not task.steps[-1].outcome:
            # the screen before and the screen after are both in hand exactly here, and nowhere else
            change = change_between(prev.get("screen") or "", obs.screen_text, prev.get("window"), obs.window,
                                    (prev.get("app") or {}).get("name"), (app or {}).get("name"), picture=seen)
            task.steps[-1].outcome, task.steps[-1].change = change.describe(), change.as_dict()
        if not prev or not prev.get("sig"):
            return
        events = [x for x in prev.get("events", []) if not x.startswith("wait failed")]
        # a window that changes with nobody acting — a clock, live figures — raises events for everything;
        # the engine measures that once per window (`ambient`), and there its events are not evidence
        if f"{(app or {}).get('pid')}|{obs.window}" in self.cache.get("ambient", set()):
            events = []
        # an effect is a new screen, a UI event, or different text on screen (a calculator's display, a field)
        changed = sig != prev["sig"] or bool(events) or (prev.get("screen") is not None and prev["screen"] != obs.screen_text)
        changed = changed or bool(seen and seen["cells"])      # a person would say it changed: they saw it change
        self.models.record(prev.get("app"), prev["sig"], prev["label"], sig, bool(prev.get("ok")), changed)
        handle = prev.get("handle") or prev["label"]
        last = task.steps[-1] if task.steps else None
        if last is not None and last.kept is None and last.promise in DEFERRED and prev.get("ok"):
            # The deferred promises — the screen changes, something opens, a row is selected — are judged
            # here, where the screen before and the screen after are both in hand. A step whose effect may
            # not show in anything observed, with no glance to compare, is not judged at all.
            last.kept, last.kept_why = kept_by_change(last.promise, changed, seen=not (prev.get("unseen") and seen is None))
        if not prev.get("ok"):
            # It could not be carried out — the app was busy, the element went away, a wait timed out. That
            # says something about the moment, not about the action, and this used to remove the action from
            # this screen for the rest of the task all the same. It is withheld while the screen is exactly
            # as it was when it failed (asking again there gets the same failure), and offered again once
            # anything on it has changed.
            task.memory.note_failed(prev["sig"], handle, exact_state(prev["sig"], prev.get("screen") or ""))
        elif last is not None and last.kept is False and last.before:
            # It promised a change and nothing on screen changed: a fact about this action on this exact screen,
            # withheld while the screen is as it was, the same rule as a failure. Under the screen's structure
            # alone it was withheld from every display the window would show. Keyed on the step's own handle:
            # the prev a redo rebuilds names only its label.
            task.memory.note_no_effect(last.before, last.handle)

    # ------------------------------------------------------------------ routines
    def _replay(self, task: Task, skill: dict[str, Any], progress: Progress) -> bool:
        """Replay a learned routine, looking every step up again on the live screen; stop at the first miss."""
        done = 0
        ok = True
        budget = self.cfg.section("engine")
        for st in skill["steps"]:
            # a routine is not a free pass: its steps count against the same budgets, and it stops when cancelled
            if task.id in self._cancelled or self._overspent(task, budget) is not None:
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
            # the same bookkeeping a step taken by the loop gets: a routine that renames, types and saves
            # changed exactly as much as the steps it replays, and `revert` used to answer "this task
            # changed nothing" for one
            effect = str(self.cache.get("floor.last_category") or "")
            task.steps.append(Step(len(task.steps), a.label, a.id, out.ok, events, {"replay": skill["id"]},
                                   round((time.monotonic() - t0) * 1000), out.error, a.channel, a.verb, a.context,
                                   sorted(params), key=a.key, effect=effect, produced=bool(out.output)))
            if out.ok and effect not in ("", "navigate", "enter") and ctx.app:
                task.changed.append({"n": len(task.steps) - 1, "action": a.label, "effect": effect,
                                     "app": {k: ctx.app.get(k) for k in ("pid", "name", "bundle_id")}})
            if out.target:
                task.target = out.target
            if not out.ok:
                ok = False
                break
            done += 1
        self.skills.bump(skill["id"], ok)
        task.outputs.setdefault("routines", []).append({"id": skill["id"], "goal": skill["goal"], "steps_done": done, "of": len(skill["steps"]), "ok": ok})
        return ok

