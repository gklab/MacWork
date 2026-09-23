"""The one vocabulary every part speaks: what can be seen (Observation) and what can be done (Affordance)."""

from __future__ import annotations

import re
import time
import unicodedata
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


def word_kind(ch: str) -> str:
    """What kind of writing one character is, for "does a word go on here": a letter of a script without
    capitals ("uncased": Chinese, Japanese, Korean, Arabic, Hebrew, Thai…), a letter of one with them
    ("cased"), a digit, a combining mark, or "" — a space, punctuation, an underscore — where a word ends.
    Scripts of one kind are not told apart: Latin and Cyrillic are both "cased", Chinese and Arabic both
    "uncased"."""
    if ch.isalpha():
        return "uncased" if ch.lower() == ch.upper() else "cased"
    if ch.isdigit():
        return "digit"
    return "mark" if len(ch) == 1 and unicodedata.category(ch).startswith("M") else ""


def joined(a: str, b: str) -> bool:
    """Does a word whose last letter is `a` go on into `b`? Into a combining mark it does: the mark belongs to
    the letter before it. Otherwise only where both are of one kind, so a word ends at a space, punctuation
    or an underscore, where letters with capitals meet letters without ("Safari" in "Safari浏览器"), and where
    letters meet digits.

    `a` is a letter, never a mark: a mark does not say what kind of letter it sits on. Taking the mark for
    the letter, this went on after it into anything a word is made of, so after 「café」 written with its
    accent as a mark of its own a word went on into Chinese and into digits, where after 「café」 written as
    one character it ends. Text is asked with `joined_at`, which looks past a mark to its letter; a mark
    given here as `a` sits on no letter and carries no word on."""
    ka, kb = word_kind(a), word_kind(b)
    return kb == "mark" or (kb != "" and ka == kb)


def joined_at(text: str, k: int) -> bool:
    """Does a word go on across position `k` of `text`, from text[k - 1] into text[k]? `joined`, with a
    combining mark read as the letter it sits on, so a word ends in the same places whether an accent is
    written into its letter or as a mark after it: 「Chloé2024」 is 「Chloé」 and 「2024」 either way, and
    「cafés」 is one word. Nothing is joined at either end of the text."""
    if not 0 < k < len(text):
        return False
    i = k - 1
    while i > 0 and word_kind(text[i]) == "mark":
        i -= 1
    return joined(text[i], text[k])


