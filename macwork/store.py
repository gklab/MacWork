"""Tasks that survive a restart.

A task lived in a dict and nowhere else: quit the engine — a crash, a logout, `macwork serve` restarted —
and every task waiting on the caller was gone, along with the record of what it had opened, which is the only
thing that lets tidying touch its own doing and nothing else. A caller holding a `task_id` got "unknown or
expired task" with no way to tell a real expiry from a restart.

What is stored is the task's own state, never a live handle: pids, window numbers and Accessibility
references belong to the machine as it was and are deliberately left behind. A resumed task looks at the
screen again, which is what it would do anyway.

SQLite from the standard library — no new dependency, and a single file that survives being copied,
inspected with `sqlite3`, or deleted by a user who wants to forget.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from .config import Config, expand
from .facts import Fact, Facts
from .model import Desktop, Memory, Pace, Step, Task

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    goal       TEXT NOT NULL,
    updated    REAL NOT NULL,
    state      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks (updated);
"""

# Rebuilt from their own fields; anything not listed is a plain value.
_PARTS = {"pace": Pace, "memory": Memory, "desktop": Desktop}
_SETS = {"approved", "no_effect", "no_progress", "declined", "yielded", "consulted"}


def _as_set(items: list[Any]) -> set[Any]:
    """A set read back from JSON. JSON has no tuples, so a tuple went out as a list and has to come back as
    a tuple to be hashable again — `memory.consulted` is keyed on (screen, plan step, started), and it was
    the one set left off the list: a resumed task's first replan raised on `.add` of a list."""
    return {tuple(x) if isinstance(x, list) else x for x in items}


def _plain(value: Any) -> Any:
    """A dataclass tree as JSON, with sets written as lists and the live handles left out."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def dump(task: Task) -> str:
    state = {f.name: _plain(getattr(task, f.name)) for f in fields(task) if f.name != "held"}
    facts = task.memory.facts
    state["memory"]["facts"] = {} if facts is None else {
        "seen": {k: _plain(v) for k, v in facts.seen.items()},
        "inputs": facts.inputs, "goal": facts.goal, "limit": facts.limit}
    # held is the affordance the caller must answer about: it names a live element that will not exist after a
    # restart, so it is not stored. A resumed task looks again and offers the same thing from the new screen.
    return json.dumps(state, ensure_ascii=False, default=str)


# Handles to a machine that has moved on. The module docstring has always said these are left behind;
# `dump` stores every field but `held`, so they were not. macOS reuses pids, so a resumed task carrying
# one can act on whatever process holds that number now. `desktop.opened` stays — it is the only record
# that lets tidying touch this task's own doing — and what makes it safe is the check at the moment of
# acting (`PolicyMixin.still_the_app`), not its absence from disk.
_STALE = {"desktop": ("initial_pids", "initial_windows"), "target": ("pid",)}


def load(text: str) -> Task:
    state = json.loads(text)
    for field_, keys in _STALE.items():
        got = state.get(field_)
        if isinstance(got, dict):
            for k in keys:
                got[k] = None
    task = Task(goal=state.get("goal", ""))
    for f in fields(Task):
        if f.name not in state or f.name == "held":
            continue
        value = state[f.name]
        if f.name in _PARTS:
            part = _PARTS[f.name]()
            for pf in fields(part):
                if pf.name in value:
                    got = value[pf.name]
                    setattr(part, pf.name, _as_set(got) if pf.name in _SETS and isinstance(got, list) else got)
            setattr(task, f.name, part)
        elif f.name == "steps":
            task.steps = [Step(**{k: v for k, v in s.items() if k in {x.name for x in fields(Step)}}) for s in value]
        elif f.name in _SETS and isinstance(value, list):
            setattr(task, f.name, _as_set(value))
        else:
            setattr(task, f.name, value)
    facts = (state.get("memory") or {}).get("facts") or {}
    task.memory.facts = Facts(inputs=facts.get("inputs") or {}, goal=facts.get("goal") or task.goal,
                              limit=int(facts.get("limit") or 600))
    task.memory.facts.seen = {k: Fact(**v) for k, v in (facts.get("seen") or {}).items() if isinstance(v, dict)}
    desktop = task.desktop
    if isinstance(desktop.initial_pids, list):
        desktop.initial_pids = set(desktop.initial_pids)
    if isinstance(desktop.initial_windows, dict):
        desktop.initial_windows = {int(k): set(v) for k, v in desktop.initial_windows.items()}
    desktop.opened = {int(k): v for k, v in (desktop.opened or {}).items()}
    # The monotonic clock restarted with the process: the task began `spent_s` of working time ago as far as
    # it can tell. (`min` was written here; it is always 0.0, and every task came back having taken no time.)
    task.started = time.monotonic() - max(task.spent_s, 0.0)
    return task


class Store:
    """Where tasks live between runs. ``enabled: false`` keeps everything in memory, as before."""

    def __init__(self, cfg: Config) -> None:
        self.ttl = float(cfg.get("engine.tasks_ttl_s", 1800))
        self.enabled = bool(cfg.get("engine.persist", True))
        path = expand(cfg.get("engine.store")) or Path("~/Library/Application Support/macwork/tasks.db").expanduser()
        self.path = path
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if self.enabled:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(path), check_same_thread=False)
                self._db.executescript(SCHEMA)
                self._db.commit()
                path.chmod(0o600)   # it holds what the tasks saw
            except (OSError, sqlite3.Error) as exc:
                log.warning("tasks are not being kept (%s): %s", path, exc)
                self._db = None

    def save(self, task: Task) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO tasks (id, status, goal, updated, state) VALUES (?,?,?,?,?)",
                                 (task.id, task.status, task.goal, task.updated, dump(task)))
                self._db.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:   # never let bookkeeping take a task down
            log.warning("could not keep task %s: %s", task.id, exc)

    def get(self, task_id: str) -> Task | None:
        if self._db is None:
            return None
        try:
            with self._lock:
                row = self._db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return load(row[0])
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("task %s could not be read back: %s", task_id, exc)
            return None

    def forget(self, task_id: str) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._db.commit()

    def sweep(self) -> int:
        """Drop what is past its time. Returns how many went."""
        if self._db is None:
            return 0
        with self._lock:
            cur = self._db.execute("DELETE FROM tasks WHERE updated < ?", (time.time() - self.ttl,))
            self._db.commit()
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
