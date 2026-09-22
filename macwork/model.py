"""The one vocabulary every part speaks: what can be seen (Observation) and what can be done (Affordance)."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Slot:
    """A parameter an affordance needs. ``text`` slots can only be filled by the caller (Jev writes no text)."""

    kind: str                      # text | bool | number
    desc: str
    required: bool = True


def with_state(name: str, state: str = "") -> str:
    """An action's name with what it shows right now: 「type into Search (now: hello)」, 「select row (selected)」.

    The decider should see the state; nothing else should. What a field holds changes with every keystroke, so
    anything *keyed* on the full label — a floor verdict, a standing grant, a learned routine — saw a new
    action each time. Three modules each grew their own regex to strip the state back off, and the three did
    not agree on what to strip. It is put on here and taken off in `steady`, and nowhere else.
    """
    return f"{name} ({state})" if state else name


_STATE = re.compile(r"\s*\((?:now: [^)]*|selected)\)")


def steady(label: str) -> str:
    """The label without its momentary state — the inverse of `with_state`, for labels that arrive as strings
    (a recorded step, a stored routine). An `Affordance` knows its own: `a.name()`."""
    return _STATE.sub("", label or "").strip()


@dataclass
class Affordance:
    id: str                        # unique within one observation
    channel: str                   # which executor runs it: app | menu | window | keys | shortcut | file | web | …
    verb: str                      # press | open | activate | type | key | run | research | …
    label: str                     # what the decider reads
    target: dict[str, Any] = field(default_factory=dict)   # executor-specific (refs, pids, paths); never sent out
    slots: dict[str, Slot] = field(default_factory=dict)
    context: str = ""              # where it lives (app ▸ menu path, window)
    key: str = ""                  # language-independent identity, where the Mac gives one (see `identity`)
    yields: str = ""               # for an action that only *returns* something (a file's text, a window's): what
                                   # exactly it would return, as an identity that changes when the source does.
                                   # Once a task holds that, the action is complete and is not offered again

    def name(self) -> str:
        """What this action is called, without what it happens to show right now."""
        return steady(self.label)

    def identity(self) -> str:
        """What this action *is*, as independently of the interface language as the Mac allows.

        Labels are the app's own words, so everything keyed on them — a screen's signature, a learned
        routine's steps — breaks the moment the Mac's language changes. Menu items almost always carry an
        Accessibility identifier (the selector behind the command: `_systemSettingsRequested:`), which is the
        same in every language and stable across launches. Window controls mostly carry none, and there the
        label is the only identity there is; that is a limit of what the Mac exposes, not a choice.
        """
        return self.key or self.label

    def describe(self) -> str:
        text = f"{self.label}" + (f" — in {self.context}" if self.context and self.context not in self.label else "")
        if self.slots:
            text += " [needs: " + ", ".join(f"{k} ({s.desc})" for k, s in self.slots.items()) + "]"
        return text

    def public(self) -> dict[str, Any]:
        d = {"id": self.id, "channel": self.channel, "verb": self.verb, "label": self.label}
        if self.context:
            d["context"] = self.context
        if self.slots:
            d["slots"] = {k: asdict(s) for k, s in self.slots.items()}
        return d


@dataclass
class Observation:
    app: dict[str, Any] | None                   # target app (pid, name, bundle_id)
    window: str | None
    affordances: list[Affordance]
    screen_text: str = ""                        # what is readable on screen (for the decider's judgement)
    focused: dict[str, Any] | None = None
    notes: dict[str, Any] = field(default_factory=dict)   # provider diagnostics (timings, truncation)
    at: float = field(default_factory=time.time)

    def by_id(self) -> dict[str, Affordance]:
        return {a.id: a for a in self.affordances}


@dataclass
class Step:
    n: int
    action: str                                  # affordance label (or a status note)
    affordance_id: str | None = None
    ok: bool = True
    events: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
    ms: int = 0
    error: str | None = None
    channel: str = ""                            # what it was, by meaning (for skills and the app model)
    verb: str = ""
    context: str = ""
    slot_keys: list[str] = field(default_factory=list)
    before: str | None = None                    # screen state the step was taken from (signature + text digest)
    key: str = ""                                # the affordance's language-independent identity, if it had one
    outcome: str = ""                            # what it did, once the next look has shown it: "「12×」 became
                                                 # 「12×2」". "ok" only ever meant the action was carried out
    produced: bool = False                       # it handed something back — a file's text, a window read in
                                                 # full, what a Shortcut returned. Such a step raises no
                                                 # Accessibility event, and "did anything happen" used to
                                                 # be a count of those alone
    picture: float | None = None                 # share of the window's picture that changed across this step
                                                 # (sight.py); None when it could not be measured
    unseen: bool = False                         # what it does may not show in anything the engine observes (a
                                                 # key held in a view that draws itself). "The screen looks the
                                                 # same" is then not evidence of anything, and nothing that
                                                 # concludes from it may count this step
    effect: str = ""                             # the floor's category for it: navigate | enter | delete | send | …
                                                 # "" = nobody judged it. This is what `revert` reads to know
                                                 # which steps changed something, rather than judging again


TERMINAL = {"done", "failed", "cancelled"}
# need_continue: this run's share of steps or seconds is gone, but the task is getting somewhere and has
# not touched its whole-task ceiling. Everything it learned is kept; mac_resume picks it up.
PENDING = {"need_input", "need_confirm", "ambiguous", "need_continue"}
# plus "blocked": terminal for the engine — something only the user can do (sign in, password, permission) stands in the way



@dataclass
class Pace:
    """How much of each kind of move this task has already made (the engine's own budgets)."""
    replans: int = 0
    corrections: int = 0                                       # replans that followed a step judged to have gone wrong
    looks: int = 0                                             # how many times it did (budget: engine.max_looks)
    launched: bool = False                                     # the app the caller named was opened by the engine
    tidied: bool = False
    waits: int = 0                                             # times the decider chose to wait (budget: engine.max_waits)
    settled_leave: bool = False                                # the app was let finish before the task left it
    settled_done: bool = False                                 # the screen was let settle before judging "done"
    redo: int = 0                                              # decisions discarded because the screen moved meanwhile
    fruitless: int = 0                                         # "rethink" asked with no new route to be had
    answer_tries: int = 0                                      # times a goal asking for information ended with none
    done_opinions: int = 0                                     # second opinions asked on "done" (budget: planner.max_done_opinions)


@dataclass
class Memory:
    """What this run learned as it went: what changed nothing, what was skipped, which screens it saw, and the
    facts it may write from."""
    consulted: set[Any] = field(default_factory=set)           # screens the planner has already been asked about
    interruptions: dict[str, dict[str, Any]] = field(default_factory=dict)   # what each thing in the way turned out to be
    no_effect: set[str] = field(default_factory=set)           # "screen|action" that changed nothing: never offered again
    expanded: set[str] = field(default_factory=set)            # option groups the decider chose to look into
    screens_seen: set[str] = field(default_factory=set)
    screen_notes: dict[str, str] = field(default_factory=dict)  # distinct screens seen (signature -> short text)
    circles: list[str] = field(default_factory=list)           # actions that only led back to a screen already seen
    facts: Any = None                                          # facts.Facts: what this task has actually seen
    serves: dict[str, bool] = field(default_factory=dict)      # "does leaving for X serve the goal", asked once per action
    declined: set[str] = field(default_factory=set)            # floor actions the decider judged the goal never asked for
    retracted: dict[str, list[str]] = field(default_factory=dict)   # exact state -> choices made from it that the
                                                               # task then came back from. A System-1 model gives
                                                               # the same answer to the same state, so without
                                                               # this it makes the same mistake from the same
                                                               # screen until it runs out of steps
    failed: dict[str, str] = field(default_factory=dict)       # "screen|action" that could not be carried out ->
                                                               # the exact state it failed in. Not `no_effect`:
                                                               # "it could not be done just then" is not "it
                                                               # does nothing", and only the second is a fact
                                                               # about the action
    yielded: set[str] = field(default_factory=set)             # `Affordance.yields` of what this task already got
    retracted_at: int = -1                                     # len(steps) when the last one was noted (a look can repeat)


@dataclass
class Desktop:
    """The desktop as the task found it, and what it opened — so tidying touches only its own doing."""
    initial_pids: set[int] | None = None                       # apps running when the task began: never closed by it
    initial_windows: dict[int, set[int]] | None = None         # windows on screen when it began (pid -> window numbers)
    opened: dict[int, dict[str, Any]] = field(default_factory=dict)   # apps the task opened (pid -> info)


@dataclass
class Task:
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    app: str | None = None                       # optional app to work in (name or bundle id)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"                      # running | done | failed | cancelled | need_input | need_confirm
                                                 #  | ambiguous | need_continue
    steps: list[Step] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)   # what the caller must answer to resume
    outputs: dict[str, Any] = field(default_factory=dict)   # results worth returning (web notes, files found…)
    approved: set[str] = field(default_factory=set)          # affordance labels the caller confirmed
    target: dict[str, Any] | None = None                     # app currently worked in (pid, name, bundle_id)
    held: Any = None                                         # the affordance waiting on the caller (need_input/confirm)
    confirm_key: str = ""                                    # which identity a "yes" approves, when that is
                                                 # not the held action's own: the text re-judge asks about
                                                 # the action *with its text in it*, and approving the bare
                                                 # action would leave the caller confirming forever
    options: dict[str, Any] = field(default_factory=dict)    # affordances offered when ambiguous, by id
    plan: list[str] | None = None                            # sub-goals from the planner, if one was consulted
    plan_i: int = 0
    prev: dict[str, Any] | None = None                       # last step, waiting to learn where it led
    apps: dict[str, Any] = field(default_factory=dict)       # apps touched (bundle -> info), for saving their models
    changed: list[dict[str, Any]] = field(default_factory=list)   # steps that altered something, newest last:
                                                 # what `revert` works back through. Judged by the safety
                                                 # floor, which classifies every action anyway
    start_app: str | None = None
    tries: list[dict[str, str]] = field(default_factory=list)   # moves the planner suggested (offered, not forced)
    started_wall: float = field(default_factory=time.time)
    wants_answer: float | None = None                          # the goal asks for information to be reported back
    blocked_reason: str = ""                                   # set by the planner when only the user can unblock
    pace: Pace = field(default_factory=Pace)
    memory: Memory = field(default_factory=Memory)
    desktop: Desktop = field(default_factory=Desktop)
    started: float = field(default_factory=time.monotonic)
    updated: float = field(default_factory=time.time)
    reason: str = ""
    cause: str = ""                                            # why it ended, for a program: budget |
                                                 # screen_locked | decider_unreachable | redaction_failed |
                                                 # engine_error.
                                                 # `reason` is for a person; the eval harness read it for
                                                 # phrases to tell "the service was down" from "it failed"
    decider_calls: int = 0
    cost_usd: float = 0.0
    # One *run* is one uninterrupted turn of the loop: do(), or a resume() after the caller answered. The step
    # and time budgets are per run, because the caller's thinking time is not the task's — a task that waited
    # two minutes for a "yes" must not come back already out of budget. What bounds a task over all its runs
    # are the `total_` ceilings, checked against `started` and `steps` as a whole.
    run_started: float = field(default_factory=time.monotonic)
    run_step0: int = 0                                         # len(steps) when this run began
    spent_s: float = 0.0                                       # working time of the runs that are over
    run_calls0: int = 0                                        # the decider's call count when this run began
    run_cost0: float = 0.0

    def begin_run(self, calls: int, cost: float) -> None:
        self.run_started, self.run_step0, self.run_calls0, self.run_cost0 = time.monotonic(), len(self.steps), calls, cost
        # The counts in `pace` bound loops inside a run — how many times to look into a group, wait, ask
        # the planner again — and were never reset, while steps and seconds were. A task handed back with
        # `need_continue` three times came back with fresh steps and no replan left. The flags stay: they
        # record things that happened to the task, not how much of a run's allowance is spent.
        self.pace = Pace(launched=self.pace.launched, tidied=self.pace.tidied,
                         settled_leave=self.pace.settled_leave, settled_done=self.pace.settled_done)

    def end_run(self) -> None:
        self.spent_s += time.monotonic() - self.run_started

    @property
    def working_s(self) -> float:
        """Time actually spent working, waiting for the caller excluded: what the total budget is about."""
        return self.spent_s + (time.monotonic() - self.run_started)

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {"task_id": self.id, "status": self.status, "goal": self.goal,
                               "steps": [s.action + ("" if s.ok else " (failed)") for s in self.steps],
                               "seconds": round(time.monotonic() - self.started, 1),
                               "decider": {"calls": self.decider_calls, "cost_usd": round(self.cost_usd, 6)}}
        if self.reason:
            out["reason"] = self.reason
        if self.cause:
            out["cause"] = self.cause
        if self.pending:
            out["pending"] = self.pending
        if self.outputs:
            out["outputs"] = self.outputs
        if self.plan:
            out["plan"] = {"steps": self.plan, "at": min(self.plan_i, len(self.plan)), "replans": self.pace.replans}
        return out
