"""Deciders answer typed questions about a state. Jev is the default; anything with the same shape plugs in
(entry point group ``macwork.deciders``), so the engine never depends on one vendor.

Questions and answers use TypeSafe's wire shape:
  questions: {name: {"type": "choice"|"noul"|"score", "instructions": str, "criteria": ...}}
  answers:   {name: {"type": ..., "choice"/"noul"/"score": ..., "probabilities": {...}, "confidence": float}}
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from importlib.metadata import entry_points
from typing import Any, Protocol

from .config import Config

log = logging.getLogger(__name__)

MAX_OPTIONS = 255


class DeciderError(RuntimeError):
    pass


class Decider(Protocol):
    calls: int
    cost_usd: float
    last_ms: float

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]: ...


def noul(instructions: str, true: str | None = None, false: str | None = None,
         fills: dict[str, str] | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "yes", "false": false or "no"}
    if fills:                # "{action}" and friends: redacted on their own, so our own wording survives
        q["fills"] = dict(fills)
    return q


def choice(instructions: str, options: dict[str, str], fills: dict[str, str] | None = None) -> dict[str, Any]:
    if not 2 <= len(options) <= MAX_OPTIONS:
        raise ValueError(f"choice needs 2..{MAX_OPTIONS} options, got {len(options)}")
    q: dict[str, Any] = {"type": "choice", "instructions": instructions, "criteria": options}
    if fills:
        q["fills"] = dict(fills)
    return q


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

    name = "jev"
    calibrated = True      # its probabilities are measured frequencies; the thresholds cut on them

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

    def route_ms(self, samples: int = 3) -> float | None:
        """One round trip to the service on a connection that is already open, in ms — no key, no tokens.

        Every step costs at least this, whatever the engine does, and it differs by an order of magnitude
        between networks: measured from one Mac behind a tunnel, a four-option request a game would send
        took 467 ms against 550 ms for the engine's own two-hundred-option one. People driving games
        smoothly with the same model are not sending smaller requests, they are closer to it. So the
        number is reported rather than left to be mistaken for the engine being slow.
        """
        try:
            self._http.head(self._base, timeout=8)             # opens (or reuses) the pooled connection
            took = []
            for _ in range(max(1, samples)):
                t0 = time.monotonic()
                self._http.head(self._base, timeout=8)
                took.append((time.monotonic() - t0) * 1000)
            return round(min(took), 1)
        except Exception:                                       # noqa: BLE001  (a diagnostic must not raise)
            return None

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

    name = "dry_run"
    calls = 0
    cost_usd = 0.0
    last_ms = 0.0

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        raise DeciderError("dry run: decider not called (see the audit log for the state that would be sent)")


def _build(kind: str, cfg: Config, helper: Any) -> Decider:
    if kind == "jev":
        return JevDecider(cfg)
    if kind == "local":     # any model the planner can reach; see localdecider.py on what it costs in calibration
        from .localdecider import LocalDecider

        return LocalDecider(cfg, helper)
    for ep in entry_points(group="macwork.deciders"):
        if ep.name == kind:
            return ep.load()(cfg)
    raise DeciderError(f"unknown decider '{kind}'")


class ChainDecider:
    """Deciders in preference order; the next one answers when the one in front cannot.

    Building them in order covers "no key". It does not cover the case that actually happens — a key that
    works and a network that does not — because that one only shows up at the moment a decision is needed,
    which is every step. So a failure here moves to the next decider and *stays* there: half a task decided
    by a calibrated model and half by an uncalibrated one, alternating per step, would make the thresholds in
    `config.yaml` mean nothing in particular. Switching is loud, and `status()` reports who is answering.

    A decision the decider *answered* is never retried here. Only a `DeciderError` — no answer at all — moves
    on; a bad answer is the engine's business, and asking a second model until one agrees is not a fallback.
    """

    def __init__(self, deciders: list[Decider]) -> None:
        self.deciders = deciders
        # Every step makes two `decide()` calls — the step's own, and the safety classification sent beside
        # it — so two failures can arrive together. Dropping the head with a bare pop(0) could then drop
        # two for one failure and empty the list, which wedges the engine for the rest of the session.
        self._lock = threading.Lock()

    def __getattr__(self, attr: str) -> Any:      # name, calls, cost_usd, last_ms, warm…: whoever is in front
        return getattr(self.deciders[0], attr)

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        while True:
            head = self.deciders[0]
            try:
                return head.decide(state, questions)
            except DeciderError as exc:
                with self._lock:
                    if self.deciders and self.deciders[0] is not head:
                        continue          # someone else already moved on; try whoever is in front now
                    if len(self.deciders) == 1:
                        raise
                    log.warning("decider %s could not answer (%s); switching to %s for the rest of this session",
                                getattr(head, "name", "?"), exc, getattr(self.deciders[1], "name", "?"))
                    self.deciders.pop(0)


def make_decider(cfg: Config, helper: Any = None) -> Decider:
    """The decider named by ``decider.kind``, or with ``auto`` the first one this Mac can actually reach.

    Every step needs a decision, so the decider is the one part with no fallback: no key, no network, that
    vendor down, and nothing runs. The entry point for a second one existed, an implementation was written
    for it — and reaching it still meant editing a config file first, which is not a fallback, it is a
    manual recovery. ``auto`` tries each in turn and says in the error what it tried.

    The order is a preference, not a tie: they are not equivalent. Jev returns calibrated probabilities and
    every threshold in `config.yaml` is a cut-off on those; a text model's "0.9" is a word it wrote, which is
    why `decider.local.confidence_ceiling` keeps one from releasing an action the safety floor flagged.
    """
    if cfg.get("audit.dry_run"):
        return DryRunDecider()
    kind = cfg.get("decider.kind", "jev")
    if kind != "auto":
        return _build(kind, cfg, helper)
    tried: list[str] = []
    found: list[Decider] = []
    for name in cfg.get("decider.auto_order") or ["jev", "local"]:
        try:
            found.append(_build(name, cfg, helper))
        except DeciderError as exc:
            tried.append(f"{name}: {exc}")
        except Exception as exc:                        # noqa: BLE001  (a backend that will not import or connect)
            tried.append(f"{name}: {type(exc).__name__}: {exc}")
    if not found:
        raise DeciderError("no decider is reachable — " + " | ".join(tried))
    if tried:
        log.warning("decider: %s unavailable (%s)", len(tried), "; ".join(tried))
    log.info("decider: %s", " > ".join(getattr(d, "name", "?") for d in found))
    return found[0] if len(found) == 1 else ChainDecider(found)
