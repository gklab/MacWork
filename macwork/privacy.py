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
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config, expand

log = logging.getLogger(__name__)

Entities = Callable[[list[str]], list[list[dict[str, Any]]]]   # many texts -> entities per text (one round trip)
Detect = Callable[[list[str]], list[list[dict[str, Any]]]]     # the same shape, for phone numbers and addresses
# Script-agnostic. These used to be written for CJK and Latin only, which did not merely miss names in other
# scripts — it stopped the text from ever reaching the tagger: a Russian, Korean, Greek or Arabic clause was
# judged to contain nothing name-like and skipped.
_CLAUSE = re.compile(r"[^\w\s'’\-]+|[\n\t]+|⟦[^⟧]*⟧")     # any punctuation, in any script


def _caseless(c: str) -> bool:
    """A letter from a script with no upper/lower distinction — Chinese, Japanese, Korean, Arabic, Hebrew,
    Thai, Devanagari. `isupper` says nothing about those, so a capital-letter test cannot see them."""
    return c.isalpha() and c.lower() == c.upper()


def _nameish(s: str) -> bool:
    """Could a name be in here? A capital in any script, or any letter from a script without capitals."""
    return any(c.isupper() or _caseless(c) for c in s)


def _runs(text: str) -> list[str]:
    """Stretches of one kind of writing: letters that have capitals, and letters that do not.

    The tagger reads a clause as one language, so in "Meeting with 王芳 at 3pm" it sees English and misses
    the Chinese name. Each stretch is offered on its own as well. Written out rather than as a pattern
    because "is this script caseless" is not something a character class can ask.
    """
    out: list[str] = []
    cur: list[str] = []
    kind: str | None = None
    for ch in text:
        this = "caseless" if _caseless(ch) else ("cased" if ch.isalpha() else None)
        if this is None:
            if ch in " '’.-" and cur:          # keeps "Grace Lee" and "de la Cruz" in one piece
                cur.append(ch)
            elif cur:
                out.append("".join(cur).strip())
                cur, kind = [], None
            continue
        if kind and this != kind:
            out.append("".join(cur).strip())
            cur = []
        cur.append(ch)
        kind = this
    if cur:
        out.append("".join(cur).strip())
    return [r for r in out if len(r) >= 2]


_CARRIERS = {"cjk": "我和{}开会。", "latin": "I met {} yesterday."}   # a name alone is often not recognized as one


def _carrier(run: str) -> str:
    """A run inside a neutral sentence: the tagger needs a little context to see a name in a bare fragment.

    Only two carriers, and a run in a script neither covers gets the Latin one. That is a real limit — but a
    carrier only helps where the on-device tagger knows the language at all, and writing one sentence per
    script would be inventing coverage that NaturalLanguage does not have. What the fixes around this do buy
    is that such text now *reaches* the tagger instead of being skipped before it.
    """
    return _CARRIERS["cjk" if any(_caseless(c) for c in run) else "latin"].format(run)


def _short_name_like(c: str) -> bool:
    """A fragment short enough to be just a name — in a script with capitals, or one without."""
    caseless = c.strip()
    if 2 <= len(caseless) <= 5 and caseless and all(_caseless(ch) for ch in caseless):
        return True
    words = c.split()
    return 1 <= len(words) <= 3 and all(w[:1].isupper() and len(w) > 1 for w in words)


class RedactionError(RuntimeError):
    """Nothing could be checked for personal data, so nothing may be sent.

    Deliberately not a ``DeciderError``: every caller of the gate handles that one and carries on with a
    default, which is right when the decider cannot answer but wrong here — a task that keeps running with
    every string replaced by "[withheld]" is deciding blind. This one is meant to reach the engine and end
    the task.
    """


