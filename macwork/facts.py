"""The working set: what this task has actually seen.

A task may only write what it saw. Values come from the caller's inputs, from the goal itself, or from a screen
the task looked at — never from a planner's own arithmetic or memory. That is what keeps "the Calculator is
still computing" from becoming "391 typed into the document".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WORD = re.compile(r"[0-9]+(?:[.,][0-9]+)*|[A-Za-z]+|[㐀-鿿]")


def pieces(text: str) -> list[str]:
    """Numbers, words and CJK characters — what a value is made of, for checking where it could have come from."""
    return _WORD.findall(text or "")


@dataclass
class Fact:
    app: str
    window: str
    text: str
    step: int

    def public(self) -> dict[str, Any]:
        return {"app": self.app, "window": self.window, "text": self.text, "after_step": self.step}


@dataclass
class Facts:
    """What each app showed while the task was in it, newest per app, plus the caller's own words."""
    seen: dict[str, Fact] = field(default_factory=dict)      # app name -> what it last showed
    inputs: dict[str, Any] = field(default_factory=dict)
    goal: str = ""
    limit: int = 600

    def record(self, app: str | None, window: str | None, text: str, step: int) -> None:
        if app and text.strip():
            self.seen[str(app)] = Fact(str(app), str(window or ""), text[: self.limit], step)

    def brief(self) -> dict[str, str]:
        return {app: f"{f.window}: {f.text}" if f.window else f.text for app, f in self.seen.items()}

    def source_of(self, text: str) -> str | None:
        """Where this text could have come from, or None when the task has never seen it. A value counts as seen
        when every number and word in it appears in one place the task may draw from (numbers must match exactly;
        that is what makes an invented result stand out)."""
        want = pieces(text)
        if not want:
            return "no value"
        haystacks = {"the goal": self.goal, "the caller's inputs": " ".join(str(v) for v in self.inputs.values())}
        haystacks |= {f"{app} ({f.window})" if f.window else app: f.text for app, f in self.seen.items()}
        for name, hay in haystacks.items():
            if not hay:
                continue
            have = set(pieces(hay))
            if all(w in have for w in want):
                return name
        joined = " ".join(h for h in haystacks.values() if h)
        have = set(pieces(joined))
        return "several screens" if all(w in have for w in want) else None
