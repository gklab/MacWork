"""What must hold, checked where it must hold — and said, never assumed.

A run on a real Mac found engine bugs that no reading of the code had: a switch recorded as taken while
the old app was still in front, a step whose memory key was spelled from the label a day after the rest had
moved on, an observation whose ids collided. Each of these is a statement about the engine's own state that
is either true or not, and the engine can check it itself, cheaply, every time. A broken invariant is not
an exception — the task goes on — it is a fact written to the task's outputs and the log, so the next real
run reports engine bugs as engine bugs instead of leaving them to be dug out of traces.
"""

from __future__ import annotations

import logging
from typing import Any

from .model import PENDING, TERMINAL, Affordance, Step, Task

log = logging.getLogger(__name__)

KNOWN_STATUSES = TERMINAL | PENDING | {"blocked", "running", "queued"}


def broken(task: Task, what: str) -> None:
    """Record a broken invariant on the task: the engine's own bug, as a fact."""
    log.error("invariant broken in task %s: %s", task.id, what)
    task.outputs.setdefault("invariants_broken", []).append(what[:200])


def check_look(task: Task, affs: list[Affordance], sig: str, app: dict[str, Any] | None) -> None:
    """After a look: every option has a handle to be remembered by, ids are unique, a screen has a signature."""
    ids = [a.id for a in affs]
    if len(ids) != len(set(ids)):
        broken(task, f"an observation offered the same id twice: {sorted({i for i in ids if ids.count(i) > 1})[:5]}")
    nameless = [a for a in affs if not a.handle()]
    if nameless:
        broken(task, f"{len(nameless)} option(s) with nothing to remember them by (no label, no identity)")
    if app and not sig:
        broken(task, "a look at an app produced no screen signature")


def check_step(task: Task, step: Step, had_look: bool) -> None:
    """After a step: it records where it was taken from and what it promised; a broken immediate promise is
    a failed step, never an ok one."""
    if had_look and not step.before:
        broken(task, f"step {step.n} ({step.action[:40]}) does not record the screen it was taken from")
    if not step.promise:
        broken(task, f"step {step.n} ({step.action[:40]}) made no promise")
    if step.kept is False and step.ok:
        broken(task, f"step {step.n} ({step.action[:40]}) broke its promise and was recorded ok")


def check_ending(task: Task, status: str) -> None:
    """At an ending: the status is one the callers know, and a task that stopped short says why."""
    if status not in KNOWN_STATUSES:
        broken(task, f"ended in a status nobody knows: {status!r}")
    if status in ("failed", "blocked") and not task.reason:
        broken(task, f"ended {status} with no reason")
    if status in ("failed", "blocked") and not task.cause:
        broken(task, f"ended {status} with no cause")    # a real run ended failed with cause null: nothing for a program to read
