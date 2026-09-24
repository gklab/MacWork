"""Nothing reaches the decider without passing through here, on this Mac.

* named entities (on-device NaturalLanguage via the helper) and configured patterns become stable
  pseudonyms like ⟦PERSON_1⟧ — the same value gets the same token for the whole task, so the decider can
  still tell things apart and choose between them — where the value stands as a word, and glued into a
  longer word only the way a name is (`Redactor._swap`), never cutting a name of this Mac's own in two;
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
from .model import joined_at, word_kind

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


def _in_carrier(piece: str) -> str | None:
    """The run a carrier sentence was built around, or None for a piece of real text."""
    for tmpl in _CARRIERS.values():
        head, tail = tmpl.split("{}")
        if piece.startswith(head) and piece.endswith(tail) and len(piece) > len(head) + len(tail):
            return piece[len(head): len(piece) - len(tail)]
    return None


def _short_name_like(c: str) -> bool:
    """A fragment short enough to be just a name — in a script with capitals, or one without."""
    caseless = c.strip()
    if 2 <= len(caseless) <= 5 and caseless and all(_caseless(ch) for ch in caseless):
        return True
    words = c.split()
    return 1 <= len(words) <= 3 and all(w[:1].isupper() and len(w) > 1 for w in words)


# A second neutral sentence, only for asking whether a value in a script without capitals is a name *on its
# own*. Of the 50 such values the tagger found on this Mac's screens (09-20..23), the carrier sentence alone
# called 45 a name and both sentences 38: the second turns away 次方根, 释义, 排列, 添加列 and three names of
# scripts (日文, 韩文, 波罗的海文), and all 401 synthetic Chinese full names pass both. Given names alone pay for
# it: of 117, 101 pass the carrier and 94 both.
_ALONE = "{}的电话是多少？"
_TOKEN = re.compile(r"⟦[^⟧]*⟧")


def _beside(s: str, i: int, j: int) -> tuple[str, str]:
    """What s[i:j] is glued to: the character on each side across which its word goes on (`joined_at`), or ""
    where the word ends there. ("", "") is a word of its own — 「美国」 in 「美国Y02」 is one, since a change of
    script ends a word; 「Saf」 in 「Safari」 and 「打开」 in 「打开最近使用」 are not."""
    return (s[i - 1] if joined_at(s, i) else "", s[j] if joined_at(s, j) else "")


def _count(tally: dict[str, dict[str, int]], why: str, token: str) -> None:
    """One more occurrence of `why` for the kind of value `token` stands for (⟦PERSON_3⟧ counts as PERSON)."""
    kind = token[1:-1].rsplit("_", 1)[0]
    per_kind = tally.setdefault(why, {})
    per_kind[kind] = per_kind.get(kind, 0) + 1


class RedactionError(RuntimeError):
    """Nothing could be checked for personal data, so nothing may be sent.

    Deliberately not a ``DeciderError``: every caller of the gate handles that one and carries on with a
    default, which is right when the decider cannot answer but wrong here — a task that keeps running with
    every string replaced by "[withheld]" is deciding blind. This one is meant to reach the engine and end
    the task.
    """


class Redactor:
    def __init__(self, cfg: Config, entities: Entities | None = None, protect: Callable[[], Any] | None = None,
                 detect: Detect | None = None, identity: Callable[[], list[str]] | None = None) -> None:
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
        self._skipped: set[str] = set()  # …and the ones found to hold nothing name-like, which repeat just as much
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
        self._order: list[str] = []              # the table, longest first (sorted again only when it grows)
        self._places: dict[str, set[tuple[str, str]]] = {}   # value -> what it was glued to where it was found
        self._found: dict[str, list[dict[str, Any]]] = {}    # tagged piece -> what the tagger found in it
        self._placed: set[str] = set()           # clauses whose finds have been placed
        self._alone: dict[str, bool] = {}        # value without capitals -> a name even alone, in both sentences
        self._context: set[str] = set()          # values the tagger found inside running text longer than they are
        self._anywhere: set[str] = set()         # hidden wherever they occur, glued into a word or not
        for name in r.get("names") or []:        # names the user always wants hidden, whatever the tagger thinks
            self._token("PERSON", str(name))
            self._anywhere.add(str(name))
        self.identity = identity                 # this Mac's serial number and hardware UUID, asked of the Mac
        self._mine: list[str] = []
        self._know_mine()

    def _know_mine(self) -> None:
        """This Mac's own identifiers, hidden wherever they occur — and asked for again while none is known.

        They were asked for once, when the redactor was built. A task's redactor is built for each task, but the
        engine's own for what mac_observe and mac_act hand a caller lives as long as the process: built while
        the helper could not say them (an older one does not know the question), it went on sending the serial
        number after the helper was rebuilt and restarted. The engine keeps an answer once it has one, so asking
        again costs a lookup; while the helper cannot say, it costs the question, once per request."""
        if self._mine or self.identity is None:
            return
        try:
            mine = [str(v) for v in (self.identity() or []) if v]
        except Exception:  # noqa: BLE001  (nothing to add; what the tagger and the patterns find still stands)
            return
        self._mine = sorted(mine, key=len, reverse=True)
        for value in mine:
            self._token("DEVICE", value)
            self._anywhere.add(value)

    def _token(self, kind: str, value: str) -> str:
        if value not in self.table:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self.table[value] = f"⟦{kind}_{self._counts[kind]}⟧"
        return self.table[value]

    def _place(self, value: str, where: str) -> None:
        """Remember what `value` was glued to where the tagger or the detector found it (see `_beside`)."""
        i = where.find(value)
        while i != -1:
            glue = _beside(where, i, i + len(value))
            if glue != ("", ""):
                self._places.setdefault(value, set()).add(glue)
            i = where.find(value, i + 1)

    def _load_protected(self, again: bool = False) -> None:
        if self._protected is not None and not again:
            return
        try:
            raw = list(self.protect() if self.protect else [])
            names = sorted({str(n) for n in raw if n and len(str(n)) >= 2}, key=len, reverse=True)
        except Exception:  # noqa: BLE001
            raw, names = [], []
        self._protected_from = len(raw)
        self._protected = [n.casefold() for n in names]
        self._protected_set = set(self._protected)
        self._protected_words = [tuple(w for w in re.split(r"[^\w]+", n) if w) for n in self._protected]
        self._protected_rx = re.compile("|".join(map(re.escape, names)), re.I) if names else None

    def _is_protected(self, t: str) -> bool:
        """Is this the Mac's own vocabulary rather than somebody's name?

        It used to ask whether the text is a *substring* of any installed app's name, so "Mai" was taken
        for "Mail", "Nu" for "Numbers", "Bo" for "Books" — and anyone called those was never redacted.
        Short names in scripts without capitals are exactly the ones the tagger is least sure about, so
        the substring rule was silently strongest where the redaction was weakest.

        A whole app name, or a whole word of a multi-word one ("Activity Monitor" also protects
        "Monitor"), or a run of whole words of one: the tagger took "Look Up" of the Service 「Look Up in
        Dictionary」 for a person, and the floor, shown 「⟦PERSON_1⟧ in Dictionary」, could not say what the
        action did and stopped a real task to ask. Nothing shorter than a word: a fragment is not a name.
        """
        if t in self.keep:
            return True
        self._vocabulary()
        low = t.casefold().strip()
        if not low:
            return False
        for name in self._protected or []:
            if low == name or (len(low) >= 3 and low in re.split(r"[^\w]+", name)):
                return True
        run = tuple(w for w in re.split(r"[^\w]+", low) if w)
        if len(run) >= 2:
            for words in self._protected_words:
                if any(words[i:i + len(run)] == run for i in range(len(words) - len(run) + 1)):
                    return True
        return False

    def _vocabulary(self) -> None:
        self._load_protected()
        try:
            # The Mac's vocabulary grows once the Services have been read behind the first look, which is
            # after this task's first request: asked about a name, look again if there is more of it now.
            if self.protect is not None and len(self.protect()) != self._protected_from:
                self._load_protected(again=True)
        except Exception:  # noqa: BLE001  (the list stays as it was)
            pass

    def _names_in(self, s: str) -> list[tuple[int, int]]:
        """Where this Mac's own names (its apps, their Services) stand in `s` as words of their own. 「Mail」
        inside 「gmail」 is not the app, and must not keep a name next to it from being hidden.

        The pattern tries the longest name first. Where that one is glued on, a shorter name it begins with can
        still stand as a word: 「Safari浏览器扩展…」 holds the app 「Safari」 and not 「Safari浏览器」, and a piece
        learned from a cut label, 「Safar」, went out there as 「⟦PERSON_5⟧i浏览器扩展…」 — 27 times in 12 of the
        looks replayed from 09-20..23 — until each place a name starts was asked for the longest one standing.
        """
        self._vocabulary()
        if not self._protected_rx:
            return []
        spans: list[tuple[int, int]] = []
        m = self._protected_rx.search(s)
        while m:
            i, found, after = m.start(), m.group(0), m.start() + 1
            for k in range(len(found), 1, -1):          # the longest of this Mac's names that begins here as a word
                if (k == len(found) or found[:k].casefold() in self._protected_set) and _beside(s, i, i + k) == ("", ""):
                    spans.append((i, i + k))
                    after = i + k                        # a name inside this one is cut only where this one is
                    break
            m = self._protected_rx.search(s, after)
        return spans

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
        owners: dict[str, list[str]] = {}      # a clause seen for the first time -> the pieces tagged for it
        for s in strings:
            for kind, rx in self.patterns:   # patterned data (emails, phones) confuses the tagger: take it out first
                s = rx.sub(" ", s)
            for c in _CLAUSE.split(s):
                c = c.strip()
                if c in self._placed or c in owners:   # read, tagged and placed already: labels repeat every step
                    continue
                # The tagger reads a clause in one language. In mixed text ("Meeting with 张伟 at 3pm") it misses
                # the other script's names — and it makes them up: 「用 Numbers 打开」, cut off a goal by its
                # path, was read as English and its 「打开」 ("open") came back a person, so all 14 tasks of
                # 09-20..23 whose goal began so sent it as "用 Numbers ⟦PERSON_2⟧ ~/…" (232 step requests, the
                # number varying). Each run of one script is read on its own, in a sentence of its own script,
                # and a mixed clause is not read whole: privacy-check's corpus leaks the same 81 texts either way.
                mixed = any(_caseless(ch) for ch in c) and any(ch.isalpha() and not _caseless(ch) for ch in c)
                runs = _runs(c) if mixed else ([c] if _short_name_like(c) else [])
                pieces = ([] if mixed else [c]) + [_carrier(r) for r in runs if len(r) >= 2]
                if len(c) >= 2:
                    owners[c] = pieces
                for piece in pieces:
                    if len(piece) < 2 or piece in seen or piece in self._tagged or piece in self._skipped:
                        continue
                    # Both answers are remembered, not only "yes". A clause worth tagging went into
                    # `_tagged` and was skipped next step; one *not* worth it went nowhere, so every menu
                    # and button label was re-examined on every step against a regex made of every
                    # installed app's name — measured at 155 ms of the 163 ms the engine itself spends
                    # per step. The answer cannot change: the protected list is loaded once.
                    if not self._worth_tagging(piece):
                        self._skipped.add(piece)
                        continue
                    seen.add(piece)
                    clauses.append(piece)
        if not clauses and not owners:
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
        found = self._ask(clauses)
        if found is None:
            return
        self._tagged.update(clauses)
        for piece, per_text in zip(clauses, found):
            if per_text:
                self._found[piece] = list(per_text)
        # Where a value was found is part of what was found. A pseudonym used to replace its value as a raw
        # substring of every later string, so a value found in a clause cut up by the engine, or read in the
        # wrong language, went out inside whatever longer word held the same letters: 「Saf」 inside
        # 「Safari」, 「单元格」 inside the goal. Each find is placed in the real clause it came from.
        new: list[str] = []
        for real, pieces in owners.items():
            self._placed.add(real)
            for piece in pieces:
                run = _in_carrier(piece)             # entities in a carrier sentence must lie inside the run
                for e in self._found.get(piece, []):
                    t = str(e.get("text", "")).strip()
                    if e.get("type") not in self.kinds or len(t) < 2 or t not in (run or piece) or t not in real \
                            or self._is_protected(t):
                        continue
                    if t not in self.table and t not in new:
                        new.append(t)
                    self._token(str(e["type"]), t)
                    self._place(t, real)
                    if run is None and len(real) > len(t):   # found in running text, not in a sentence of ours
                        self._context.add(t)
        self._ask_alone([t for t in new if all(_caseless(ch) for ch in t if ch.isalpha())])

    def _ask(self, pieces: list[str]) -> list[Any] | None:
        """The tagger's answers for `pieces`, in batches of `max_clauses`; None, and nothing may be sent, if it
        failed."""
        found: list[Any] = []
        for i in range(0, len(pieces), max(1, self.max_clauses)):
            batch = pieces[i: i + max(1, self.max_clauses)]
            try:
                found += list(self.entities(batch))
            except Exception as exc:  # noqa: BLE001  (redaction must never block, but it must not leak either)
                log.warning("entity tagging failed (%s); withholding all text", exc)
                self.failed = True
                return None
        return found

    def _ask_alone(self, values: list[str]) -> None:
        """Is each of these a name even on its own? Asked once per value, in two neutral sentences.

        In a script written without spaces nothing marks where a word ends, so a name glued into running text
        (「来自吴军的消息」) can only be told from a word that shares its letters (「打开」 in 「打开最近使用」) by
        asking. Where the tagger found the value in that very text it is replaced there anyway; this is for
        text where it did not.
        """
        todo = [v for v in values if v not in self._alone]
        if not todo:
            return
        asked = self._ask([_carrier(v) for v in todo] + [_ALONE.format(v) for v in todo])
        if asked is None:
            return
        for k, v in enumerate(todo):
            self._alone[v] = all(any(e.get("type") in self.kinds and str(e.get("text", "")).strip() == v
                                     for e in asked[k + n * len(todo)]) for n in (0, 1))

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
        for where, per_text in zip(todo, found):
            for hit in per_text:
                text = str(hit.get("text", "")).strip()
                if hit.get("type") in self.detect_kinds and len(text) >= 4 and not self._is_protected(text):
                    self._token(str(hit["type"]), text)
                    self._place(text, where)

    def _redact(self, s: str, tally: dict[str, dict[str, int]] | None = None) -> str:
        if self.failed:
            return "[withheld]"
        if any(rx.search(s) for rx in self.never):
            return "[withheld]"
        for rx, to in self.replace:
            s = rx.sub(to, s)
        # This Mac's own identifiers go whole, before the patterns: a hardware UUID whose middle groups are
        # digits reads as a card number to them (「4A1B2C3D-1111-2222-3333-4444ABCDEF12」), and the rest of it
        # would go out in the clear around the card's pseudonym.
        for value in self._mine:
            s = s.replace(value, self.table[value])
        for kind, rx in self.patterns:
            s = rx.sub(lambda m, k=kind: m.group(0) if m.group(0) in self.keep else self._token(k, m.group(0)), s)
        return self._swap(s, {} if tally is None else tally)

    def _swap(self, s: str, tally: dict[str, dict[str, int]]) -> str:
        """Every value in the table, where it stands in `s`: as a word of its own, or glued into a longer word
        where `_glued_in` allows it. Never inside a pseudonym already there, and never cutting one of this Mac's
        own names in two.

        `tally` counts, by kind, what was left in place — "glued" (inside a longer word where it may not be
        replaced), "in_a_name" (it would cut a name of this Mac's) — and "replaced_glued": what was replaced
        although glued into a longer word. Counts only, never the values.
        """
        if len(self._order) != len(self.table):
            self._order = sorted(self.table, key=len, reverse=True)
        taken = [(m.start(), m.end()) for m in _TOKEN.finditer(s)]
        names: list[tuple[int, int]] | None = None
        out: list[tuple[int, int, str]] = []
        for v in self._order:
            token = self.table[v]
            i = s.find(v)
            while i != -1:
                j = i + len(v)
                if not any(a < j and i < b for a, b in taken):
                    glue = _beside(s, i, j)
                    ok = v in self._anywhere or glue == ("", "") or self._glued_in(v, s, j, glue)
                    if not ok:
                        _count(tally, "glued", token)
                    elif v not in self._anywhere:
                        names = self._names_in(s) if names is None else names
                        if any(a < i < b or a < j < b for a, b in names):
                            ok = False
                            _count(tally, "in_a_name", token)
                    if ok:
                        if glue != ("", ""):
                            _count(tally, "replaced_glued", token)
                        taken.append((i, j))
                        out.append((i, j, token))
                i = s.find(v, i + 1)
        for i, j, token in sorted(out, reverse=True):
            s = s[:i] + token + s[j:]
        return s

    def _glued_in(self, v: str, s: str, j: int, glue: tuple[str, str]) -> bool:
        """May `v`, glued into a longer word of `s` (it ends at j; `glue` is `_beside`), be replaced there?

        * Where it was found glued the same way: the tagger or the detector found it so.
        * A value without capitals: where it is a name even alone, in two neutral sentences, or was found
          inside running text longer than itself. Such a script writes a name glued to the words around it
          (「给一诺发消息」), and the tagger does not find it in every such sentence.
        * A value that begins with a capital, glued on the right only: where its word goes on by exactly one
          lowercase letter and ends there — a genitive or a plural written without an apostrophe
          (「Lars Andersens iPhone」).
        Nothing else: 「Saf」, learned from a label the engine cut, stays whole inside 「Safari」.
        """
        if glue in self._places.get(v, ()):
            return True
        if all(_caseless(ch) for ch in v if ch.isalpha()):
            return self._alone.get(v, False) or v in self._context
        if not v[:1].isupper() or glue[0]:
            return False
        k = j
        while joined_at(s, k):
            k += 1
        letters = [ch for ch in s[j:k] if word_kind(ch) != "mark"]   # an accent belongs to its letter
        return len(letters) == 1 and letters[0].islower()

    def text(self, s: str) -> str:
        if not self.enabled or not s:
            return s
        with self._lock:
            self._know_mine()
            self._learn([s])
            return self._redact(s)

    def value(self, v: Any, tally: dict[str, dict[str, int]] | None = None) -> Any:
        """Redact every string inside a JSON-like value (one entity pass for all of it); dict keys are our own ids.
        `tally`, if given, counts what was left in place and what was replaced glued (see `_swap`)."""
        if not self.enabled:
            return v
        with self._lock:
            self._know_mine()
            self._learn(list(_strings(v)))
            return self._apply(v, {} if tally is None else tally)

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

    def _apply(self, v: Any, tally: dict[str, dict[str, int]]) -> Any:
        if isinstance(v, str):
            return self._redact(v, tally) if v else v
        if isinstance(v, dict):
            return {k: self._apply(x, tally) for k, x in v.items()}
        if isinstance(v, list):
            return [self._apply(x, tally) for x in v]
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
        # Redacting the wording as a whole cost the wording. The tagger reads it as prose, and prose has
        # names in it: "Judge from what the words mean" — the floor's own instruction — went out as
        # "⟦PERSON_1⟧ from what the words mean". 5817 of 12288 questions in one run carried a pseudonym
        # where a word of ours had been. A question that names a live UI string now hands it over in
        # `fills` instead of interpolating it first: the template is ours and goes as written, the value
        # is redacted with everything else, and the two are put together here.
        says = {k: q["instructions"] for k, q in questions.items() if "instructions" in q and "fills" not in q}
        fills = {k: q["fills"] for k, q in questions.items() if "fills" in q}
        # What a pseudonym was not put in for, by kind: a known value left inside a longer word, or inside one
        # of this Mac's own names — and what was put in although glued into a longer word. Counted on every
        # request, next to what was sent, so the price of not cutting words is on the record.
        kept: dict[str, dict[str, int]] = {"glued": {}, "in_a_name": {}, "replaced_glued": {}}
        safe = redactor.value({"state": state, "criteria": crit, "instructions": says, "fills": fills}, tally=kept)
        safe_state = safe["state"]

        def worded(k: str, q: dict[str, Any]) -> str:
            text = q["instructions"]
            for name, value in (safe["fills"].get(k) or {}).items():
                text = text.replace("{" + name + "}", str(value))
            return text

        safe_questions = {k: {**{kk: vv for kk, vv in q.items() if kk != "fills"},
                              **({"criteria": safe["criteria"][k]} if k in crit else {}),
                              **({"instructions": safe["instructions"][k]} if k in says else {}),
                              **({"instructions": worded(k, q)} if k in fills else {})}
                          for k, q in questions.items()}
        if redactor.failed:   # tagging broke: every string is "[withheld]" — sending that is deciding blind
            self.audit.record("refused", task=task, why="entity tagging failed; nothing was sent")
            raise RedactionError("personal data could not be checked for, so nothing was sent")
        self.audit.record("decide", task=task, state=safe_state, questions=safe_questions, dry_run=self.audit.dry_run,
                          kept=kept)
        answers = self.decider.decide(safe_state, safe_questions)
        # The probabilities stay. They are not personal data, and they are exactly what decides whether
        # the safety floor released an action — a log that drops them cannot answer "why was this let
        # through", which is the one question anyone reads it for.
        self.audit.record("answers", task=task, answers=answers)
        return answers
