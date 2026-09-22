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
STEP_ITEM = {"type": "object", "properties": {"goal": {"type": "string"}, "evidence": {"type": "string"}},
             "required": ["goal", "evidence"], "additionalProperties": False}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": STEP_ITEM},   # a sub-goal, and what the screen shows once it is done
        "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
        "try": {"type": "array", "items": TRY_ITEM},   # concrete moves to attempt now, in order
        "blocked": {"type": "string"},                  # non-empty: only the user can unblock this (and how)
    },
    "required": ["steps", "inputs", "try", "blocked"],
    "additionalProperties": False,
}


def _steps(raw: Any, limit: int) -> tuple[list[str], list[str]]:
    """Sub-goals and, for each, the evidence that shows it done — from a list of objects, or of plain
    strings from a planner (or a stored plan) that gave none. The two lists are always the same length."""
    goals: list[str] = []
    evidence: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            goal, seen = str(item.get("goal") or item.get("sub_goal") or item.get("step") or "").strip(), \
                str(item.get("evidence") or item.get("result") or "").strip()
        else:
            goal, seen = str(item).strip(), ""
        if goal:
            goals.append(goal)
            evidence.append(seen)
    return goals[:limit], evidence[:limit]


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
from .words import WORDS_SCHEMA  # noqa: E402

TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
DONE_SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}, "why": {"type": "string"}},
               "required": ["done", "why"], "additionalProperties": False}


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
        self.spare: list[str] = []     # other models the server listed, tried in turn when it refuses this one
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
        # A local server lists every model cached on this Mac, not the one it has loaded: this Mac listed a
        # vision model and a TTS model ahead of the served one, and naming the first row made every plan 404.
        # So an unnamed model stays unnamed — the server answers with whatever it is serving — and the list
        # is kept only as candidates for a server that insists on being told (LM Studio, Ollama).
        try:
            data = self._post("/models", None, 2).get("data") or []
        except (OSError, urllib.error.URLError, ValueError):
            return False
        if not self.model:
            self.spare = [m.get("id", "") for m in data if m.get("id")]
        return bool(data) or bool(self.model)

    def _next_model(self) -> bool:
        """Name a model the server listed, after it refused the request as it stood. False: nothing left."""
        if not self.spare:
            return False
        self.model = self.spare.pop(0)
        log.info("planner %s: the server wants a model named; trying %s", self.name, self.model)
        return True

    def warm(self) -> None:
        """A tiny request so a local model's weights are resident before a real plan is needed — and so a
        model the server will not serve is found now rather than in the middle of a task."""
        for _ in range(len(self.spare) + 1):
            try:
                self._post("/chat/completions", {**({"model": self.model} if self.model else {}),
                                                 "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1}, self.timeout)
                return
            except urllib.error.HTTPError as exc:
                if exc.code not in (400, 404) or not self._next_model():   # not "no such model here": leave it be
                    return
            except Exception:  # noqa: BLE001  (best effort)
                return

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        example = {k: ([] if v.get("type") == "array" else {} if v.get("type") == "object" else "") for k, v in schema.get("properties", {}).items()}
        body: dict[str, Any] = {"messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\nReply with one JSON object only, shaped like this example: {json.dumps(example)}"}],
            **self.extra}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        last: Exception | None = None
        for _ in range(2 + len(self.spare)):   # JSON mode may occasionally return empty content: ask once more
            if self.model:
                body["model"] = self.model
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
                if exc.code in (400, 404) and not self._next_model():   # this server cannot serve this model
                    break
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
    local = True      # it says so itself; nothing else should be deciding this from its name

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
        self.tried: list[str] = []      # who saw the last prompt, in order — a refused one read it too

    def __getattr__(self, attr: str) -> Any:   # name, warm…: those of the planner currently in front
        return getattr(self.planners[0], attr)

    @property
    def members(self) -> list[Any]:
        """Everyone who could end up answering. `local` on the chain reports only whoever is in front, and
        the fall-through happens *during* a call, so anything deciding what may be sent has to look at
        all of them."""
        return list(self.planners)

    @property
    def local(self) -> bool:
        return all(bool(getattr(p, "local", False)) for p in self.planners)

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.tried = []
        while True:
            head = self.planners[0]
            self.tried.append(str(getattr(head, "name", "?")))
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

    def __init__(self, cfg: Config, planner: Planner, redactor: Any = None, audit: Any = None, task: str | None = None) -> None:
        self.cfg = cfg
        self.p = planner
        self.redactor = redactor
        self.audit = audit
        self.task = task          # the audit record of every exchange names the task it was for
        self.max_steps = int(cfg.get("planner.max_steps", 8))

    @property
    def on_device(self) -> bool:
        """May this prompt stay as it is?

        Worked out per call, and over the whole chain. It used to be settled once in `__init__` from
        whoever was in front — and a chain drops a planner whose credentials are rejected and lets the
        next one answer, so a prompt built for a local model went to a cloud model in the clear. The
        fall-through happens *during* the call, so the only safe reading is "nothing in this chain can
        reach off the Mac".
        """
        # just `local`, and nothing else. It used to also accept `name == "foundation"` — and a Chain
        # forwards `name` to whoever is in front, so a chain headed by the on-device model reported itself
        # on-device however many cloud planners stood behind it. A planner says whether it is on this Mac.
        return bool(getattr(self.p, "local", False))

    def _ask(self, prompt_key: str, schema: dict[str, Any], **fields: Any) -> dict[str, Any]:
        if self.redactor is not None and not (self.on_device and self.cfg.get("planner.trust_on_device", True)):
            fields = self.redactor.value(fields)
        system = self.cfg.question("planner_system")
        prompt = self.cfg.question(prompt_key).format(**{k: json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v for k, v in fields.items()})
        out: dict[str, Any] | None = None
        try:
            out = self.p.complete(system, prompt, schema)
        finally:
            # after the call, not before: which planner answered is only known once it has. A refused one
            # read the prompt before refusing, so it belongs in the record too — and so does the answer:
            # a fill that came back empty was indistinguishable in the record from one never made.
            if self.audit is not None:
                tried = list(getattr(self.p, "tried", []) or [str(getattr(self.p, "name", "?"))])
                self.audit.record("plan", task=self.task, answered_by=tried[-1] if tried else "?", tried=tried, prompt=prompt,
                                  answer=json.dumps(out, ensure_ascii=False)[:2000] if out is not None else None)
        return self.redactor.restore(out) if self.redactor is not None else out

    def floor_words(self, language: str, categories: dict[str, str]) -> dict[str, list[str]]:
        """The words that mean each floor category in one interface language: asked once per language, kept
        on disk by `words.FloorWords`. Nothing of the user's is in the question."""
        out = self._ask("floor_words", WORDS_SCHEMA, language=language, categories=categories)
        words = out.get("words") if isinstance(out, dict) else None
        return {k: [str(w) for w in v if str(w).strip()] for k, v in (words or {}).items() if k in categories and isinstance(v, list)}

    def plan(self, goal: str, context: dict[str, Any]) -> dict[str, Any]:
        out = self._ask("planner_plan", PLAN_SCHEMA, goal=goal, context=context)
        steps, evidence = _steps(out.get("steps"), self.max_steps)
        return {"steps": steps, "evidence": evidence, "inputs": {str(k): str(v) for k, v in (out.get("inputs") or {}).items()},
                "try": _tries(out.get("try"))[: self.max_steps], "blocked": str(out.get("blocked") or "").strip()}

    def replan(self, goal: str, context: dict[str, Any], done: list[str], problem: str) -> dict[str, Any]:
        out = self._ask("planner_replan", PLAN_SCHEMA, goal=goal, context=context, done=done, problem=problem)
        steps, evidence = _steps(out.get("steps"), self.max_steps)
        return {"steps": steps, "evidence": evidence, "inputs": {str(k): str(v) for k, v in (out.get("inputs") or {}).items()},
                "try": _tries(out.get("try"))[: self.max_steps], "blocked": str(out.get("blocked") or "").strip()}

    def answer(self, goal: str, context: dict[str, Any]) -> str:
        return str(self._ask("planner_answer", ANSWER_SCHEMA, goal=goal, context=context).get("answer", "")).strip()

    def judge_done(self, goal: str, context: dict[str, Any]) -> tuple[bool, str]:
        """A second opinion on "done", from a different model reading the same screen."""
        out = self._ask("planner_done", DONE_SCHEMA, goal=goal, context=context)
        return bool(out.get("done")), str(out.get("why") or "").strip()

    def fill(self, goal: str, step: str, slot: str, context: dict[str, Any]) -> str:
        return str(self._ask("planner_fill", TEXT_SCHEMA, goal=goal, step=step, slot=slot, context=context).get("text", ""))
