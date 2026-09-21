"""The word list must not decide how strong the floor is.

Batch 2 of the v4 plan was called "take the language out of the safety floor". It removed the old defect —
a word list two languages wide, where a miss read as "nothing to classify", i.e. as safe. What replaced it
has the same shape, pointing the other way:

    if hits:                                      # the words recognised something
        if nav >= release_threshold: return []    # near-certainty required to release
        return hits
    return [] if top in self._releases(a) ...     # the words said nothing: argmax, no threshold at all

So a low-confidence "probably just navigates" releases an action on a German or Japanese Mac and stops the
same action on an English one. `nav` is computed for both paths and used by only one. policy.yaml and the
README both state the rule as "only 'it just navigates' at >= release_threshold releases".

None of the existing floor tests could see this: every fake classifier in them answers with probability
1.0, which is above any threshold.
"""

import pytest

from macwork.engine import Engine
from macwork.model import Affordance
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Unsure(ScriptedDecider):
    """A classifier that is not sure: it picks `top`, but only just."""

    def __init__(self, top="navigate", p=0.34, rest=("delete", "other")):
        super().__init__([])
        self.top, self.p, self.rest = top, p, rest

    def decide(self, state, questions):
        share = round((1.0 - self.p) / len(self.rest), 3)
        probs = {self.top: self.p, **{r: share for r in self.rest}}
        return {k: {"type": "choice", "choice": self.top, "probabilities": probs} for k in questions}


def floor(tmp_path, label, decider, **over):
    eng = Engine(cfg(tmp_path, **over), helper=FakeHelper(), decider=decider)
    ctx = eng._ctx("", {}, {"pid": 1, "name": "X", "bundle_id": "com.x"}, "t")
    ctx.gate = eng.gate
    a = Affordance("m1", "menu", "press", label, {}, context="Ablage")
    return eng._floor("t", ctx, a, "w"), eng, a


# --------------------------------------------------------------------------- the asymmetry

@pytest.mark.parametrize("label,language", [
    ("menu Ablage ▸ Löschen", "German"),
    ("menu ファイル ▸ 削除", "Japanese"),
    ("menu Файл ▸ Удалить", "Russian"),
    ("menu Fichier ▸ Supprimer", "French"),
    ("menu File ▸ Delete", "English — the words do recognise this one"),
    ("menu 文件 ▸ 删除", "Chinese — and this one"),
])
def test_an_unsure_verdict_never_releases_whatever_language_the_mac_is_in(tmp_path, label, language):
    gated, eng, a = floor(tmp_path, label, Unsure(p=0.34))
    assert gated, f"{language}: released on a 0.34 verdict"
    assert eng._needs_confirm(a, 0.0, set(), floor=gated), f"{language}: acted without asking"


def test_a_confident_verdict_still_releases(tmp_path):
    """The threshold must not turn the floor into "ask about everything" — that stopped a real task dead."""
    gated, eng, a = floor(tmp_path, "menu Ablage ▸ Öffnen", Unsure(top="navigate", p=0.97, rest=("other",)))
    assert gated == []


def test_the_bars_are_the_ones_in_policy_and_nowhere_else(tmp_path):
    lenient = {"policy": {"confirm": {"release_threshold": 0.3, "release_threshold_unflagged": 0.3}}}
    assert floor(tmp_path, "menu Ablage ▸ Löschen", Unsure(p=0.34), **lenient)[0] == []
    assert floor(tmp_path, "menu Ablage ▸ Löschen", Unsure(p=0.2), **lenient)[0]


def test_the_words_never_make_the_bar_lower(tmp_path):
    """This one replaced "the two paths agree at every confidence", which I wrote and a run disproved:
    with one bar at 0.9 a calculator's `Equals` needed confirmation. A word hit may ask for *more*
    certainty — it is evidence of risk — and must never ask for less."""
    from macwork.config import Config
    conf = Config.load().policy["confirm"]
    for p in (0.2, 0.5, 0.75, 0.89, 0.91, 0.99):
        unflagged = bool(floor(tmp_path, "menu Ablage ▸ Öffnen", Unsure(p=p))[0])
        flagged = bool(floor(tmp_path, "menu File ▸ Delete", Unsure(top="navigate", p=p, rest=("delete",)))[0])
        assert flagged or not unflagged, \
            f"at p={p} the words flagging it released something their silence would have gated"


def test_a_verdict_that_is_not_a_release_is_gated_however_sure_it_is(tmp_path):
    gated, _, _ = floor(tmp_path, "menu Ablage ▸ Löschen", Unsure(top="delete", p=0.99, rest=("navigate",)))
    assert gated == ["delete"]


