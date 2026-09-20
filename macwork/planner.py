"""Planners: the slow, deliberate System 2 next to Jev's System 1.

A planner turns a goal plus what is known about the app into a few sub-goals, writes the text a step needs
(Jev writes none), and re-plans when the fast loop gets stuck. It is consulted rarely — at the start of a
multi-step task, when a text slot is empty, and when Jev is unsure — so most steps stay fast and cheap.

Implementations (``planner.kind``), all optional:
  * ``none``        the caller plans (MCP clients such as Claude Code already are planners)
  * endpoints       any OpenAI-compatible chat API listed in ``planner.endpoints`` — DeepSeek's official API is
                    preset, so is a local server (LM Studio, Ollama, mlx_lm.server); add others in config
  * ``anthropic``   Claude through the official SDK (``pip install anthropic``)
  * ``foundation``  Apple's on-device Foundation Models, through the helper (macOS 26, eligible devices)
  * ``auto``        the first of ``planner.auto_order`` that is available
More plug in through the ``macwork.planners`` entry point. Every prompt lives in questions.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from importlib.metadata import entry_points
from typing import Any, Protocol

from .config import Config

log = logging.getLogger(__name__)

TRY_ITEM = {"type": "object", "properties": {"action": {"type": "string"}, "keys": {"type": "string"}, "type": {"type": "string"},
                                             "open_url": {"type": "string"},
                                             "drag": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
        "try": {"type": "array", "items": TRY_ITEM},   # concrete moves to attempt now, in order
        "blocked": {"type": "string"},                  # non-empty: only the user can unblock this (and how)
    },
    "required": ["steps", "inputs", "try", "blocked"],
    "additionalProperties": False,
}


def _tries(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        drag = t.get("drag")
        if isinstance(drag, list) and len(drag) == 2 and all(str(x).strip() for x in drag):
            out.append({"drag": [str(drag[0]).strip(), str(drag[1]).strip()]})
            continue
        item = {k: str(v).strip() for k, v in t.items() if k in ("action", "keys", "type", "open_url") and str(v).strip()}
        if len(item) == 1:
            out.append(item)
    return out
TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}


class PlannerError(RuntimeError):
    def __init__(self, msg: str, refused: bool = False) -> None:
        super().__init__(msg)
        self.refused = refused   # the credentials were rejected: this planner will not work until the user fixes them


class Planner(Protocol):
    name: str

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


def _json_from(text: str) -> dict[str, Any]:
    """The first JSON object in a model's reply (local models do not always honour a schema)."""
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break
        start = text.find("{", start + 1)
    if start := text.find("{") + 1:   # cut off by max_tokens: keep the complete items, close what is open
        body = text[start - 1:]
        for end in (m.end() for m in reversed(list(re.finditer(r'"\s*,?\s*\n|[}\]]\s*,?\s*\n', body)))):
            head = body[:end].rstrip().rstrip(",")
            closers, in_str, esc = [], False, False
            for ch in head:
                if in_str:
                    esc, in_str = (not esc and ch == "\\"), in_str and (esc or ch != '"')
                elif ch == '"':
                    in_str = True
                elif ch in "{[":
                    closers.append("}" if ch == "{" else "]")
                elif ch in "}]" and closers:
                    closers.pop()
            if in_str:
                continue
            try:
                return json.loads(head + "".join(reversed(closers)))
            except json.JSONDecodeError:
                continue
    raise PlannerError(f"no JSON in planner reply: {text[:200]!r}")


