"""The safety floor, in a language its word list never saw.

`policy.yaml` spells its categories in English and Chinese. A German "Löschen", a Japanese "削除", a French
"Supprimer" or a Russian "Удалить" matches none of them — and the floor used to read a miss as "nothing to
classify", i.e. as safe, so the engine would delete without asking on any Mac not set to one of those two
languages. The words are now a hint about what to classify first; the verdict is the decider's, on every
action that is about to run.
"""

import json

from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import Ctx
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

GERMAN = {"nodes": [
    {"ref": "g1.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "g1.1", "role": "AXMenuBarItem", "title": "Apple", "parent": "g1.0"},   # skipped, as the real one is
    {"ref": "g1.2", "role": "AXMenuBarItem", "title": "Ablage", "parent": "g1.0"},
    {"ref": "g1.3", "role": "AXMenu", "parent": "g1.2"},
    {"ref": "g1.4", "role": "AXMenuItem", "title": "Neues Dokument", "parent": "g1.3"},
    {"ref": "g1.5", "role": "AXMenuItem", "title": "Löschen", "parent": "g1.3"},
], "ms": 3}


class GermanMac(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "menubar":
            self.calls.append((method, p))
            return GERMAN
        return super().call(method, timeout, **p)


class Classifier(ScriptedDecider):
    """Answers a floor question the way a real classifier would: it reads the action, not a word list."""

    def _classify(self, q):
        destructive = "Löschen" in q.get("instructions", "")
        pick = "delete" if destructive else "navigate"
        return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}


def test_a_destructive_button_in_another_language_still_asks(tmp_path):
    decider = Classifier([{"pick": "Löschen"}])
    result = Engine(cfg(tmp_path), helper=GermanMac(), decider=decider).do("räum das auf")

    assert result["status"] == "need_confirm"
    assert result["pending"]["because"] == ["delete"]
    assert "Löschen" in result["pending"]["confirm"]["label"]


def test_an_ordinary_action_in_another_language_is_not_gated(tmp_path):
    decider = Classifier([{"pick": "Neues Dokument"}, {"pick": "done", "done": 0.95}])
    result = Engine(cfg(tmp_path), helper=GermanMac(), decider=decider).do("neues Dokument anlegen")

    assert result["status"] == "done"
    assert result["steps"] == ["menu Ablage ▸ Neues Dokument"]


def test_the_words_alone_would_have_missed_it(tmp_path):
    """The word list is what the fix stops relying on — show that it really does miss."""
    engine = Engine(cfg(tmp_path), helper=GermanMac(), decider=ScriptedDecider([]))
    loeschen = Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {})
    assert engine._floor_hits(loeschen) == []
    assert engine._floor_hits(Affordance("m2", "menu", "press", "menu File ▸ Delete", {})) == ["delete"]


def test_a_classification_that_could_not_be_made_is_not_remembered(tmp_path):
    """Caching a failure would let one hiccup put the words back in charge for the rest of the run."""
    from macwork.decider import DeciderError

    class Broken(ScriptedDecider):
        def decide(self, state, questions):
            raise DeciderError("no route to the service")

    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Broken([]))
    ctx = Ctx(engine.cfg, engine.helper, app={"pid": 42, "bundle_id": "com.apple.TextEdit"})
    ctx.gate = engine.gate
    action = Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {})

    assert engine._floor("t", ctx, action) == []          # nothing known, and the words are silent
    assert engine.cache.get("floor.verdicts") == {}, "a failure must leave no verdict behind"


def test_a_release_is_written_to_the_audit_log(tmp_path):
    class Navigates(ScriptedDecider):
        def _classify(self, q):
            return {"type": "choice", "choice": "navigate", "probabilities": {"navigate": 1.0}}

    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Navigates([{"pick": "删除文稿"}, {"pick": "done", "done": 0.95}]))
    engine.do("打开删除文稿这个视图")

    log = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    released = [r for r in log if r["kind"] == "floor" and r["released"]]
    assert released and released[0]["words"] == ["delete"]


def test_step_level_acts_are_classified_too(tmp_path):
    """`mac_act` trusted the word list alone, so the same German button went through without being offered."""
    engine = Engine(cfg(tmp_path), helper=GermanMac(), decider=Classifier([]))
    seen = engine.observe()
    loeschen = next(a for a in seen["affordances"] if "Löschen" in a["label"])

    refused = engine.act(loeschen["id"])
    assert refused["needs_confirm"] and refused["because"] == ["delete"]
    assert engine.act(loeschen["id"], confirm=True)["ok"] is True


def test_the_step_level_classification_can_be_turned_off(tmp_path):
    """A caller that does its own judging can have the old, purely mechanical behaviour back."""
    engine = Engine(cfg(tmp_path, policy={"confirm": {"classify_step_level": False}}),
                    helper=GermanMac(), decider=Classifier([]))
    seen = engine.observe()
    loeschen = next(a for a in seen["affordances"] if "Löschen" in a["label"])

    assert engine.act(loeschen["id"])["ok"] is True      # the words never saw it, and nobody else was asked
