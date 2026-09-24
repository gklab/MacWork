"""Skills: tasks that worked, kept as routines and replayed step by step.

A step is stored by meaning, not by position: channel + verb + label + context (the menu path, the button's
name and where it sits). Replay re-observes before every step and looks the element up again, so a moved
window or a new app version still works; if a step cannot be found the replay stops and the normal loop
takes over from wherever it got to. Text a step typed is never stored — only which input key it used.

Skills are JSON files under ``skills.dir`` — readable, editable, deletable by the user.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Config, expand
from .model import Affordance, Observation, Task, steady
from .observe import plain_key


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", steady(s)).strip()


def _without_detours(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the net path: the same action taken again from exactly the same screen state (same controls, same
    text) means everything in between led nowhere. A repeated action from a different state — "1 + 1", typing
    the same word into two fields — is real and kept. Steps without a recorded state are never pruned."""
    out: list[dict[str, Any]] = []
    for st in steps:
        key = (st["channel"], st["verb"], st["label"], st.get("before"))
        prev = next((i for i, o in enumerate(out) if st.get("before") and (o["channel"], o["verb"], o["label"], o.get("before")) == key), None)
        if prev is not None:
            del out[prev + 1:]          # back where we were: drop the detour, keep the first occurrence
            continue
        out.append(st)
    return out


class Skills:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dir = expand(cfg.get("skills.dir")) or Path("~/Library/Application Support/macwork/skills").expanduser()
        if not self.dir.exists():                      # data written before the project was named macwork
            older = Path(str(self.dir).replace("/macwork/", "/jev-mac-use/"))
            if older.exists():
                self.dir = older

    def all(self) -> list[dict[str, Any]]:
        if not self.dir.exists():
            return []
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def for_app(self, bundle_id: str | None) -> list[dict[str, Any]]:
        """Routines that start in this app, or that start anywhere (their first step opens/switches the app)."""
        skills = [s for s in self.all() if s.get("start_app") in (bundle_id, None) and s.get("fails", 0) <= s.get("uses", 0) + 1]
        return sorted(skills, key=lambda s: -s.get("uses", 0))[: int(self.cfg.get("skills.max_offered", 20))]

    def record(self, task: Task, start_app: str | None) -> Path | None:
        steps = [s for s in task.steps if s.channel and s.channel != "skill"]
        if not self.cfg.get("skills.record", True) or len(steps) < int(self.cfg.get("skills.min_steps", 2)):
            return None
        if any(not s.ok for s in task.steps) or any(s.decision.get("replay") for s in task.steps):
            return None
        body = [{k: v for k, v in b.items() if k != "before"} for b in _without_detours(
            [{"channel": s.channel, "verb": s.verb, "label": _norm(s.action), "key": s.key, "context": s.context,
              "inputs": s.slot_keys, "before": s.before} for s in steps])]
        sid = hashlib.sha1(json.dumps([start_app, body], ensure_ascii=False).encode()).hexdigest()[:12]
        f = self.dir / f"{sid}.json"
        if f.exists():
            return f
        self.dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"id": sid, "goal": task.goal, "start_app": start_app, "steps": body,
                                 "inputs": sorted({k for s in body for k in s["inputs"]}), "created": time.time(),
                                 "uses": 0, "fails": 0}, ensure_ascii=False, indent=1), encoding="utf-8")
        return f

    def bump(self, sid: str, ok: bool) -> None:
        f = self.dir / f"{sid}.json"
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        s["uses" if ok else "fails"] = s.get("uses" if ok else "fails", 0) + 1
        f.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def find(step: dict[str, Any], obs: Observation) -> Affordance | None:
        """The affordance on screen that means the same as a stored step.

        By identity first — the Accessibility identifier behind the command, which does not change with the
        interface language — and by label only where the app offered no identity, or for routines recorded
        before identities were kept.
        """
        kind = [a for a in obs.affordances if a.channel == step["channel"] and a.verb == step["verb"]]
        if step.get("key"):
            by_identity = [a for a in kind if a.identity() == step["key"]]
            if by_identity:
                return by_identity[0]
        if step["channel"] == "keys":
            # A key has no identity but its label, and its label says the button it presses and where the keyboard
            # is (`plain_key`): 'press the return key' recorded in one place is the same key with the keyboard in
            # a field. Replaying it is judged by the floor on the label as it reads here.
            same = [a for a in kind if _norm(plain_key(a.label)) == _norm(plain_key(step["label"]))]
        else:
            same = [a for a in kind if _norm(a.label) == step["label"]]
        if len(same) > 1 and step.get("context"):
            same = [a for a in same if a.context == step["context"]] or same
        return same[0] if same else None

    @staticmethod
    def describe(skill: dict[str, Any]) -> str:
        path = " → ".join(s["label"] for s in skill["steps"][:6]) + (" → …" if len(skill["steps"]) > 6 else "")
        needs = f" [uses inputs: {', '.join(skill['inputs'])}]" if skill.get("inputs") else ""
        return f"replay the learned routine for 「{skill['goal']}」: {path}{needs}"
