"""The engine facade. The loop itself is in loop.py (look → ask → judge → perform), acting and waiting in
effects.py, the gates and follow-up questions in judge.py, the planner in consult.py, tidying in tidy.py,
learning an app in learn.py.

The loop: observe -> one decider request (which action, and what kind of move is right now: act, done, rethink,
ask the user, blocked, impossible — plus risk and progress, fanned out) -> policy gates -> act -> wait for the UI
to settle -> repeat. The engine holds no strategy of its own: which move to make is the decider's judgement on the
live screen, a different route is the planner's; the engine keeps facts (what changed nothing), budgets and the
safety floor.

Three levels of control, same engine:
* ``do(goal)``            the decider drives, returns a task (done / failed / need_input / need_confirm / ambiguous)
* ``observe()`` + ``act()`` the caller drives step by step (no decider involved)
* ``resume(task_id, …)``  answer what a pending task asked for
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any, Callable

from .appmodel import AppModels
from .config import Config
from .consult import ConsultMixin
from .decider import Decider, DeciderError, make_decider
from .effects import EffectsMixin
from .facts import Facts
from .grants import Grants
from .helper import Helper, HelperError
from .interrupt import InterruptMixin
from .judge import MOVES, JudgeMixin
from .learn import LearnMixin
from .loop import LoopMixin, Progress
from .policy import PolicyMixin
from .model import PENDING, Affordance, Observation, Task
from .observe import Ctx, installed_apps, observe
from .privacy import Audit, Gate, RedactionError, Redactor
from .skills import Skills
from .store import Store
from .tidy import TidyMixin

log = logging.getLogger(__name__)

__all__ = ["Engine", "MOVES", "Progress"]


def _norm_name(s: str) -> str:
    return "".join(str(s).split()).casefold()


def _names(a: dict[str, Any]) -> list[str]:
    """Ways to call an app: localized name, bundle id, its last part, the .app file name (spacing ignored)."""
    bid = _norm_name(a.get("bundle_id", ""))
    return [_norm_name(a.get("name", "")), bid, bid.rsplit(".", 1)[-1], _norm_name(a.get("file", "")),
            _norm_name(str(a.get("path", "")).rsplit("/", 1)[-1].removesuffix(".app"))]


class Engine(LoopMixin, EffectsMixin, JudgeMixin, PolicyMixin, InterruptMixin, ConsultMixin, TidyMixin, LearnMixin):
    def __init__(self, cfg: Config | None = None, helper: Helper | None = None, decider: Decider | None = None) -> None:
        self.cfg = cfg or Config.load()
        self.helper = helper or Helper(self.cfg)
        self._decider = decider
        self.audit = Audit(self.cfg)
        self.cache: dict[str, Any] = {}
        self.tasks: dict[str, Task] = {}
        self._redactors: dict[str, Redactor] = {}
        self._cancelled: set[str] = set()
        self._last: tuple[Ctx, Observation] | None = None
        self._obs_seq = 0                     # every observation stamps its affordance ids, so a stale id cannot act
        # One Mac, one keyboard, one front app: whatever *drives* it goes one at a time, and that is not a
        # limitation to design away. What was wrong is that *looking* waited for it too — a `mac_observe`
        # blocked for the whole length of a task, then came back describing a screen from minutes ago.
        self._lock = threading.RLock()      # driving the Mac
        self._slot = threading.Lock()       # the observe → act hand-off, held for an instant
        # Waiting for that turn is the caller's business too. A second `do` used to block on the lock with
        # no id and no position, in whatever order the lock felt like; this is the same one-at-a-time rule
        # with a queue the caller can see, cancel and hand work to without waiting (`submit`).
        self._qlock = threading.Lock()
        self._queue: list[tuple[Task, Any]] = []
        self._done_events: dict[str, threading.Event] = {}
        self._worker: threading.Thread | None = None
        self._running: Task | None = None   # counted in a queue position: "how many ahead of me" includes it
        self.models = AppModels(self.cfg)
        self.skills = Skills(self.cfg)
        self.store = Store(self.cfg)
        self.grants = Grants(self.cfg)
        self.cache["appmodels"] = self.models
        self.cache["skills"] = self.skills
        self._planner: Any = None
        self.last_timing: dict[str, int] = {}
        self.helper.on_reset = self._helper_restarted
        atexit.register(self.close)

    def close(self) -> None:
        """Let go of everything this engine started.

        Nothing called this: `macwork serve` ending left the helper child, Playwright's Chromium and the
        open database behind, and the pseudonym tables of the three long-lived redactors (observe, web,
        learn) grew for the life of the process because only per-task ones were ever collected.
        """
        browser = self.cache.pop("web.browser", None)
        for shut in (getattr(browser, "reset", None), self.store.close, getattr(self.helper, "close", None)):
            try:
                if shut:
                    shut()
            except Exception as exc:  # noqa: BLE001  (shutting down must not raise on the way out)
                log.info("while closing: %s", exc)

    def _helper_restarted(self) -> None:
        """A new helper process means every Accessibility reference handed out by the old one is gone, and pids
        may be reused. Anything keyed on them has to go with it."""
        for key in ("menu.snap", "menubar.snap", "menubar.owners", "ambient", "vision.ocr"):
            self.cache.pop(key, None)
        self._last = None
        log.info("helper restarted: dropped the caches that held its references")

    # ----------------------------------------------------------------- plumbing
    @property
    def decider(self) -> Decider:
        if self._decider is None:
            self._decider = make_decider(self.cfg, self.helper)
        return self._decider

    def _entities(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        """On-device name tagging, memoized across tasks (menus and buttons are the same every time)."""
        cache: dict[str, list[dict[str, Any]]] = self.cache.setdefault("entities", {})
        todo = [t for t in dict.fromkeys(texts) if t not in cache]
        if todo:
            for t, found in zip(todo, self.helper.call("nl.entities", texts=todo)):
                cache[t] = found
            self._trim(cache)
        return [cache[t] for t in texts]

    def _detect(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        """Phone numbers and addresses, from the system's own detector, memoized like the name tagging."""
        cache: dict[str, list[dict[str, Any]]] = self.cache.setdefault("detected", {})
        todo = [t for t in dict.fromkeys(texts) if t not in cache]
        if todo:
            kinds = list(self.cfg.get("redact.detect", doc="privacy") or [])
            for t, found in zip(todo, self.helper.call("nl.detect", texts=todo, kinds=kinds)):
                cache[t] = found
            self._trim(cache)
        return [cache[t] for t in texts]

    def _trim(self, cache: dict[str, Any]) -> None:
        if len(cache) > int(self.cfg.get("redact.entity_cache", 20000, doc="privacy")):
            for k in list(cache)[: len(cache) // 2]:
                cache.pop(k, None)

    def _mac_vocabulary(self) -> list[str]:
        """App names on this Mac: the tagger likes to call them people or companies; they are not personal data."""
        if "privacy.vocab" not in self.cache:
            names: list[str] = []
            for a in installed_apps(self.cfg, self.helper) + self.helper.call("apps.running"):
                names += [a.get("name", ""), a.get("file", ""), str(a.get("bundle_id", ""))]
            self.cache["privacy.vocab"] = [n for n in names if n]
        return self.cache["privacy.vocab"]

    def system(self) -> dict[str, Any]:
        """What this Mac is set to — asked once, and of the Mac. Everything that used to be a fixed locale or
        a fixed pair of OCR languages reads it from here."""
        if "system.locale" not in self.cache:
            try:
                self.cache["system.locale"] = self.helper.call("system.locale")
            except HelperError:
                self.cache["system.locale"] = {}
        return self.cache["system.locale"]

    def redactor(self, key: str) -> Redactor:
        # observe / web / learn are not tasks and never expire, so their pseudonym tables grew for the life
        # of the process. They are started afresh once they get big; a token only has to be stable within
        # one piece of work, and for those three a piece of work is one call.
        keeper = self._redactors.get(key)
        if keeper is not None and key in ("observe", "learn") \
                and len(keeper.table) > int(self.cfg.get("redact.table_limit", 5000, doc="privacy")):
            log.info("pseudonym table for %r restarted at %d entries", key, len(keeper.table))
            self._redactors.pop(key, None)
        if key not in self._redactors:
            self._redactors[key] = Redactor(self.cfg, entities=self._entities, protect=self._mac_vocabulary,
                                            detect=self._detect)
        return self._redactors[key]

    @property
    def gate(self) -> Gate:
        return Gate(self.decider, self.audit)

    def _resolve_app(self, hint: str | dict[str, Any] | None, running: list[dict[str, Any]]) -> dict[str, Any] | None:
        if isinstance(hint, dict) and hint.get("pid"):
            if any(a.get("pid") == hint["pid"] for a in running):
                return hint
            hint = hint.get("bundle_id") or hint.get("name")
        if isinstance(hint, str) and hint.strip():
            h = _norm_name(hint)
            for a in running:
                if h in _names(a):
                    return a
            for a in running:
                if any(h in n for n in _names(a) if n):
                    return a
            inst = self._installed_named(hint)   # "Safari浏览器" vs "Safari": the same bundle under another name
            if inst and inst.get("bundle_id"):
                return next((a for a in running if a.get("bundle_id") == inst["bundle_id"]), None)
            # the Dock, Control Center, the input-method agent: ordinary processes with an Accessibility tree,
            # they simply have no Dock icon, so they are not in the list the apps provider offers
            background = [a for a in self.helper.call("apps.running", all=True) if a not in running]
            return next((a for a in background if h in _names(a)), None) or \
                next((a for a in background if any(h in n for n in _names(a) if n)), None)
            return None  # not running: the engine opens an app the caller named; otherwise the apps provider offers it
        return (self.helper.call("apps.frontmost") or {}).get("app")

    def _installed_named(self, hint: str) -> dict[str, Any] | None:
        h = _norm_name(hint)
        inst = self.cache.get("installed") or installed_apps(self.cfg, self.helper)
        self.cache["installed"] = inst
        return next((a for a in inst if h in _names(a)), None) or next((a for a in inst if any(h in n for n in _names(a) if n)), None)

    def _ctx(self, goal: str, inputs: dict[str, Any], app: Any, key: str) -> Ctx:
        running = self.helper.call("apps.running")
        return Ctx(self.cfg, self.helper, goal=goal, inputs=inputs, app=self._resolve_app(app, running), running=running,
                   cache=self.cache, redactor=self.redactor(key),   # gate is attached only where a decision is needed
                   hands=self.take_hands, task=key)

    # --------------------------------------------------------------- step level
    def observe(self, app: str | None = None, goal: str = "", inputs: dict[str, Any] | None = None, limit: int = 200) -> dict[str, Any]:
        """Reading the screen. Deliberately not behind the lock that serialises driving: looking changes
        nothing, and a caller asking what is on screen should not wait out someone else's task."""
        ctx = self._ctx(goal, inputs or {}, app, "observe")
        obs = observe(ctx)
        affs = [a for a in obs.affordances if not self._denied(a, ctx.app)]   # in provider order: no relevance guessing
        with self._slot:
            self._obs_seq += 1
            for a in affs:   # ids carry which observation they came from: acting on a stale one fails instead of
                a.id = f"o{self._obs_seq}:{a.id}"   # resolving to whatever element now happens to sit at that id
            obs.affordances = affs
            self._last = (ctx, obs)
        return {"app": ctx.app, "window": obs.window, "screen_text": obs.screen_text,
                "affordances": [a.public() for a in affs[:limit]], "total": len(affs), "notes": obs.notes}

    def act(self, affordance_id: str, params: dict[str, Any] | None = None, confirm: bool = False,
            remember: bool = False) -> dict[str, Any]:
        with self._lock:      # acting does drive the Mac, so it waits its turn like a task does
            if not self._last:
                return {"ok": False, "error": "call observe first"}
            seen, obs = self._last
            a = obs.by_id().get(affordance_id)
            if a is None:
                stale = ":" in affordance_id and not affordance_id.startswith(f"o{self._obs_seq}:")
                return {"ok": False, "error": f"{affordance_id} is from an earlier observation: observe again" if stale
                        else f"no affordance {affordance_id} in the last observation"}
            # the observation's goal and inputs still apply, but which app is in front and what is running
            # may have changed since it was taken
            ctx = self._ctx(seen.goal, seen.inputs, obs.app, "observe")
            # Two channels are not executors: the loop reads them and decides again rather than handing them
            # to `act`. At the step level there is no loop, so they were offered by `observe` and then came
            # back "no channel 'vision'" — an option that cannot be taken is not an option.
            if a.channel == "vision":
                ctx.cache.setdefault("vision.wanted", {}).setdefault("observe", set()).add(a.target["key"])
                self._last = None
                return {"ok": True, "observe_again": True,
                        "note": "this window will be read from the screen on the next observe"}
            if a.channel == "skill":
                return {"ok": False, "error": "a learned routine is a whole task, not a step: run it with mac_do",
                        "affordance": a.public()}
            # The gate is built only where a decision is actually needed. Building it unconditionally meant
            # `classify_step_level: false` still constructed a decider — so the switch did nothing, and the
            # step level, which is documented as "the caller drives", could not be used without an API key.
            classify = (self.cfg.policy.get("confirm") or {}).get("classify_step_level", True)
            if classify:
                ctx.gate = self.gate
            floor = self._floor("step", ctx, a, obs.window) if classify else None
            if self._needs_confirm(a, 0.0, set(), floor=floor):
                granted, offer = self.standing("step", ctx, a, floor if floor is not None else self._floor_hits(a))
                if not (granted or confirm):
                    return {"ok": False, "needs_confirm": True, "affordance": a.public(), "because": floor,
                            "hint": "call act again with confirm=true", **({"remember": offer} if offer else {})}
                if confirm and remember and offer and not granted:
                    self.grants.add(offer, "caller")
                    self.audit.record("grant", task="step", id=offer["id"], app=offer.get("bundle_id"),
                                      action=offer.get("action"), because=offer.get("because"), source="caller")
            missing = [k for k, s in a.slots.items() if s.required and k not in (params or {})]
            if missing:
                return {"ok": False, "needs_input": missing, "affordance": a.public()}
            out, events = self._execute(ctx, a, params or {})
            self._last = None  # refs may be stale now: observe again
            return {"ok": out.ok, "error": out.error, "events": events, "output": out.output, "now_in": out.target}

    # --------------------------------------------------------------- goal level
    def do(self, goal: str, inputs: dict[str, Any] | None = None, app: str | None = None, progress: Progress | None = None) -> dict[str, Any]:
        """Run a task and wait for it. It waits its turn in the queue like any other; `submit` is the same
        thing without the waiting."""
        task = self._new_task(goal, inputs, app)
        if hasattr(self.decider, "warm"):
            threading.Thread(target=self.decider.warm, name="decider-warm", daemon=True).start()
        backend = self.planning_backend if self.cfg.get("planner.warm_on_task_start", True) else None
        if backend is not None and getattr(backend, "local", False) and hasattr(backend, "warm"):
            threading.Thread(target=backend.warm, name="planner-warm", daemon=True).start()   # never blocks the task
        return self._run_queued(task, progress, None)

    def allow(self, task_id: str, source: str = "cli") -> dict[str, Any]:
        """Remember the confirmation a task is waiting for (or was), as a standing grant."""
        task = self.tasks.get(task_id) or self._recall(task_id)
        offer = (task.pending or {}).get("remember") if task else None
        if not offer:
            return {"ok": False, "error": "unknown task" if task is None else
                    "this task is not waiting on a confirmation that may be remembered"}
        row = self.grants.add(offer, source)
        self.audit.record("grant", task=task_id, id=row["id"], app=row.get("bundle_id"), action=row.get("action"),
                          because=row.get("because"), source=source)
        return {"ok": True, "grant": {k: row.get(k) for k in ("id", "app", "bundle_id", "action", "because")}}

    def resume(self, task_id: str, inputs: dict[str, Any] | None = None, confirm: bool | None = None,
               choice_id: str | None = None, progress: Progress | None = None, remember: bool = False) -> dict[str, Any]:
        self._gc()
        task = self.tasks.get(task_id) or self._recall(task_id)
        if task is None:
            return {"task_id": task_id, "status": "failed", "reason": "unknown or expired task"}
        if task.status not in PENDING:
            return task.result()
        task.inputs.update(inputs or {})
        held: Affordance | None = task.held
        if task.status == "need_confirm":
            if not confirm:
                task.status, task.reason = "cancelled", "the caller declined"
                return task.result()
            if held:
                self.resume_approval(task)
            if remember:          # "yes, and do not ask me about this one again"
                task.outputs["remembered"] = self.allow(task.id, "caller")
        if task.status == "ambiguous":
            held = task.options.get(choice_id or "")
            if held is None:
                return {**task.result(), "error": f"choose one of {list(task.options)}"}
            # Which one and whether it is safe are two questions, and picking an option answers only the
            # first — so a chosen action still meets the floor. A caller that already knows can answer both
            # at once by passing `confirm` with the choice, rather than being asked twice in a row.
            if confirm:
                self.approve(task, held)
        task.status, task.pending, task.options = "running", {}, {}
        task.held = held
        return self._run_queued(task, progress, None)

    def _recall(self, task_id: str) -> Task | None:
        """A task the engine does not remember may still be on disk: a restart is not an expiry.

        What comes back is the task's own state. Pids, window numbers and element references are left behind
        deliberately — they described a machine that has moved on — so a resumed task looks at the screen
        again, which is what it would do anyway.
        """
        task = self.store.get(task_id)
        if task is None:
            return None
        log.info("task %s picked up again after a restart", task_id)
        task.outputs.setdefault("resumed_after_restart", True)
        self.tasks[task_id] = task
        return task

    def feedback(self, task_id: str, ok: bool, note: str = "") -> dict[str, Any]:
        """Ground truth from the caller (or an eval check): a task judged done that was not, must not become a routine."""
        task = self.tasks.get(task_id)
        if task is None:
            return {"task_id": task_id, "error": "unknown or expired task"}
        routine = task.outputs.get("learned_routine")
        if not ok and routine:
            (self.skills.dir / f"{routine}.json").unlink(missing_ok=True)
            task.outputs.pop("learned_routine", None)
        for r in task.outputs.get("routines", []):
            if r.get("id"):
                self.skills.bump(r["id"], ok)
        self.audit.record("feedback", task=task_id, ok=ok, note=note)
        return {"task_id": task_id, "ok": ok, "routine_removed": bool(not ok and routine)}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id) or self._recall(task_id)
        if task is None:   # saying "cancelled" for an id nobody knows only hides a typo or an expired task
            return {"task_id": task_id, "cancelled": False, "error": "unknown or expired task"}
        self._cancelled.add(task_id)
        dropped = self._drop_from_queue(task_id)
        if task.status in PENDING or task.status == "queued" or dropped:
            task.status = "cancelled"
        if dropped:                       # it never started, so nobody is waiting on the loop to notice
            self._done_events.pop(task_id, threading.Event()).set()
        return {"task_id": task_id, "cancelled": True, "was_queued": dropped}

    def _gc(self) -> None:
        ttl = float(self.cfg.get("engine.tasks_ttl_s", 1800))
        for tid, t in list(self.tasks.items()):
            if time.time() - t.updated > ttl:
                self.tasks.pop(tid, None)
                self._redactors.pop(tid, None)
                self._cancelled.discard(tid)   # else the set grows for the life of the process
        self.store.sweep()

    def exclusive(self) -> bool:
        """Does this run own the Mac, or does it belong to the user (the default)?"""
        return str(self.cfg.get("engine.mode", "yield")) in ("exclusive", "background")

    def take_hands(self, wait_s: float | None = None, cancelled: Callable[[], bool] | None = None) -> bool:
        """The Mac has one keyboard and one front app. Outside an exclusive run the engine may use them only
        while the user is not: it waits for their keyboard and mouse to be quiet, and gives up its turn if they
        keep working. Actions that never touch the front app do not call this.

        ``input.idle`` reports the time since the *user* last did something; the helper keeps the engine's own
        keystrokes and clicks out of that figure, which the raw system reading cannot do (measured: one
        synthetic mouse move takes it from 25.97 s to 0.15 s).
        """
        if self.exclusive():
            return True
        need = float(self.cfg.get("engine.yield_idle_s", 1.5))
        deadline = time.monotonic() + (float(self.cfg.get("engine.yield_max_s", 60)) if wait_s is None else wait_s)
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                return False
            try:
                if float(self.helper.call("input.idle").get("idle_s", 99)) >= need:
                    return True
            except HelperError:
                return True
            time.sleep(0.25)
        return False

    def _yield_to_user(self, task: Task, deadline: float) -> None:
        """Before each step: wait for the user to stop, but never longer than one turn's share of the budget."""
        wait = min(float(self.cfg.get("engine.yield_max_s", 60)), max(0.0, deadline - time.monotonic()))
        self.take_hands(wait_s=wait, cancelled=lambda: task.id in self._cancelled)

    def _finish(self, task: Task, status: str, reason: str = "", pending: dict[str, Any] | None = None) -> dict[str, Any]:
        answered_anyway = False
        if status in ("done", "failed", "cancelled", "blocked", "need_continue"):
            self._answer_before_ending(task, None)   # a question asked is owed whatever the task found out
            # A goal that asks a question is done when the question is answered. One asked which item cost
            # the most read the file, answered it correctly — "金额最高的是显示器，金额为1790。" — and still
            # reported `failed`, because it had gone on to flail in the app's sort options. The caller is not
            # served by "failed" with the right answer attached. The reason it gave itself is kept, and what
            # it did is not learned as a routine: the answer was right, the way it went about it was not.
            if status in ("failed", "need_continue") and task.outputs.get("answer"):
                task.outputs["unfinished_but_answered"] = reason
                status, reason, answered_anyway = "done", "the question is answered from what the task saw", ""
        task.status, task.reason, task.pending = status, reason, pending or {}
        task.updated = time.time()
        self.store.save(task)
        if status == "done" and not answered_anyway:
            path = self.skills.record(task, task.start_app)
            if path:
                task.outputs["learned_routine"] = path.stem
        for app in task.apps.values():
            self.models.save(app)
        self.audit.record("task", task=task.id, status=status, reason=reason, steps=len(task.steps))
        if status in ("failed", "cancelled") and task.changed and self.cfg.get("engine.revert.on_failure", False):
            # Before tidy, not after: putting a change back is done through the app's own undo command, and
            # tidy closes the windows that command lives in. `revert` refuses on its own terms — it will not
            # undo while someone has been using the Mac, and it stops at the first change it cannot put back.
            task.outputs["revert"] = self.revert(task.id)
        if status in ("done", "failed", "cancelled") and self.cfg.get("engine.tidy", "auto") == "auto":
            # pending and blocked tasks keep everything as it is: the user or caller continues there
            if self.cfg.get("engine.tidy_background", True):   # the result goes back now; closing apps can follow
                task.outputs["tidy"] = "closing what the task opened, in the background (see mac_status)"
                threading.Thread(target=self._tidy_later, args=(task.id,), name="tidy", daemon=True).start()
            else:
                self.tidy(task.id)
        return task.result()

    # ----------------------------------------------------------------- the queue
    def submit(self, goal: str, inputs: dict[str, Any] | None = None, app: str | None = None) -> dict[str, Any]:
        """Hand in work and get its id back now, rather than waiting for the Mac to be free.

        The Mac has one keyboard and one front app, so tasks run one at a time; that part was already true.
        What was missing is a queue in front of it. A second caller simply blocked on a lock: no id, no
        position, no way to change its mind, and — since a Python lock is not first-come-first-served — no
        promise about what ran next. An MCP client that called `mac_do` twice just stopped responding.

        Nothing is run in parallel here and nothing should be. This is about *waiting* being something the
        caller can see and cancel.
        """
        task = self._new_task(goal, inputs, app)
        with self._qlock:
            self._queue.append((task, None))
            position = len(self._queue) + (1 if self._running is not None else 0)
            self._ensure_worker()
        task.status = "queued"
        return {"task_id": task.id, "status": "queued", "position": position, "goal": task.goal}

    def _new_task(self, goal: str, inputs: dict[str, Any] | None, app: str | None) -> Task:
        task = Task(goal=goal.strip(), inputs=dict(inputs or {}), app=app)
        task.memory.facts = Facts(inputs=task.inputs, goal=task.goal, limit=int(self.cfg.get("engine.carry_chars", 600)))
        self.tasks[task.id] = task
        self._gc()
        return task

    def _ensure_worker(self) -> None:
        """Started on the first queued task, not at construction: an engine used for one `observe` should not
        leave a thread behind."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._serve_queue, name="macwork-queue", daemon=True)
            self._worker.start()

    def _serve_queue(self) -> None:
        while True:
            with self._qlock:
                if not self._queue:
                    self._worker = None
                    return
                task, progress = self._queue.pop(0)
            if task.id in self._cancelled or task.status == "cancelled":
                task.status = "cancelled"
                self._done_events.pop(task.id, threading.Event()).set()
                continue
            self._running = task
            try:
                self._run(task, progress)
            except Exception:                    # noqa: BLE001  (one task must not take the queue down)
                log.exception("task %s failed outside the loop", task.id)
                task.status, task.reason = "failed", "the engine itself failed; see the log"
            finally:
                self._running = None
                self._done_events.pop(task.id, threading.Event()).set()

    def queue(self) -> list[dict[str, Any]]:
        """What is running and what is waiting, in order. Position 1 is whatever has the Mac right now."""
        with self._qlock:
            running = self._running
            out = ([{"task_id": running.id, "goal": running.goal, "position": 1, "running": True}]
                   if running is not None else [])
            return out + [{"task_id": t.id, "goal": t.goal, "position": i + 1 + len(out)}
                          for i, (t, _) in enumerate(self._queue)]

    def _drop_from_queue(self, task_id: str) -> bool:
        with self._qlock:
            before = len(self._queue)
            self._queue = [(t, pr) for t, pr in self._queue if t.id != task_id]
            return len(self._queue) < before

    def _run_queued(self, task: Task, progress: Progress | None, timeout: float | None) -> dict[str, Any]:
        """Put a task in the queue and wait for it: what `do` and `resume` do, so one caller cannot jump
        ahead of another by calling a different method."""
        done = threading.Event()
        with self._qlock:
            self._done_events[task.id] = done
            self._queue.append((task, progress))
            waiting = len(self._queue) > 1
            self._ensure_worker()
        if waiting:
            task.status = "queued"
        if not done.wait(timeout if timeout is not None else float(self.cfg.get("engine.queue_wait_s", 900))):
            self._drop_from_queue(task.id)
            return {**task.result(), "status": "failed",
                    "reason": "gave up waiting for the Mac to be free (engine.queue_wait_s)"}
        return task.result()

    def _run(self, task: Task, progress: Progress | None) -> dict[str, Any]:
        with self._lock:
            d = self._decider
            calls0, cost0 = (d.calls, d.cost_usd) if d else (0, 0.0)
            task.begin_run(calls0, cost0)   # steps and seconds are budgeted per turn, not across the caller's pauses
            try:
                self._loop(task, progress or (lambda _m: None))
            except RedactionError as exc:
                # deciding on "[withheld]" everywhere is deciding blind: stop rather than degrade quietly
                self._finish(task, "failed", f"nothing could be sent to the decider: {exc}")
            finally:  # everything this run asked the decider, web research included
                task.end_run()
                if self._decider is not None:
                    task.decider_calls += self._decider.calls - calls0
                    task.cost_usd += self._decider.cost_usd - cost0
            return task.result()

    def busy(self) -> bool:
        """Is something driving the Mac right now? Answering without waiting is the point."""
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return False
        return True

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"config": str(self.cfg.get("helper.mode"))}
        try:
            ping = self.helper.call("ping", timeout=5)
            out["helper"] = {k: ping.get(k) for k in ("version", "ax_trusted", "screen_capture", "secure_input")} | {"mode": self.helper.mode}
        except HelperError as exc:
            out["helper"] = {"error": str(exc)}
        try:
            d = self.decider
            # what it resolved to, not what was asked for: with `auto` those differ, and which one is
            # answering changes how much its confidence is worth
            out["decider"] = {"kind": self.cfg.get("decider.kind"), "using": getattr(d, "name", None),
                              "model": self.cfg.get("decider.model"),
                              "calls": d.calls, "cost_usd": round(d.cost_usd, 6)}
        except DeciderError as exc:
            out["decider"] = {"error": str(exc)}
        backend = self.planning_backend
        out["planner"] = {"kind": self.cfg.get("planner.kind"), "using": getattr(backend, "name", None) if backend else None,
                          "model": getattr(backend, "model", None) if backend else None}
        out["skills"] = len(self.skills.all())
        out["busy"] = self.busy()
        out["queue"] = self.queue()
        out["tasks"] = {"in_memory": len(self.tasks), "kept": bool(self.store.enabled)}
        out["dry_run"] = bool(self.cfg.get("audit.dry_run"))
        return out
