"""Nothing reaches the decider without passing through here, on this Mac.

* named entities (on-device NaturalLanguage via the helper) and configured patterns become stable
  pseudonyms like ⟦PERSON_1⟧ — the same value gets the same token for the whole task, so the decider can
  still tell things apart and choose between them;
* configured replacements (home paths -> ~) and ``never_send`` rules drop what must never leave;
* every request is appended to a local audit log (redacted, as sent).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config, expand

log = logging.getLogger(__name__)

Entities = Callable[[list[str]], list[list[dict[str, Any]]]]   # many texts -> entities per text (one round trip)
_NAMEISH = re.compile(r"[\u3400-\u9fff]|\b[A-Z][a-z]")   # something a name could be made of
_CLAUSE = re.compile(r"[，。；;、,.!?！？:：()（）\[\]\n\t]+|⟦[^⟧]*⟧")
_RUNS = re.compile(r"[\u3400-\u9fff]{2,}|[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*")   # runs of one script


_CARRIERS = {"cjk": "我和{}开会。", "latin": "I met {} yesterday."}   # a name alone is often not recognized as one


def _carrier(run: str) -> str:
    """A run of one script inside a neutral sentence of that script: the tagger needs a little context to see a
    name in it. Only entities found inside the run count (the carrier's own words are never redacted)."""
    return _CARRIERS["cjk" if re.search(r"[\u3400-\u9fff]", run) else "latin"].format(run)


def _short_name_like(c: str) -> bool:
    return bool(re.fullmatch(r"[\u3400-\u9fff]{2,4}", c) or re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}", c))


class Redactor:
    def __init__(self, cfg: Config, entities: Entities | None = None, protect: Callable[[], Any] | None = None) -> None:
        r = cfg.privacy.get("redact", {}) or {}
        self.max_clauses = int(r.get("max_clauses", 600))
        self.protect = protect            # names from this Mac itself (installed apps…) that are never personal data
        self._protected: list[str] | None = None
        self._tagged: set[str] = set()   # clauses already tagged in this task (labels repeat every step)
        self.enabled = bool(r.get("enabled", True))
        self.kinds = set(r.get("entities") or [])
        self.patterns = [(name, re.compile(rx)) for name, rx in (r.get("patterns") or {}).items()]
        self.replace = [(re.compile(rx), to) for rx, to in (r.get("replace") or {}).items()]
        self.never = [re.compile(rx) for rx in (r.get("never_send") or [])]
        self.keep = set(r.get("keep") or [])
        self.entities = entities
        self.table: dict[str, str] = {}          # original -> token (kept on this Mac only)
        self._counts: dict[str, int] = {}
        for name in r.get("names") or []:        # names the user always wants hidden, whatever the tagger thinks
            self._token("PERSON", str(name))

    def _token(self, kind: str, value: str) -> str:
        if value not in self.table:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self.table[value] = f"⟦{kind}_{self._counts[kind]}⟧"
        return self.table[value]

    def _load_protected(self) -> None:
        if self._protected is not None:
            return
        try:
            names = sorted({str(n) for n in (self.protect() if self.protect else []) if n and len(str(n)) >= 2}, key=len, reverse=True)
        except Exception:  # noqa: BLE001
            names = []
        self._protected = [n.casefold() for n in names]
        self._protected_rx = re.compile("|".join(map(re.escape, names)), re.I) if names else None

    def _is_protected(self, t: str) -> bool:
        if t in self.keep:
            return True
        self._load_protected()
        low = t.casefold()
        return any(low in name for name in self._protected or [])

    def _worth_tagging(self, clause: str) -> bool:
        """Skip clauses made only of this Mac's own vocabulary: nothing name-like is left once app names are removed."""
        self._load_protected()
        rest = self._protected_rx.sub(" ", clause) if self._protected_rx else clause
        return bool(_NAMEISH.search(rest))

    def _learn(self, strings: list[str]) -> None:
        """Tag names clause by clause (the tagger misses names in long mixed text) in one batch; found names join
        the pseudonym table unless they belong to this Mac's own vocabulary (app names)."""
        if not (self.enabled and self.entities and self.kinds):
            return
        clauses: list[str] = []
        seen: set[str] = set()
        for s in strings:
            for kind, rx in self.patterns:   # patterned data (emails, phones) confuses the tagger: take it out first
                s = rx.sub(" ", s)
            for c in _CLAUSE.split(s):
                c = c.strip()
                # the tagger reads a clause in one language: in mixed text ("Meeting with 张伟 at 3pm") the other
                # script's names are missed, so each run of one script is tagged on its own as well
                mixed = bool(re.search(r"[\u3400-\u9fff]", c)) and bool(re.search(r"[A-Za-z]{2}", c))
                runs = [r.strip() for r in _RUNS.findall(c)] if mixed else ([c] if _short_name_like(c) else [])
                for piece in [c] + [_carrier(r) for r in runs if len(r) >= 2]:
                    if len(piece) >= 2 and piece not in seen and piece not in self._tagged and self._worth_tagging(piece):
                        seen.add(piece)
                        clauses.append(piece)
        if not clauses:
            return
        try:
            found = self.entities(clauses[: self.max_clauses])
        except Exception as exc:  # noqa: BLE001  (redaction must never block, but it must not leak either)
            log.warning("entity tagging failed (%s); withholding all text", exc)
            self._failed = True
            return
        self._tagged.update(clauses[: self.max_clauses])
        carriers = {c: c for c in clauses}
        for tmpl in _CARRIERS.values():                  # entities in a carrier sentence must lie inside the run
            head, tail = tmpl.split("{}")
            for c in clauses:
                if c.startswith(head) and c.endswith(tail) and len(c) > len(head) + len(tail):
                    carriers[c] = c[len(head): len(c) - len(tail)]
        for clause, per_text in zip(clauses, found):
            for e in per_text:
                t = str(e.get("text", "")).strip()
                if e.get("type") in self.kinds and len(t) >= 2 and t in carriers[clause] and not self._is_protected(t):
                    self._token(str(e["type"]), t)

    def _redact(self, s: str) -> str:
        if getattr(self, "_failed", False):
            return "[withheld]"
        if any(rx.search(s) for rx in self.never):
            return "[withheld]"
        for rx, to in self.replace:
            s = rx.sub(to, s)
        for kind, rx in self.patterns:
            s = rx.sub(lambda m, k=kind: m.group(0) if m.group(0) in self.keep else self._token(k, m.group(0)), s)
        for original in sorted(self.table, key=len, reverse=True):
            if original in s:
                s = s.replace(original, self.table[original])
        return s

    def text(self, s: str) -> str:
        if not self.enabled or not s:
            return s
        self._learn([s])
        return self._redact(s)

    def value(self, v: Any) -> Any:
        """Redact every string inside a JSON-like value (one entity pass for all of it); dict keys are our own ids."""
        if not self.enabled:
            return v
        self._learn(list(_strings(v)))
        return self._apply(v)

    def restore(self, v: Any) -> Any:
        """Put the real values back into text that came back from outside (a planner's typed text)."""
        if isinstance(v, str):
            for original, token in self.table.items():
                v = v.replace(token, original)
            return v
        if isinstance(v, dict):
            return {k: self.restore(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.restore(x) for x in v]
        return v

    def _apply(self, v: Any) -> Any:
        if isinstance(v, str):
            return self._redact(v) if v else v
        if isinstance(v, dict):
            return {k: self._apply(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._apply(x) for x in v]
        return v


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


class Audit:
    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(cfg.get("audit.enabled", True))
        self.path: Path | None = expand(cfg.get("audit.path"))
        self.dry_run = bool(cfg.get("audit.dry_run", False))

    def record(self, kind: str, **data: Any) -> None:
        if not (self.enabled and self.path):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "kind": kind, **data}, ensure_ascii=False) + "\n")


class Gate:
    """The only door to the decider: redact -> audit -> decide."""

    def __init__(self, decider: Any, audit: Audit) -> None:
        self.decider = decider
        self.audit = audit

    def decide(self, redactor: Redactor, state: Any, questions: dict[str, dict[str, Any]], task: str = "") -> dict[str, dict[str, Any]]:
        crit = {k: q["criteria"] for k, q in questions.items() if "criteria" in q}
        safe = redactor.value({"state": state, "criteria": crit})   # one entity pass for the whole request
        safe_state = safe["state"]
        safe_questions = {k: {**q, "criteria": safe["criteria"][k]} if k in crit else q for k, q in questions.items()}
        self.audit.record("decide", task=task, state=safe_state, questions=safe_questions, dry_run=self.audit.dry_run)
        answers = self.decider.decide(safe_state, safe_questions)
        self.audit.record("answers", task=task, answers={k: {kk: vv for kk, vv in a.items() if kk != "probabilities"} for k, a in answers.items()})
        return answers