def clip(text: str, n: int) -> str:
    """At most `n` characters, cut where a word ends (`joined_at`), with "…" where anything was cut.

    Cut anywhere else, the piece that is left reads like a word of its own: a 'look into' summary cut each
    label at 40 characters, 「… — a Service of Saf」 was tagged a person, and 「Saf」 then went out as a
    pseudonym inside 「Safari」. So the cut steps back to where a word ends, giving up at most n // 2
    characters: in a script written without spaces a whole run of letters is one word, and a step back with
    no bound gives up as much of the room as that run fills. A word that began more than n // 2 characters
    before the end of the room is cut where the room ends, though never between a letter and its accent; one
    that began nearer is left out whole. Cut at 40, as a 'look into' summary cuts them, the option labels in
    this Mac's audit lose a median of 1 character to the step back; the bound decides 5 of the 6,106 that
    are longer, each a Latin name of more than 20 letters, which it cuts inside.
    """
    if len(text) <= n:
        return text
    if n < 2:
        return "…"[:max(n, 0)]
    k = n - 1
    floor = max(k - n // 2, 1)
    while k > floor and joined_at(text, k):
        k -= 1
    if joined_at(text, k):                   # still inside a word, one that began further back than that
        k = n - 1
        while k > 1 and word_kind(text[k]) == "mark":
            k -= 1
    return text[:k].rstrip() + "…"


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

    def handle(self) -> str:
        """What the engine keys its memory on: the identity where there is one, else the name without its
        momentary state. Memory was keyed on the full label, so a field that read 「type into Search (now:
        hello)」 was a new action after every keystroke, and nothing remembered about it ever matched."""
        return self.key or self.name()

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
class Change:
    """What the last action did to the screen, as facts. `describe()` is the sentence the decider reads.

    It was only ever the sentence, and the engine read the sentence back — `endswith("no text on screen
    did")` — to know what it had itself just said. Facts here, prose rendered from them; nothing parses the
    prose. Stored on the step as a plain dict (`Step.change`) so it survives a restart like everything else.
    """
    app_before: str | None = None
    app_after: str | None = None
    window_before: str | None = None
    window_after: str | None = None
    appeared: list[str] = field(default_factory=list)      # lines of screen text that are there now and were not
    gone: list[str] = field(default_factory=list)          # …and the reverse
    picture_share: float | None = None                     # of the window's picture, when a glance could compare
    picture_where: str | None = None

    @property
    def app_changed(self) -> bool:
        return bool(self.app_after and self.app_before and self.app_after != self.app_before)

    @property
    def window_changed(self) -> bool:
        return (self.window_after or "") != (self.window_before or "") and bool(self.window_before or self.window_after)

    @property
    def text_changed(self) -> bool:
        return bool(self.appeared or self.gone)

    @property
    def picture_changed(self) -> bool:
        return bool(self.picture_share)

    @property
    def picture_only(self) -> bool:
        """Something changed that no text or window title shows: what the tree cannot say, the screen can."""
        return self.picture_changed and not (self.text_changed or self.window_changed or self.app_changed)

    @property
    def nothing(self) -> bool:
        return not (self.text_changed or self.window_changed or self.app_changed or self.picture_changed)

    def describe(self, limit: int = 200) -> str:
        def cut(x: str, n: int = 48) -> str:   # never inside a word: see `clip`
            return clip(x, n)
        parts: list[str] = []
        if self.app_changed:
            parts.append(f"now in {self.app_after}")
        if self.window_changed:
            parts.append(f"window 「{cut(self.window_before or '(none)', 30)}」 → 「{cut(self.window_after or '(none)', 30)}」")
        if len(self.gone) == 1 and len(self.appeared) == 1:
            parts.append(f"「{cut(self.gone[0])}」 became 「{cut(self.appeared[0])}」")
        else:
            for name, items in (("appeared", self.appeared), ("gone", self.gone)):
                if items:
                    shown = "; ".join(cut(x) for x in items[:3])
                    parts.append(f"{name}: {shown}" + (f" (+{len(items) - 3} more)" if len(items) > 3 else ""))
        if not parts and self.picture_changed:
            share = (self.picture_share or 0.0) * 100
            amount = f"{share:.0f}%" if share >= 1 else "under 1%"
            return f"the picture in the window changed ({amount} of it, {self.picture_where}); no text on screen did"
        out = ", ".join(parts) or NOTHING_CHANGED
        return clip(out, limit)

    def as_dict(self) -> dict[str, Any]:
        return {"app_before": self.app_before, "app_after": self.app_after, "window_before": self.window_before,
                "window_after": self.window_after, "appeared": list(self.appeared), "gone": list(self.gone),
                "picture_share": self.picture_share, "picture_where": self.picture_where,
                "picture_only": self.picture_only, "nothing": self.nothing}


NOTHING_CHANGED = "nothing on screen changed"


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
    change: dict[str, Any] = field(default_factory=dict)   # the same, as facts (`Change.as_dict()`): what the
                                                 # engine reads; `outcome` is what the decider reads
    led_back: bool = False                       # it only led back to a screen the task had already seen
    promise: str = ""                            # what the action said it would do (contract.promise)
    kept: bool | None = None                     # and whether it did: True / False / None = could not be judged
    kept_why: str = ""
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

    @property
    def handle(self) -> str:
        """The same as `Affordance.handle()`, for a step already taken."""
        return self.key or steady(self.action)


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
    no_progress: set[str] = field(default_factory=set)         # "screen|action" taken in a stretch that got the task nowhere
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
    last_route: list[str] = field(default_factory=list)       # the sub-goals and moves of the planner's last answer:
                                                               # the same answer again is no new route

    # ---- the one way in and the one way out. The keys were built by hand in six places ("sig|handle",
    # "sig|label" before that) and read back in five; one of them still used the label a day after the
    # rest had moved to handles. Nothing outside these methods spells a key.
    @staticmethod
    def _at(sig: str, handle: str) -> str:
        return f"{sig}|{handle}"

    def note_no_effect(self, sig: str, handle: str) -> None:
        """This action, from this screen, changed nothing: a fact, never offered from here again."""
        self.no_effect.add(self._at(sig, handle))

    def note_no_progress(self, sig: str, handle: str) -> None:
        """Taken in a stretch of steps that got the task nowhere: not offered again from where it was taken."""
        self.no_progress.add(self._at(sig, handle))

    def note_failed(self, sig: str, handle: str, state: str) -> None:
        """It could not be carried out just then: withheld while the screen is exactly as it was."""
        self.failed[self._at(sig, handle)] = state

    def note_withdrawn(self, state: str, handle: str) -> None:
        """Chosen from this exact state, and the task came back here: told once, withdrawn at the limit."""
        self.retracted.setdefault(state, []).append(handle)

    def decline(self, handle: str) -> None:
        """Not what the goal is about, or only ever leads back: not offered again in this task."""
        self.declined.add(handle)

    def no_effect_handles(self) -> set[str]:
        return {k.split("|", 1)[1] for k in self.no_effect}

    def withdrawn_from(self, state: str, limit: int) -> set[str]:
        got = self.retracted.get(state) or []
        return {h for h in set(got) if got.count(h) >= limit}

    def withheld_reason(self, sig: str, state: str, handle: str, approval: str, limit: int) -> str | None:
        """Why this action is not offered from this screen, or None: no_effect | failed | withdrawn | declined."""
        if self._at(sig, handle) in self.no_effect:
            return "no_effect"
        if self._at(sig, handle) in self.no_progress:
            return "no_progress"
        if self.failed.get(self._at(sig, handle)) == state:
            return "failed"
        if handle in self.withdrawn_from(state, limit):
            return "withdrawn"
        if handle in self.declined or approval in self.declined:
            return "declined"
        return None


@dataclass
class TaskScope:
    """What one task keeps between its looks and is never stored: live, per-task working state.

    It sat in `engine.cache` under keys like `pictures[task_id]` and `vision.wanted[task_id]`, beside
    caches that are the Mac's (installed apps, floor verdicts), collected by naming each key in a list.
    One object per task, made with the task and dropped with it.
    """
    pictures: dict[str, Any] = field(default_factory=dict)     # exact state -> what it looked like the first time
    vision_wanted: set[str] = field(default_factory=set)       # windows this task asked to read by sight
    windows_seen: set[Any] = field(default_factory=set)        # window ids seen so far: a new one is read first


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
    plan_evidence: list[str] = field(default_factory=list)   # per sub-goal, what the screen shows once it is done ("" = unsaid)
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
