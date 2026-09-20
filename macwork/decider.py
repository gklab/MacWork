"""Deciders answer typed questions about a state. Jev is the default; anything with the same shape plugs in
(entry point group ``macwork.deciders``), so the engine never depends on one vendor.

Questions and answers use TypeSafe's wire shape:
  questions: {name: {"type": "choice"|"noul"|"score", "instructions": str, "criteria": ...}}
  answers:   {name: {"type": ..., "choice"/"noul"/"score": ..., "probabilities": {...}, "confidence": float}}
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from importlib.metadata import entry_points
from typing import Any, Protocol

from .config import Config

MAX_OPTIONS = 255


class DeciderError(RuntimeError):
    pass


class Decider(Protocol):
    calls: int
    cost_usd: float
    last_ms: float

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]: ...


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "yes", "false": false or "no"}
    return q


def choice(instructions: str, options: dict[str, str]) -> dict[str, Any]:
    if not 2 <= len(options) <= MAX_OPTIONS:
        raise ValueError(f"choice needs 2..{MAX_OPTIONS} options, got {len(options)}")
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    if len(levels) < 2:
        raise ValueError("score needs at least 2 levels")
    return {"type": "score", "instructions": instructions, "criteria": levels}


def keychain_key(service: str, account: str = "typesafe") -> str:
    for name in (service, "jev-mac-use"):              # keys stored before the project was named macwork still work
        r = subprocess.run(["security", "find-generic-password", "-s", name, "-a", account, "-w"],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            return r.stdout.strip()
    return ""


def store_keychain_key(service: str, key: str, account: str = "typesafe") -> None:
    subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", key],
                   capture_output=True, check=True)


class JevDecider:
    """TypeSafe System One via the official SDK. Key: TYPESAFE_API_KEY, else the Keychain item."""

    def __init__(self, cfg: Config) -> None:
        import typesafe_sdk as ts

        key = os.environ.get("TYPESAFE_API_KEY", "").strip() or keychain_key(cfg.get("decider.keychain_service", "macwork"))
        if not key:
            raise DeciderError("no TypeSafe API key: set TYPESAFE_API_KEY or run `macwork key set`")
        import httpx2

        timeout = float(cfg.get("decider.timeout_s", 10))
        proxy = str(cfg.get("decider.proxy", "system") or "system")
        # "system" follows the Mac's proxy settings; "none" goes direct (e.g. when a VPN tunnel handles routing
        # and its local proxy port comes and goes); anything else is a proxy URL
        http = httpx2.Client(timeout=timeout, trust_env=proxy == "system",
                             proxy=None if proxy in ("system", "none") else proxy)
        self._http = http
        self._base = str(cfg.get("decider.base_url", "https://api.typesafe.ai"))
        self._client = ts.TypeSafeClient(api_key=key, model=cfg.get("decider.model", "jev-latest"), http_client=http,
                                         retry=ts.RetryPolicy(max_retries=int(cfg.get("decider.max_retries", 3)),
                                                              backoff_initial=float(cfg.get("decider.backoff_s", 0.5)),
                                                              backoff_max=float(cfg.get("decider.backoff_max_s", 4.0))))
        self._errors = (ts.TypeSafeError,)
        self.usd_per_mtok = float(cfg.get("decider.usd_per_mtok", 0.042))
        self._lock = threading.Lock()
        self.calls = 0
        self.cost_usd = 0.0
        self.last_ms = 0.0

    def warm(self) -> None:
        """Open (or refresh) the pooled connection before the first decision: after a pause the TLS handshake
        alone costs a round trip or two. Best effort, nothing is sent but a HEAD."""
        if time.monotonic() - getattr(self, "_last_used", 0.0) < float(self._warm_after_s):
            return
        try:
            self._http.head(self._base, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    _warm_after_s = 15.0

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        t0 = time.monotonic()
        self._last_used = t0
        try:
            r = self._client.system_one(state=state, questions=questions)
        except self._errors as exc:
            raise DeciderError(f"{type(exc).__name__}: {exc}") from exc
        answers = {k: v.model_dump() for k, v in r.answers.items()}
        with self._lock:
            self.calls += 1
            self.cost_usd += (r.usage.input_tokens or 0) * self.usd_per_mtok / 1e6 if r.usage else 0.0
            self.last_ms = (time.monotonic() - t0) * 1000
        return answers


class DryRunDecider:
    """Audit mode: nothing leaves the Mac. Every decision fails, so callers see exactly what would be sent."""

    calls = 0
    cost_usd = 0.0
    last_ms = 0.0

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        raise DeciderError("dry run: decider not called (see the audit log for the state that would be sent)")


def make_decider(cfg: Config, helper: Any = None) -> Decider:
    if cfg.get("audit.dry_run"):
        return DryRunDecider()
    kind = cfg.get("decider.kind", "jev")
    if kind == "jev":
        return JevDecider(cfg)
    if kind == "local":     # any model the planner can reach; see localdecider.py on what it costs in calibration
        from .localdecider import LocalDecider

        return LocalDecider(cfg, helper)
    for ep in entry_points(group="macwork.deciders"):
        if ep.name == kind:
            return ep.load()(cfg)
    raise DeciderError(f"unknown decider '{kind}'")
