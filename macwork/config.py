"""Layered configuration: packaged defaults < ~/.config/macwork/*.yaml < MACWORK__A__B=value env vars.

Four documents, each overridable the same way: ``config`` (engine knobs), ``questions`` (every instruction
the decider sees), ``policy`` (what may be done without asking) and ``privacy`` (what may leave the Mac).
"""

from __future__ import annotations

import copy
import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DOCS = ("config", "questions", "policy", "privacy")
USER_DIR = Path(os.environ.get("JMU_CONFIG_DIR", "~/.config/macwork")).expanduser()


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _env_overrides(prefix: str = "MACWORK__") -> dict[str, Any]:
    """MACWORK__ENGINE__MAX_STEPS=20 -> {"engine": {"max_steps": 20}} (values parsed as YAML scalars/lists)."""
    out: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = [p.lower() for p in key[len(prefix):].split("__") if p]
        if not path:
            continue
        node = out
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = yaml.safe_load(raw)
    return out


class Config:
    """Read-only view with dotted lookups: ``cfg.get("engine.thresholds.act")``."""

    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    @classmethod
    def load(cls, user_dir: Path | None = None, overrides: dict[str, dict[str, Any]] | None = None) -> "Config":
        user_dir = user_dir or USER_DIR
        if not user_dir.exists():                      # settings written before the project was named macwork
            for older in (user_dir.parent / "jev-mac-use",):
                if older.exists():
                    user_dir = older
                    break
        docs: dict[str, dict[str, Any]] = {}
        for name in DOCS:
            text = resources.files("macwork").joinpath(f"defaults/{name}.yaml").read_text(encoding="utf-8")
            doc = yaml.safe_load(text) or {}
            user = user_dir / f"{name}.yaml"
            if user.exists():
                doc = _merge(doc, yaml.safe_load(user.read_text(encoding="utf-8")) or {})
            docs[name] = doc
        docs["config"] = _merge(docs["config"], _env_overrides())
        for name, over in (overrides or {}).items():
            docs[name] = _merge(docs[name], over)
        return cls(docs)

    def get(self, dotted: str, default: Any = None, doc: str = "config") -> Any:
        node: Any = self.docs[doc]
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str, doc: str = "config") -> dict[str, Any]:
        v = self.get(dotted, {}, doc)
        return v if isinstance(v, dict) else {}

    def question(self, name: str) -> str:
        q = self.docs["questions"].get(name)
        if not q:
            raise KeyError(f"no question text '{name}' in questions.yaml")
        return str(q).strip()

    @property
    def policy(self) -> dict[str, Any]:
        return self.docs["policy"]

    @property
    def privacy(self) -> dict[str, Any]:
        return self.docs["privacy"]


def expand(path: str | None) -> Path | None:
    return Path(path).expanduser() if path else None
