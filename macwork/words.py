"""The floor's words in the language this Mac is set to — derived once from what each category means, never
written for one language.

The word lists in policy.yaml are a prior: a hit is evidence of risk, and evidence is allowed to raise the bar
a release has to clear. They were written in two languages, so on a German or Japanese Mac the evidence was
never there and the floor was one bar lower for every deleting, sending and paying action. Writing a third
list would fix a third language. Instead, the lists are *derived*: the Mac says which languages its interface
uses (`system.locale`), and for each one the lists do not already cover, the planner is asked once for the words
that mean each category in that language. The answer is kept on disk beside the grants and never asked for
again. No planner reachable: the seed lists, logged — the honest degradation, never a licence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

WORDS_SCHEMA = {"type": "object",
                "properties": {"words": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}},
                "required": ["words"], "additionalProperties": False}

Derive = Callable[[str, dict[str, str]], dict[str, list[str]] | None]


def language_of(tag: str) -> str:
    """`de-DE`, `zh-Hans-CN`, `en_US` → the language alone."""
    return str(tag or "").replace("_", "-").split("-")[0].lower()


def languages_wanted(system: dict[str, Any] | None, covered: list[str]) -> list[str]:
    """The interface languages of this Mac the seed lists say nothing about, most preferred first."""
    out: list[str] = []
    for tag in (system or {}).get("languages") or []:
        lang = language_of(tag)
        if lang and lang not in covered and lang not in out:
            out.append(lang)
    return out


def pattern_for(word: str) -> str:
    """One word as one pattern. A script with letter case has word boundaries worth keeping (`\bsenden\b`, not
    `Absender`); a script without — CJK, Thai — has none a regex could find, so the word matches wherever it is."""
    w = re.sub(r"\s+", " ", str(word or "")).strip()
    if not w:
        return ""
    cased = w[0].upper() != w[0].lower()
    return rf"(?i)\b{re.escape(w)}\b" if cased else re.escape(w)


class FloorWords:
    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir).expanduser()
        self._mem: dict[str, dict[str, list[str]] | None] = {}     # language → {category: [pattern]}; None: could not derive this run

    def path(self, lang: str) -> Path:
        return self.dir / f"floor-words.{lang}.json"

    def patterns(self, categories: dict[str, dict[str, Any]], languages: list[str], derive: Derive) -> dict[str, list[str]]:
        """Extra patterns per category for these languages: from memory, then disk, then one derivation each."""
        merged: dict[str, list[str]] = {}
        meanings = {name: str((c or {}).get("what") or name) for name, c in categories.items()}
        for lang in languages:
            got = self._for(lang, meanings, derive)
            for cat, pats in (got or {}).items():
                if cat in categories:
                    merged.setdefault(cat, []).extend(p for p in pats if p and p not in merged.get(cat, []))
        return merged

    def _for(self, lang: str, meanings: dict[str, str], derive: Derive) -> dict[str, list[str]] | None:
        if lang in self._mem:
            return self._mem[lang]
        p = self.path(lang)
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            self._mem[lang] = {k: [str(x) for x in v] for k, v in (saved.get("patterns") or {}).items()}
            return self._mem[lang]
        except (OSError, ValueError, AttributeError):
            pass
        words = None
        try:
            words = derive(lang, meanings)
        except Exception as e:      # a planner error is not a floor error: the seed words stand, and it is said
            log.warning("the floor's words for %r could not be derived (%s); the seed lists stand", lang, e)
        if not words:
            self._mem[lang] = None
            log.warning("%s the floor's words for %r; the seed lists stand",
                        "no planner is reachable to say" if words is None else "the planner had none of", lang)
            return None
        patterns = {cat: [pattern_for(w) for w in ws if pattern_for(w)] for cat, ws in words.items() if cat in meanings}
        self._mem[lang] = patterns
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".floor-words-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"language": lang, "words": words, "patterns": patterns}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except OSError as e:
            log.warning("the floor's words for %r were derived but could not be kept (%s)", lang, e)
        log.info("the floor's words for %r: %d categories, kept at %s", lang, len(patterns), p)
        return patterns
