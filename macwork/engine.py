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
from .helper import Helper, HelperError
from .judge import MOVES, JudgeMixin
from .learn import LearnMixin
from .loop import LoopMixin, Progress
from .policy import PolicyMixin
from .model import PENDING, Affordance, Observation, Task
from .observe import Ctx, installed_apps, observe
from .privacy import Audit, Gate, RedactionError, Redactor
from .skills import Skills
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


class Engine(LoopMixin, EffectsMixin, JudgeMixin, PolicyMixin, ConsultMixin, TidyMixin, LearnMixin):
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
        self._lock = threading.RLock()
        self.models = AppModels(self.cfg)
        self.skills = Skills(self.cfg)
        self.cache["appmodels"] = self.models
        self.cache["skills"] = self.skills
        self._planner: Any = None
        self.last_timing: dict[str, int] = {}
        self.helper.on_reset = self._helper_restarted

    def _helper_restarted(self) -> None:
        """A new helper process means every Accessibility reference handed out by the old one is gone, and pids
        may be reused. Anything keyed on them has to go with it."""
        for key in ("menu.snap", "menubar.snap", "menubar.owners", "ambient", "vision.ocr", "vision.wanted"):
            self.cache.pop(key, None)
        self._last = None
        log.info("helper restarted: dropped the caches that held its references")

    # ----------------------------------------------------------------- plumbing
    @property
    def decider(self) -> Decider:
        if self._decider is None:
            self._decider = make_decider(self.cfg)
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
                   hands=self.take_hands)

    # --------------------------------------------------------------- step level
    def observe(self, app: str | None = None, goal: str = "", inputs: dict[str, Any] | None = None, limit: int = 200) -> dict[str, Any]:
        with self._lock:
            ctx = self._ctx(goal, inputs or {}, app, "observe")
            obs = observe(ctx)
            affs = [a for a in obs.affordances if not self._denied(a, ctx.app)]   # in provider order: no relevance guessing
            self._obs_seq += 1
            for a in affs:   # ids carry which observation they came from: acting on a stale one fails instead of
                a.id = f"o{self._obs_seq}:{a.id}"   # resolving to whatever element now happens to sit at that id
            obs.affordances = affs
            self._last = (ctx, obs)
            return {"app": ctx.app, "window": obs.window, "screen_text": obs.screen_text,
                    "affordances": [a.public() for a in affs[:limit]], "total": len(affs), "notes": obs.notes}

    def act(self, affordance_id: str, params: dict[str, Any] | None = None, confirm: bool = False) -> dict[str, Any]:
        with self._lock:
            if not self._last:
                return {"ok": False, "error": "call observe first"}
            seen, obs = self._last
            a = obs.by_id().get(affordance_id)
            if a is None:
                stale = ":" in affordance_id and not affordance_id.startswith(f"o{self._obs_seq}:")
                return {"ok": False, "error": f"{affordance_id} is from an earlier observation: observe again" if stale
                        else f"no affordance {affordance_id} in the last observation"}
            # the observation's goal and inputs still apply (the web channel reads them), but which app is in
            # front and what is running may have changed since it was taken
            ctx = self._ctx(seen.goal, seen.inputs, obs.app, "observe")
            ctx.gate = self.gate      # for the floor classification, and for the web channel below
            classify = (self.cfg.policy.get("confirm") or {}).get("classify_step_level", True)
            floor = self._floor("step", ctx, a, obs.window) if classify else None
            if self._needs_confirm(a, 0.0, set(), floor=floor) and not confirm:
                return {"ok": False, "needs_confirm": True, "affordance": a.public(), "because": floor,
                        "hint": "call act again with confirm=true"}
            missing = [k for k, s in a.slots.items() if s.required and k not in (params or {})]
            if missing:
                return {"ok": False, "needs_input": missing, "affordance": a.public()}
            out, events = self._execute(ctx, a, params or {})
            self._last = None  # refs may be stale now: observe again
            return {"ok": out.ok, "error": out.error, "events": events, "output": out.output, "now_in": out.target}

    # --------------------------------------------------------------- goal level
    def do(self, goal: str, inputs: dict[str, Any] | None = None, app: str | None = None, progress: Progress | None = None) -> dict[str, Any]:
        task = Task(goal=goal.strip(), inputs=dict(inputs or {}), app=app)
        task.memory.facts = Facts(inputs=task.inputs, goal=task.goal, limit=int(self.cfg.get("engine.carry_chars", 600)))
        if hasattr(self.decider, "warm"):
            threading.Thread(target=self.decider.warm, name="decider-warm", daemon=True).start()
        backend = self.planning_backend if self.cfg.get("planner.warm_on_task_start", True) else None
        if backend is not None and getattr(backend, "local", False) and hasattr(backend, "warm"):
            threading.Thread(target=backend.warm, name="planner-warm", daemon=True).start()   # never blocks the task
        self.tasks[task.id] = task
        self._gc()
        return self._run(task, progress)

    def resume(self, task_id: str, inputs: dict[str, Any] | None = None, confirm: bool | None = None,
               choice_id: str | None = None, progress: Progress | None = None) -> dict[str, Any]:
        self._gc()
        task = self.tasks.get(task_id)
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
                task.approved.add(held.label)
        if task.status == "ambiguous":
            held = task.options.get(choice_id or "")
            if held is None:
                return {**task.result(), "error": f"choose one of {list(task.options)}"}
        task.status, task.pending, task.options = "running", {}, {}
        task.held = held
        return self._run(task, progress)

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
        task = self.tasks.get(task_id)
        if task is None:   # saying "cancelled" for an id nobody knows only hides a typo or an expired task
            return {"task_id": task_id, "cancelled": False, "error": "unknown or expired task"}
        self._cancelled.add(task_id)
        if task.status in PENDING:
            task.status = "cancelled"
        return {"task_id": task_id, "cancelled": True}

    def _gc(self) -> None:
        ttl = float(self.cfg.get("engine.tasks_ttl_s", 1800))
        for tid, t in list(self.tasks.items()):
            if time.time() - t.updated > ttl:
                self.tasks.pop(tid, None)
                self._redactors.pop(tid, None)
                self._cancelled.discard(tid)   # else the set grows for the life of the process

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
        task.status, task.reason, task.pending = status, reason, pending or {}
        task.updated = time.time()
        if status == "done":
            path = self.skills.record(task, task.start_app)
            if path:
                task.outputs["learned_routine"] = path.stem
        for app in task.apps.values():
            self.models.save(app)
        self.audit.record("task", task=task.id, status=status, reason=reason, steps=len(task.steps))
        if status in ("done", "failed", "cancelled") and self.cfg.get("engine.tidy", "auto") == "auto":
            # pending and blocked tasks keep everything as it is: the user or caller continues there
            if self.cfg.get("engine.tidy_background", True):   # the result goes back now; closing apps can follow
                task.outputs["tidy"] = "closing what the task opened, in the background (see mac_status)"
                threading.Thread(target=self._tidy_later, args=(task.id,), name="tidy", daemon=True).start()
            else:
                self.tidy(task.id)
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

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"config": str(self.cfg.get("helper.mode"))}
        try:
            ping = self.helper.call("ping", timeout=5)
            out["helper"] = {k: ping.get(k) for k in ("version", "ax_trusted", "screen_capture", "secure_input")} | {"mode": self.helper.mode}
        except HelperError as exc:
            out["helper"] = {"error": str(exc)}
        try:
            d = self.decider
            out["decider"] = {"kind": self.cfg.get("decider.kind"), "model": self.cfg.get("decider.model"),
                              "calls": d.calls, "cost_usd": round(d.cost_usd, 6)}
        except DeciderError as exc:
            out["decider"] = {"error": str(exc)}
        backend = self.planning_backend
        out["planner"] = {"kind": self.cfg.get("planner.kind"), "using": getattr(backend, "name", None) if backend else None,
                          "model": getattr(backend, "model", None) if backend else None}
        out["skills"] = len(self.skills.all())
        out["dry_run"] = bool(self.cfg.get("audit.dry_run"))
        return out