class OpenAICompatPlanner:
    """Any OpenAI-compatible chat endpoint, named in ``planner.endpoints``: DeepSeek's official API, or a local
    server (LM Studio, Ollama, mlx_lm.server, vLLM). Keys come from an env var or the Keychain, never a file."""

    def __init__(self, cfg: Config, name: str, conf: dict[str, Any]) -> None:
        from .decider import keychain_key

        self.name = name
        self.base = str(conf.get("base_url", "")).rstrip("/")
        self.model = conf.get("model") or ""
        self.timeout = float(conf.get("timeout_s", 60))
        self.json_mode = bool(conf.get("json_mode", False))
        self.max_tokens = conf.get("max_tokens")
        self.local = bool(conf.get("local", False))
        self.extra = conf.get("extra") or {}
        env = conf.get("api_key_env")
        self.key = (os.environ.get(env, "").strip() if env else "") or \
            (keychain_key(cfg.get("decider.keychain_service", "macwork"), conf["keychain_account"]) if conf.get("keychain_account") else "")

    def _post(self, path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
        handlers = [urllib.request.ProxyHandler({})] if self.local else []   # a local server is never reached through a proxy
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return json.loads(r.read())

    def available(self) -> bool:
        if not self.base:
            return False
        if not self.local:
            return bool(self.key and self.model)
        try:
            models = self._post("/models", None, 2).get("data") or []
        except (OSError, urllib.error.URLError, ValueError):
            return False
        if not self.model and models:
            self.model = models[0].get("id", "")
        return bool(models)

    def warm(self) -> None:
        """A tiny request so a local model's weights are resident before a real plan is needed."""
        try:
            self._post("/chat/completions", {"model": self.model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1}, self.timeout)
        except Exception:  # noqa: BLE001  (best effort)
            pass

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        example = {k: ([] if v.get("type") == "array" else {} if v.get("type") == "object" else "") for k, v in schema.get("properties", {}).items()}
        body: dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\nReply with one JSON object only, shaped like this example: {json.dumps(example)}"}],
            **self.extra}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        last: Exception | None = None
        for _ in range(2):   # JSON mode may occasionally return empty content: ask once more
            try:
                r = self._post("/chat/completions", body, self.timeout)
                content = (r.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if content.strip():
                    return _json_from(content)
                last = PlannerError("empty reply")
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise PlannerError(f"{self.name}: credentials rejected ({exc.code})", refused=True) from exc
                last = exc
            except (OSError, urllib.error.URLError, ValueError, PlannerError) as exc:
                last = exc
        raise PlannerError(f"{self.name}: {last}")


class AnthropicPlanner:
    """Claude via the official SDK, with a JSON schema on the output and server-side refusal fallback."""

    name = "anthropic"

    def __init__(self, cfg: Config) -> None:
        c = cfg.section("planner.anthropic")
        self.model = c.get("model", "claude-opus-5")
        self.effort = c.get("effort", "medium")
        self.max_tokens = int(c.get("max_tokens", 16000))
        self._client: Any = None

    def available(self) -> bool:
        try:
            import anthropic
        except ImportError:
            return False
        try:
            self._client = anthropic.Anthropic()
        except Exception:  # noqa: BLE001  (no credentials configured)
            return False
        return True

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        import anthropic

        if self._client is None and not self.available():
            raise PlannerError("anthropic SDK or credentials not available")
        try:
            r = self._client.beta.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"})
        except anthropic.APIStatusError as exc:
            raise PlannerError(f"anthropic {exc.status_code}: {exc.message}", refused=exc.status_code in (401, 403)) from exc
        except anthropic.APIConnectionError as exc:
            raise PlannerError(f"anthropic connection: {exc}") from exc
        if r.stop_reason == "refusal":
            raise PlannerError("planner declined the request")
        text = next((b.text for b in r.content if b.type == "text"), "")
        return json.loads(text)


class FoundationPlanner:
    """Apple's on-device model through the helper (the helper owns the Swift-only FoundationModels API)."""

    name = "foundation"

    def __init__(self, cfg: Config, helper: Any) -> None:
        self.helper = helper

    def available(self) -> bool:
        try:
            return bool(self.helper.call("llm.available").get("available"))
        except Exception:  # noqa: BLE001
            return False

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.helper.call("llm.generate", instructions=system, prompt=prompt + "\nReply with JSON matching: " + json.dumps(schema), timeout=120)
        except Exception as exc:  # noqa: BLE001
            raise PlannerError(f"foundation: {exc}") from exc
        return _json_from(r.get("text", ""))


def make_planner(cfg: Config, helper: Any) -> Planner | None:
    kind = cfg.get("planner.kind", "auto")
    if kind in (None, "none"):
        return None
    builders: dict[str, Any] = {"anthropic": lambda: AnthropicPlanner(cfg), "foundation": lambda: FoundationPlanner(cfg, helper)}
    for name, conf in (cfg.get("planner.endpoints") or {}).items():   # deepseek, local, or any you add
        builders[name] = lambda name=name, conf=conf: OpenAICompatPlanner(cfg, name, conf)
    for ep in entry_points(group="macwork.planners"):
        builders.setdefault(ep.name, lambda ep=ep: ep.load()(cfg))
    order = (cfg.get("planner.auto_order") or []) if kind == "auto" else [kind]
    found = []
    for name in order:
        make = builders.get(name)
        if make is None:
            continue
        p = make()
        if getattr(p, "available", lambda: True)():
            found.append(p)
    if not found:
        return None
    log.info("planner: %s", " > ".join(getattr(p, "name", "?") for p in found))
    return found[0] if len(found) == 1 else Chain(found)


def probe_planners(cfg: Config, helper: Any) -> list[dict[str, Any]]:
    """Every planner this configuration knows, asked for one tiny real plan: ok (with latency), credentials
    rejected, unreachable, or not configured. Nothing personal is sent."""
    import time as _time

    builders: dict[str, Any] = {"foundation": lambda: FoundationPlanner(cfg, helper), "anthropic": lambda: AnthropicPlanner(cfg)}
    for name, conf in (cfg.get("planner.endpoints") or {}).items():
        builders[name] = lambda name=name, conf=conf: OpenAICompatPlanner(cfg, name, conf)
    out = []
    for name, make in builders.items():
        try:
            p = make()
        except Exception as exc:  # noqa: BLE001
            out.append({"planner": name, "status": "not configured", "detail": str(exc)[:120]})
            continue
        if not getattr(p, "available", lambda: True)():
            out.append({"planner": name, "status": "not configured or unreachable"})
            continue
        t0 = _time.monotonic()
        try:
            plan = Planning(cfg, p).plan("open the Calculator app", {"app": "Finder", "actions_available": ["open app Calculator"]})
            out.append({"planner": name, "status": "ok", "ms": round((_time.monotonic() - t0) * 1000),
                        "model": getattr(p, "model", None), "steps": plan["steps"][:3]})
        except PlannerError as exc:
            out.append({"planner": name, "status": "credentials rejected" if exc.refused else "error", "detail": str(exc)[:160],
                        "ms": round((_time.monotonic() - t0) * 1000)})
    return out


class Chain:
    """Several usable planners in preference order. One whose credentials are rejected is dropped for the rest of
    the session and the next answers instead, so a stale key degrades planning rather than switching it off."""

    def __init__(self, planners: list[Any]) -> None:
        self.planners = planners

    def __getattr__(self, attr: str) -> Any:   # name, local, warm…: those of the planner currently in front
        return getattr(self.planners[0], attr)

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        while True:
            head = self.planners[0]
            try:
                return head.complete(system, prompt, schema)
            except PlannerError as exc:
                if not exc.refused or len(self.planners) == 1:
                    raise
                log.warning("planner %s: %s; using %s", getattr(head, "name", "?"), exc, getattr(self.planners[1], "name", "?"))
                self.planners.pop(0)


class Planning:
    """The engine's view of a planner: plan, fill a text slot, re-plan. Prompts come from questions.yaml.
    What goes to a planner that is not on this Mac is redacted and audited like everything sent to the decider;
    pseudonyms in what comes back are restored before anything is typed."""

    def __init__(self, cfg: Config, planner: Planner, redactor: Any = None, audit: Any = None) -> None:
        self.cfg = cfg
        self.p = planner
        self.redactor = redactor
        self.audit = audit
        self.max_steps = int(cfg.get("planner.max_steps", 8))
        self.on_device = bool(getattr(planner, "local", False)) or getattr(planner, "name", "") == "foundation"

    def _ask(self, prompt_key: str, schema: dict[str, Any], **fields: Any) -> dict[str, Any]:
        if self.redactor is not None and not (self.on_device and self.cfg.get("planner.trust_on_device", True)):
            fields = self.redactor.value(fields)
        system = self.cfg.question("planner_system")
        prompt = self.cfg.question(prompt_key).format(**{k: json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v for k, v in fields.items()})
        if self.audit is not None:
            self.audit.record("plan", planner=getattr(self.p, "name", "?"), prompt=prompt)
        out = self.p.complete(system, prompt, schema)
        return self.redactor.restore(out) if self.redactor is not None else out

    def plan(self, goal: str, context: dict[str, Any]) -> dict[str, Any]:
        out = self._ask("planner_plan", PLAN_SCHEMA, goal=goal, context=context)
        steps = [str(s).strip() for s in out.get("steps") or [] if str(s).strip()][: self.max_steps]
        return {"steps": steps, "inputs": {str(k): str(v) for k, v in (out.get("inputs") or {}).items()},
                "try": _tries(out.get("try"))[: self.max_steps], "blocked": str(out.get("blocked") or "").strip()}

    def replan(self, goal: str, context: dict[str, Any], done: list[str], problem: str) -> dict[str, Any]:
        out = self._ask("planner_replan", PLAN_SCHEMA, goal=goal, context=context, done=done, problem=problem)
        steps = [str(s).strip() for s in out.get("steps") or [] if str(s).strip()][: self.max_steps]
        return {"steps": steps, "inputs": {str(k): str(v) for k, v in (out.get("inputs") or {}).items()},
                "try": _tries(out.get("try"))[: self.max_steps], "blocked": str(out.get("blocked") or "").strip()}

    def answer(self, goal: str, context: dict[str, Any]) -> str:
        return str(self._ask("planner_answer", ANSWER_SCHEMA, goal=goal, context=context).get("answer", "")).strip()

    def fill(self, goal: str, step: str, slot: str, context: dict[str, Any]) -> str:
        return str(self._ask("planner_fill", TEXT_SCHEMA, goal=goal, step=step, slot=slot, context=context).get("text", ""))