def test_the_release_is_audited_on_both_paths(tmp_path):
    """policy.yaml: "Every release is written to the audit log." It was written on one path only."""
    import json
    gated, eng, _ = floor(tmp_path, "menu Ablage ▸ Öffnen", Unsure(top="navigate", p=0.97, rest=("other",)))
    assert gated == []
    lines = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e.get("kind") == "floor" and e.get("released") for e in lines), \
        "an action was released and the audit log does not say so"


# --------------------------------------------------------------------------- an uncalibrated decider

class Uncalibrated(ScriptedDecider):
    """What `localdecider` returns: all the mass on one option, capped at `confidence_ceiling` (0.8),
    because a text model's "0.99" is a word it wrote rather than a measured frequency."""

    calibrated = False

    def __init__(self, verdict="navigate", says_safe=True, p=0.8):
        super().__init__([])
        self.verdict, self.says_safe, self.p = verdict, says_safe, p

    def decide(self, state, questions):
        out = {}
        for k, q in questions.items():
            if q.get("type") == "noul":
                out[k] = {"type": "noul", "noul": 0.8 if not self.says_safe else 0.2, "confidence": 0.8}
            else:
                out[k] = {"type": "choice", "choice": self.verdict, "probabilities": {self.verdict: self.p}}
        return out


def test_an_uncalibrated_decider_does_not_gate_every_last_action(tmp_path):
    """`decider.kind: auto` falls back to a local model whenever there is no key, and its ceiling (0.8) is
    below the release threshold (0.9) by design — so a threshold alone would make it ask about scrolling."""
    for label in ("menu 文件 ▸ 新建", "click the OK button", "scroll down in this window"):
        gated, eng, a = floor(tmp_path, label, Uncalibrated())
        assert gated == [], f"{label}: an uncalibrated decider gated a harmless action"


def test_an_uncalibrated_decider_is_still_asked_and_still_believed_when_it_says_no(tmp_path):
    gated, eng, a = floor(tmp_path, "menu Ablage ▸ Löschen", Uncalibrated(says_safe=False))
    assert gated, "it said the action is not harmless and was released anyway"


def test_an_uncalibrated_decider_cannot_release_a_word_flagged_action_by_argmax_alone(tmp_path):
    """The ceiling existed for exactly this; one threshold must not take it away."""
    gated, _, _ = floor(tmp_path, "menu File ▸ Delete", Uncalibrated(says_safe=False))
    assert gated == ["delete"]


def test_a_gate_reason_is_never_the_name_of_a_release(tmp_path):
    """`because: ['navigate']` is not a reason to show anyone."""
    gated, _, _ = floor(tmp_path, "menu Ablage ▸ Öffnen", Unsure(top="navigate", p=0.5))
    assert "navigate" not in gated and "enter" not in gated and "edit" not in gated


# --------------------------------------------------------------------------- two thresholds, both real

def test_an_unsure_verdict_is_gated_whether_or_not_the_words_saw_it(tmp_path):
    """The original defect and the reason for all of this: a 0.34 verdict must not release, in any
    language. That stays true — what changes below is only how sure is sure enough."""
    for label in ("menu Ablage ▸ Löschen", "menu File ▸ Delete"):
        assert floor(tmp_path, label, Unsure(p=0.34))[0], label


def test_an_ordinary_action_is_not_gated_for_being_less_than_nine_tenths_certain(tmp_path):
    """Measured, and the reason this is here: with one 0.9 bar on both paths a calculator's `Equals`
    came back needing confirmation — judged `edit`, which is a release, at less than 0.9. `File ▸ New`
    and `File ▸ Open…` went the same way. That is the failure `edit` was added to prevent, recreated."""
    gated, eng, a = floor(tmp_path, "button 「Equals」", Unsure(top="edit", p=0.8, rest=("other",)))
    assert gated == [], "an everyday action was gated for ordinary uncertainty"


def test_the_words_flagging_it_asks_for_more_certainty_not_less(tmp_path):
    """A word hit is evidence of risk, and evidence is allowed to change what is required. What is not
    allowed is for its *absence* to be evidence of safety — that was argmax with no floor at all."""
    assert floor(tmp_path, "menu File ▸ Delete", Unsure(top="navigate", p=0.8, rest=("delete",)))[0]
    assert floor(tmp_path, "menu File ▸ Delete", Unsure(top="navigate", p=0.95, rest=("delete",)))[0] == []


def test_both_bars_are_named_in_policy(tmp_path):
    from macwork.config import Config
    conf = Config.load().policy["confirm"]
    assert conf["release_threshold"] > conf["release_threshold_unflagged"] > 0.5, \
        "the bar for an action the words flagged must be the higher of the two, and neither may be a formality"


def test_the_lower_bar_is_still_a_bar(tmp_path):
    """Not argmax by another name: below it, an action is gated however it was labelled."""
    gated, _, _ = floor(tmp_path, "menu Ablage ▸ Öffnen", Unsure(top="navigate", p=0.5, rest=("other",)))
    assert gated
