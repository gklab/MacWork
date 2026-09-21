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
from tests.test_engine import worded, FakeHelper, ScriptedDecider, cfg

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
        destructive = "Löschen" in worded(q)
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

    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Navigates([{"pick": "Delete Document"}, {"pick": "done", "done": 0.95}]))
    engine.do("open the Delete Document view")

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


def test_editing_inside_a_document_goes_ahead_but_deleting_a_file_still_asks(tmp_path):
    """Deleting the selected text on the way to replacing it is not the delete the floor exists for: a real run
    stopped to ask about 「编辑 ▸ 删除」 while rewriting an unsaved note, and the task ended there. The line is
    drawn by the classifier — "inside the document being worked on" — not by a list of menu items."""
    class InDocument(ScriptedDecider):
        def _classify(self, q):
            pick = "edit" if "编辑" in worded(q) else "delete"
            return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}

    class Editing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "menubar":
                self.calls.append((method, p))
                return {"nodes": [
                    {"ref": "e.0", "role": "AXMenuBar", "depth": 0},
                    {"ref": "e.1", "role": "AXMenuBarItem", "title": "编辑", "parent": "e.0"},
                    {"ref": "e.2", "role": "AXMenu", "parent": "e.1"},
                    {"ref": "e.3", "role": "AXMenuItem", "title": "删除", "parent": "e.2"},
                    {"ref": "e.4", "role": "AXMenuBarItem", "title": "文件", "parent": "e.0"},
                    {"ref": "e.5", "role": "AXMenu", "parent": "e.4"},
                    {"ref": "e.6", "role": "AXMenuItem", "title": "移到废纸篓", "parent": "e.5"},
                ], "ms": 3}
            return super().call(method, timeout, **p)

    inside = Engine(cfg(tmp_path), helper=Editing(),
                    decider=InDocument([{"pick": "menu 编辑 ▸ 删除"}, {"pick": "done", "done": 0.95}]))
    assert inside.do("把这段文字改掉")["status"] == "done"

    outside = Engine(cfg(tmp_path), helper=Editing(), decider=InDocument([{"pick": "menu 文件 ▸ 移到废纸篓"}]))
    res = outside.do("把这段文字改掉")
    assert res["status"] == "need_confirm" and res["pending"]["because"] == ["delete"]


def test_putting_a_file_in_the_trash_is_offered_but_asks_first(tmp_path):
    """The must-not tasks were failing for want of the action, not for want of a guard: the engine could not
    delete a file at all, so it wandered Finder until it gave up. It can now — through the system's Trash,
    which is recoverable — and the floor stops it in front of the user, which is the whole point."""
    from macwork.observe import PROVIDERS, Ctx as _Ctx
    from macwork.model import Observation

    (tmp_path / "keep.txt").write_text("keep me", encoding="utf-8")

    class Trashing(ScriptedDecider):
        def _classify(self, q):
            pick = "delete" if "废纸篓" in worded(q) or "Trash" in worded(q) else "navigate"
            return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}

    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["files"](_Ctx(cfg(tmp_path), FakeHelper(), goal=f"删除 {tmp_path}/keep.txt", running=[]), obs)
    trashing = [a for a in obs.affordances if a.verb == "trash"]
    assert trashing and str(tmp_path / "keep.txt") == trashing[0].target["path"]

    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["files"]}}),
                 helper=FakeHelper(), decider=Trashing([{"pick": "to the Trash"}]))
    res = eng.do(f"删除 {tmp_path}/keep.txt")
    assert res["status"] == "need_confirm" and res["pending"]["because"] == ["delete"]
    assert (tmp_path / "keep.txt").exists(), "it went ahead without being confirmed"


def test_getting_out_of_a_dialog_survives_the_goal_gate(tmp_path):
    """No goal ever asks to close the panel an app put up on the way to what was asked for. A task told to
    total a column in Numbers met the Open dialog, chose its close button, had it dropped as "not asked
    for", and typed the column's name at the cursor instead. An action that only backs out stands."""
    class Gated(ScriptedDecider):
        """The goal says nothing about closing anything; the classifier says that is all this action does."""
        calls_for = 0.0
        backs_out = 1.0

        def _classify(self, q):
            pick = "back_out" if "关闭" in worded(q) else "navigate"
            return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}

    class Dialog(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") not in ("menubar",):
                return {"nodes": [{"ref": "d.0", "role": "AXWindow", "title": "打开", "depth": 0},
                                  {"ref": "d.1", "role": "AXButton", "rdesc": "按钮", "title": "关闭",
                                   "actions": ["AXPress"], "parent": "d.0"}], "ms": 1}
            return super().call(method, timeout, **p)

    d = Gated([{"pick": "关闭"}, {"pick": "done", "done": 0.95}])
    res = Engine(cfg(tmp_path), helper=Dialog(), decider=d).do("把金额那一列的合计填进去")
    assert any("关闭" in s for s in (res.get("steps") or [])), \
        f"the way out of the dialog was dropped: {res.get('status')} / {res.get('steps')}"
