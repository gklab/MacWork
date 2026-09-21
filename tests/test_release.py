"""A release verdict must not release an action it cannot honestly apply to.

`policy.yaml` has said since it was written that `enter` is a release "only for" typing. It was never
enforced where it mattered: `_floor` asked `_releases()` without the action, so a verdict of `enter` let
*any* verb through. The same slip ran through the score — the fraction of a classification that counts as
released was worked out once and cached under a key that is the app, the label and where it sits, which
does not tell a menu command apart from the file channel moving something to the Trash.

It matters more now than when it was written. `edit` was added to let the engine clear a calculator or
delete a word without stopping to ask, and the same round added an action that moves a file to the Trash.
"It only changes what this window holds" is a judgement about prose; "this is the channel that moves files"
is a fact about the action, and where a fact is available it is not the classifier's to overrule.
"""

from typing import Any

import pytest

from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import Ctx
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Says(ScriptedDecider):
    """A classifier that returns one verdict, with all its confidence on it."""

    def __init__(self, verdict: str, confidence: float = 1.0) -> None:
        super().__init__([])
        self.verdict, self.confidence = verdict, confidence
        self.offered: list[dict[str, str]] = []

    def decide(self, state, questions):
        out = {}
        for name, q in questions.items():
            self.offered.append(dict(q.get("criteria") or {}))
            out[name] = {"type": "choice", "choice": self.verdict,
                         "probabilities": {self.verdict: self.confidence}}
        return out


@pytest.fixture
def parts(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    ctx = eng._ctx("", {}, {"pid": 1, "name": "Editor", "bundle_id": "com.example.editor"}, "t")
    return eng, ctx


def floor(eng: Engine, ctx: Ctx, a: Affordance, verdict: str, confidence: float = 1.0) -> list[str]:
    eng._decider = Says(verdict, confidence)
    ctx.gate = eng.gate            # built from the decider just set; without one nothing is classified at all
    eng.cache.pop("floor.verdicts", None)
    return eng._floor("t", ctx, a, "Window")


def aff(channel: str, verb: str, label: str) -> Affordance:
    return Affordance("x", channel, verb, label, {}, context="Editor")


# --------------------------------------------------------------------------- the rule that was never enforced

def test_enter_does_not_release_something_that_is_not_typing(parts):
    """policy.yaml has said `enter: [type, type_submit]` all along; nothing checked it."""
    eng, ctx = parts
    assert floor(eng, ctx, aff("menu", "press", "click this one"), "enter") == ["enter"]


def test_enter_still_releases_typing(parts):
    eng, ctx = parts
    assert floor(eng, ctx, aff("keys", "type", "type the given text at the cursor"), "enter") == []


# --------------------------------------------------------------------------- the channel is a fact, not a judgement

def test_edit_does_not_release_the_channel_that_moves_files(parts):
    """The round that added `edit` also added moving a file to the Trash."""
    eng, ctx = parts
    # "Trash" is also a word the floor flags, so it is gated under that: what matters is that it is gated
    assert floor(eng, ctx, aff("file", "trash", "move 「old」 to the Trash"), "edit") == ["delete"]


@pytest.mark.parametrize("channel", ["file", "app", "script", "script_cmd", "service", "shortcut", "clipboard"])
def test_no_channel_that_reaches_past_the_window_is_released_as_edit(parts, channel):
    eng, ctx = parts
    assert floor(eng, ctx, aff(channel, "run", "do something"), "edit") == ["edit"]


@pytest.mark.parametrize("channel", ["window", "keys", "menu"])
def test_editing_inside_the_window_is_still_released(parts, channel):
    """The whole reason `edit` exists: asking about clearing a calculator stopped a real task dead."""
    eng, ctx = parts
    assert floor(eng, ctx, aff(channel, "press", "Clear"), "edit") == []


def test_a_coordinate_is_not_a_fact_about_what_an_action_touches(parts):
    """`channel:pointer` was on this allowlist and should not have been. The entries are meant to be facts
    — "this is the channel that moves files" — and a click is a coordinate: the text OCR read beside it may
    be wrong, and what is under it may be Send. Retracted after an audit named it."""
    eng, ctx = parts
    assert floor(eng, ctx, aff("pointer", "click", "click 「Clear」"), "edit") == ["edit"]


def test_a_verdict_that_is_not_a_release_still_asks(parts):
    eng, ctx = parts
    assert floor(eng, ctx, aff("window", "press", "Delete File"), "delete") == ["delete"]


# --------------------------------------------------------------------------- the score, not just the choice

def test_a_word_flagged_action_is_not_released_by_a_verdict_it_cannot_have(parts):
    """A flagged action goes through on the *probability* that it is a release. That sum must be counted
    over the verdicts available to this action, or `edit` releases a trash through a `delete` word hit."""
    eng, ctx = parts
    assert floor(eng, ctx, aff("file", "trash", "move 「old」 to the Trash"), "edit", 1.0) == ["delete"]


def test_the_same_label_on_two_channels_does_not_share_one_score(parts):
    """The verdict cache is keyed on app, label and where it sits — not on what runs the action."""
    eng, ctx = parts
    # the same label and the same place, so the same cache key — but not the same action. Neither trips a
    # word pattern, so what gates the second one can only be the verdict being scored for it.
    inside = aff("window", "press", "Clear")
    outside = Affordance("y", "file", "open", "Clear", {}, context="Editor")
    eng._decider = Says("edit", 1.0)
    ctx.gate = eng.gate
    eng.cache.pop("floor.verdicts", None)
    assert eng._floor("t", ctx, inside, "Window") == []          # caches the verdict under label+context
    assert eng._floor("t", ctx, outside, "Window") == ["edit"]   # same key, different action


def test_a_score_cached_by_an_older_run_is_not_thrown_away(parts):
    """Verdicts used to be cached as a number. Losing them on upgrade would re-ask for every action."""
    eng, ctx = parts
    a = aff("window", "press", "Clear")
    ctx.gate = eng.gate
    eng.cache["floor.verdicts"] = {eng._floor_key(ctx, a): ("edit", 1.0)}
    assert eng._floor("t", ctx, a, "Window") == []


# --------------------------------------------------------------------------- what the classifier is even offered

def test_a_verdict_it_may_not_have_is_not_put_on_the_menu(parts):
    """Enforcing at the decision is the guard; not offering it is what keeps the question honest."""
    eng, ctx = parts
    said = Says("other")
    eng._decider = said
    ctx.gate = eng.gate
    eng.cache.pop("floor.verdicts", None)
    eng._floor("t", ctx, aff("file", "trash", "move 「old」 to the Trash"), "Window")
    assert said.offered and "edit" not in said.offered[0] and "enter" not in said.offered[0]
