"""The one vocabulary every part speaks: what can be seen (Observation) and what can be done (Affordance)."""

from __future__ import annotations

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


@dataclass
class Affordance:
    id: str                        # unique within one observation
    channel: str                   # which executor runs it: app | menu | window | keys | shortcut | file | web | …
    verb: str                      # press | open | activate | type | key | run | research | …
    label: str                     # what the decider reads
    target: dict[str, Any] = field(default_factory=dict)   # executor-specific (refs, pids, paths); never sent out
    slots: dict[str, Slot] = field(default_factory=dict)
    context: str = ""              # where it lives (app ▸ menu path, window)
    score: float = 0.0             # local relevance before the decider sees it

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


TERMINAL = {"done", "failed", "cancelled"}
PENDING = {"need_input", "need_confirm", "ambiguous"}
# plus "blocked": terminal for the engine — something only the user can do (sign in, password, permission) stands in the way



@dataclass
class Pace:
    """How much of each kind of move this task has already made (the engine's own budgets)."""
    replans: int = 0
    looks: int = 0                                             # how many times it did (budget: engine.max_looks)
    launched: bool = False                                     # the app the caller named was opened by the engine
    tidied: bool = False
    waits: int = 0                                             # times the decider chose to wait (budget: engine.max_waits)
    settled_leave: bool = False                                # the app was let finish before the task left it
    settled_done: bool = False                                 # the screen was let settle before judging "done"
    redo: int = 0                                              # decisions discarded because the screen moved meanwhile
    fruitless: int = 0                                         # "rethink" asked with no new route to be had


@dataclass
class Memory:
    """What this run learned as it went: what changed nothing, what was skipped, which screens it saw, and the
    facts it may write from."""
    no_effect: set[str] = field(default_factory=set)           # "screen|action" that changed nothing: never offered again
    expanded: set[str] = field(default_factory=set)            # option groups the decider chose to look into
    screens_seen: set[str] = field(default_factory=set)
    screen_notes: dict[str, str] = field(default_factory=dict)  # distinct screens seen (signature -> short text)
    circles: list[str] = field(default_factory=list)           # actions that only led back to a screen already seen
    facts: Any = None                                          # facts.Facts: what this task has actually seen
    serves: dict[str, bool] = field(default_factory=dict)      # "does leaving for X serve the goal", asked once per action
    declined: set[str] = field(default_factory=set)            # floor actions the decider judged the goal never asked for


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
    status: str = "running"                      # running | done | failed | cancelled | need_input | need_confirm | ambiguous
    steps: list[Step] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)   # what the caller must answer to resume
    outputs: dict[str, Any] = field(default_factory=dict)   # results worth returning (web notes, files found…)
    approved: set[str] = field(default_factory=set)          # affordance labels the caller confirmed
    target: dict[str, Any] | None = None                     # app currently worked in (pid, name, bundle_id)
    held: Any = None                                         # the affordance waiting on the caller (need_input/confirm)
    options: dict[str, Any] = field(default_factory=dict)    # affordances offered when ambiguous, by id
    plan: list[str] | None = None                            # sub-goals from the planner, if one was consulted
    plan_i: int = 0
    prev: dict[str, Any] | None = None                       # last step, waiting to learn where it led
    apps: dict[str, Any] = field(default_factory=dict)       # apps touched (bundle -> info), for saving their models
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
    decider_calls: int = 0
    cost_usd: float = 0.0

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {"task_id": self.id, "status": self.status, "goal": self.goal,
                               "steps": [s.action + ("" if s.ok else " (failed)") for s in self.steps],
                               "seconds": round(time.monotonic() - self.started, 1),
                               "decider": {"calls": self.decider_calls, "cost_usd": round(self.cost_usd, 6)}}
        if self.reason:
            out["reason"] = self.reason
        if self.pending:
            out["pending"] = self.pending
        if self.outputs:
            out["outputs"] = self.outputs
        if self.plan:
            out["plan"] = {"steps": self.plan, "at": min(self.plan_i, len(self.plan)), "replans": self.pace.replans}
        return out