class Redactor:
    def __init__(self, cfg: Config, entities: Entities | None = None, protect: Callable[[], Any] | None = None,
                 detect: Detect | None = None) -> None:
        r = cfg.privacy.get("redact", {}) or {}
        self.detect = detect
        self.detect_kinds = set(r.get("detect") or [])
        self.max_clauses = int(r.get("max_clauses", 600))          # per call to the tagger
        self.max_clauses_total = int(r.get("max_clauses_total", 6000))   # past this, nothing is sent at all
        self.failed = False               # tagging broke: nothing may leave until the task is over
        self._lock = threading.RLock()    # the floor classification runs beside the step's own request, on one table
        self.protect = protect            # names from this Mac itself (installed apps…) that are never personal data
        self._protected: list[str] | None = None
        self._tagged: set[str] = set()   # clauses already tagged in this task (labels repeat every step)
        self._detected: set[str] = set()
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
        return _nameish(rest)

    def _learn(self, strings: list[str]) -> None:
        """Tag names clause by clause (the tagger misses names in long mixed text) in one batch; found names join
        the pseudonym table unless they belong to this Mac's own vocabulary (app names)."""
        self._detect(strings)
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
                # mixed scripts: the tagger reads a clause as one language, so each run is offered separately
                mixed = any(_caseless(ch) for ch in c) and any(ch.isalpha() and not _caseless(ch) for ch in c)
                runs = _runs(c) if mixed else ([c] if _short_name_like(c) else [])
                for piece in [c] + [_carrier(r) for r in runs if len(r) >= 2]:
                    if len(piece) >= 2 and piece not in seen and piece not in self._tagged and self._worth_tagging(piece):
                        seen.add(piece)
                        clauses.append(piece)
        if not clauses:
            return
        # `max_clauses` used to cut the list here and send the rest untagged — no error, no flag, no note,
        # so a long screen simply stopped being redacted partway through. It is a batch size now: every
        # clause is checked, in as many calls as that takes.
        if len(clauses) > self.max_clauses_total:
            # There has to be an end to it somewhere, and when it is reached this behaves like the tagger
            # failing — loudly, withholding everything — rather than like it succeeding on a part.
            log.warning("%d clauses in one request is more than the ceiling (redact.max_clauses_total=%d); "
                        "withholding all text rather than sending the rest unchecked",
                        len(clauses), self.max_clauses_total)
            self.failed = True
            return
        found: list[Any] = []
        for i in range(0, len(clauses), max(1, self.max_clauses)):
            batch = clauses[i: i + max(1, self.max_clauses)]
            try:
                found += list(self.entities(batch))
            except Exception as exc:  # noqa: BLE001  (redaction must never block, but it must not leak either)
                log.warning("entity tagging failed (%s); withholding all text", exc)
                self.failed = True
                return
        self._tagged.update(clauses)
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

    def _detect(self, strings: list[str]) -> None:
        """Phone numbers and addresses, found the way the system finds them.

        The patterns in privacy.yaml stay as a second source — they catch a German street the detector misses
        — but they cannot be the only one: written out by hand they knew mainland-Chinese mobile numbers and
        North American ones, so a German, Japanese or Brazilian number reached the decider in the clear.
        """
        if not (self.enabled and self.detect and self.detect_kinds):
            return
        todo = [s for s in dict.fromkeys(strings) if s and s not in self._detected]
        if not todo:
            return
        try:
            found = self.detect(todo)
        except Exception as exc:  # noqa: BLE001  (the patterns still stand; a failure here is not a leak)
            log.info("data detection unavailable (%s)", exc)
            return
        self._detected.update(todo)
        for per_text in found:
            for hit in per_text:
                text = str(hit.get("text", "")).strip()
                if hit.get("type") in self.detect_kinds and len(text) >= 4 and not self._is_protected(text):
                    self._token(str(hit["type"]), text)

    def _redact(self, s: str) -> str:
        if self.failed:
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
        with self._lock:
            self._learn([s])
            return self._redact(s)

    def value(self, v: Any) -> Any:
        """Redact every string inside a JSON-like value (one entity pass for all of it); dict keys are our own ids."""
        if not self.enabled:
            return v
        with self._lock:
            self._learn(list(_strings(v)))
            return self._apply(v)

    def restore(self, v: Any) -> Any:
        """Put the real values back into text that came back from outside (a planner's typed text)."""
        with self._lock:
            return self._restore(v)

    def _restore(self, v: Any) -> Any:
        if isinstance(v, str):
            for original, token in self.table.items():
                v = v.replace(token, original)
            return v
        if isinstance(v, dict):
            return {k: self._restore(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._restore(x) for x in v]
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
    """The local record of everything that was sent, as it was sent.

    It holds redacted screen text, and redaction is best effort, so it is written 0600 and rotated: a
    behaviour log of the user's own Mac should not be world-readable, nor grow until the disk is full.
    """

    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(cfg.get("audit.enabled", True))
        self.path: Path | None = expand(cfg.get("audit.path"))
        self.dry_run = bool(cfg.get("audit.dry_run", False))
        self.max_bytes = int(cfg.get("audit.max_bytes", 64 * 1024 * 1024))
        self.keep = int(cfg.get("audit.keep", 3))
        self._lock = threading.Lock()   # the floor thread and the tidy thread write here too

    def _rotate(self, path: Path) -> None:
        """audit.jsonl -> audit.1.jsonl -> … -> audit.<keep>.jsonl; the oldest falls off the end."""
        if self.max_bytes <= 0 or path.stat().st_size < self.max_bytes:
            return
        for n in range(self.keep, 0, -1):
            newer = path if n == 1 else path.with_suffix(f".{n - 1}.jsonl")
            if newer.exists():
                newer.replace(path.with_suffix(f".{n}.jsonl"))   # replace overwrites: nothing to unlink first
        if self.keep <= 0:
            path.unlink(missing_ok=True)

    def record(self, kind: str, **data: Any) -> None:
        if not (self.enabled and self.path):
            return
        line = json.dumps({"ts": round(time.time(), 3), "kind": kind, **data}, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.exists():
                    self._rotate(self.path)
            except OSError as exc:       # a log that cannot rotate must not stop the task
                log.warning("audit: could not rotate %s (%s)", self.path, exc)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)


class Gate:
    """The only door to the decider: redact -> audit -> decide."""

    def __init__(self, decider: Any, audit: Audit) -> None:
        self.decider = decider
        self.audit = audit

    def decide(self, redactor: Redactor, state: Any, questions: dict[str, dict[str, Any]], task: str = "") -> dict[str, dict[str, Any]]:
        """Everything that goes out goes through one entity pass — the state, the options, *and* the wording.

        The wording was the hole. It reads like fixed prose from `questions.yaml`, but twelve call sites
        interpolate a live UI string into it: the safety floor puts the action's own label in
        (`policy.py`), tidy puts an app and a window title in, learn puts an action in. A label carries
        what a field holds by construction — `observe` appends `(now: …)` — and a row is labelled by the
        text it shows. So one request went out with the same address pseudonymised in `state` and in the
        clear in `instructions`, several times per step, for the life of the project.

        One pass over the whole request, not three: the same person has to get the same token everywhere in
        it, or the decider is shown two names for one thing and its answer is about neither.
        """
        crit = {k: q["criteria"] for k, q in questions.items() if "criteria" in q}
        says = {k: q["instructions"] for k, q in questions.items() if "instructions" in q}
        safe = redactor.value({"state": state, "criteria": crit, "instructions": says})
        safe_state = safe["state"]
        safe_questions = {k: {**q,
                              **({"criteria": safe["criteria"][k]} if k in crit else {}),
                              **({"instructions": safe["instructions"][k]} if k in says else {})}
                          for k, q in questions.items()}
        if redactor.failed:   # tagging broke: every string is "[withheld]" — sending that is deciding blind
            self.audit.record("refused", task=task, why="entity tagging failed; nothing was sent")
            raise RedactionError("personal data could not be checked for, so nothing was sent")
        self.audit.record("decide", task=task, state=safe_state, questions=safe_questions, dry_run=self.audit.dry_run)
        answers = self.decider.decide(safe_state, safe_questions)
        self.audit.record("answers", task=task, answers={k: {kk: vv for kk, vv in a.items() if kk != "probabilities"} for k, a in answers.items()})
        return answers
