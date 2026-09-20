"""When something happens on this Mac, do something about it.

The engine could be asked to do a thing and could be resumed, but nothing ever *woke* it. The daemon sat
there, and a task began only because a person started one — which makes the whole of it a tool rather than
something that lives in the Mac.

What is missing is not a scheduler; macOS has one, and `launchd` runs this. It is the other half: "when
this happens, do that". The events come from the helper and every one of them is the system announcing
something about itself — an app started, a folder changed, the screen unlocked. Which apps and which
folders matter is not known here and must not be: it comes from the person's own file.

A trigger is a **caller**, not a provider and not a channel. It starts the loop from outside, exactly as a
person or an MCP client does, and so it inherits the safety floor, the facts, the redaction and the budgets
without any of them being re-implemented. Nothing here drives the Mac.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from .config import Config, expand

log = logging.getLogger(__name__)

WHEN_KEYS = {"event", "app", "bundle_id", "path", "text"}


class Trigger:
    """One rule: which events it answers to, what to do, and how often at most."""

    def __init__(self, raw: dict[str, Any], n: int) -> None:
        when = raw.get("when") or {}
        if isinstance(when, str):
            when = {"event": when}
        unknown = set(when) - WHEN_KEYS
        if unknown:
            raise ValueError(f"trigger {n}: 'when' has {sorted(unknown)}; it takes {sorted(WHEN_KEYS)}")
        if not raw.get("do"):
            raise ValueError(f"trigger {n}: needs a 'do' (the goal to hand to the engine)")
        self.name = str(raw.get("name") or f"trigger {n}")
        self.when = when
        self.goal = str(raw["do"])
        self.inputs = dict(raw.get("inputs") or {})
        self.debounce_s = float(raw.get("debounce_s", 30))
        self.enabled = bool(raw.get("enabled", True))
        self.pass_event = bool(raw.get("pass_event", True))
        self.last_fired = 0.0

    def matches(self, event: dict[str, Any]) -> bool:
        """Every condition given must hold. `event` is matched by pattern, the rest by glob, so a rule can
        say `app.*` or a folder's whole subtree without the file inventing a syntax of its own."""
        if not self.enabled:
            return False
        for key, want in self.when.items():
            got = str(event.get("path" if key == "path" else key, "") or "")
            if key == "event":
                got = str(event.get("kind", ""))
            if key == "text":
                got = " ".join(str(v) for v in event.values())
            if not fnmatch.fnmatch(got, str(want)) and not re.fullmatch(str(want), got or ""):
                return False
        return True

    def ready(self, now: float) -> bool:
        """A folder being written to fires once per file; a rule that answers it should not run once per file."""
        return now - self.last_fired >= self.debounce_s


def triggers_path(cfg: Config) -> Path:
    return Path(expand(cfg.get("watch.file", "~/.config/macwork/triggers.yaml")))


def load_triggers(cfg: Config) -> list[Trigger]:
    path = triggers_path(cfg)
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Trigger(raw, i + 1) for i, raw in enumerate(doc.get("triggers") or [])]


def subscription(cfg: Config, triggers: list[Trigger]) -> dict[str, Any]:
    """What to ask the helper to watch, worked out from the rules rather than switched on wholesale.

    Watching a folder costs a stream and watching the clipboard costs a poll; neither should happen because
    a rule somewhere else needed apps.
    """
    kinds = [t.when.get("event", "*") for t in triggers if t.enabled]
    paths = sorted({str(t.when["path"]).split("*")[0].rstrip("/") or "/"
                    for t in triggers if t.enabled and t.when.get("path")})
    def any_kind(*prefixes: str) -> bool:
        return any(k == "*" or k.startswith(prefixes) or fnmatch.fnmatch(p, k)
                   for k in kinds for p in prefixes)
    return {"apps": any_kind("app.", "volume."),
            "screen_lock": any_kind("screen."),
            "clipboard_every_s": float(cfg.get("watch.clipboard_every_s", 1.0)) if any_kind("clipboard.") else 0,
            "paths": paths}


class Watcher:
    """Polls the helper for what happened and hands matching work to the engine's queue."""

    def __init__(self, engine: Any, cfg: Config | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or engine.cfg
        self.triggers = load_triggers(self.cfg)
        self.seq = 0
        self.fired: list[dict[str, Any]] = []

    def start(self) -> dict[str, Any]:
        sub = subscription(self.cfg, self.triggers)
        got = self.engine.helper.call("events.watch", **sub)
        self.seq = int(self.engine.helper.call("events.poll", after=0).get("seq", 0))   # only what happens from now
        return {"watching": got.get("watching"), "triggers": [t.name for t in self.triggers],
                "kinds_available": got.get("kinds")}

    def tick(self) -> list[dict[str, Any]]:
        """One round: read what happened, hand over what matches. Returns what it started."""
        got = self.engine.helper.call("events.poll", after=self.seq)
        if got.get("oldest", 0) > self.seq + 1 and self.seq:
            log.warning("missed %d events while away (the helper's buffer only holds so many)",
                        got["oldest"] - self.seq - 1)
        self.seq = int(got.get("seq", self.seq))
        started: list[dict[str, Any]] = []
        now = time.monotonic()
        for event in got.get("events") or []:
            for t in self.triggers:
                if not t.matches(event) or not t.ready(now):
                    continue
                t.last_fired = now
                inputs = dict(t.inputs)
                if t.pass_event:
                    # what happened is an input to the task, like any other text the caller supplies — and
                    # it goes through the same redaction on its way to the decider
                    inputs.setdefault("event", {k: v for k, v in event.items() if k != "seq"})
                out = self.engine.submit(t.goal, inputs)
                log.info("%s fired on %s -> task %s", t.name, event.get("kind"), out.get("task_id"))
                started.append({"trigger": t.name, "on": event.get("kind"), **out})
        self.fired += started
        return started

    def run(self, every_s: float | None = None, until: float | None = None) -> None:
        every = float(every_s if every_s is not None else self.cfg.get("watch.poll_s", 2.0))
        deadline = (time.monotonic() + until) if until else None
        while deadline is None or time.monotonic() < deadline:
            try:
                self.tick()
            except Exception as exc:                 # noqa: BLE001  (a watcher that stops watching is useless)
                log.warning("watch: %s", exc)
            time.sleep(every)

    def stop(self) -> None:
        try:
            self.engine.helper.call("events.watch", stop=True)
        except Exception:                            # noqa: BLE001
            pass
