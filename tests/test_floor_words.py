"""The floor's words in the language this Mac is set to — derived once, never written for one language.

policy.yaml's word lists were two languages wide, so on a German Mac "Löschen" was never a hit and the floor
was one bar lower for every deleting action. The words are now derived from what each category means, once per
language, and kept on disk; no planner: the seed lists stand, and the log says so.
"""

import json
import logging

from macwork.engine import Engine
from macwork.model import Affordance
from macwork.words import FloorWords, language_of, languages_wanted, pattern_for
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


class GermanMac(FakeHelper):
    def call(self, method, **p):
        if method == "system.locale":
            return {"locale": "de_DE", "languages": ["de-DE", "en-US"], "ocr_languages": ["de-DE"], "region": "DE"}
        return super().call(method, **p)


GERMAN = {"words": {"delete": ["Löschen", "Entfernen", "In den Papierkorb legen", "Nicht sichern"], "send": ["Senden", "Posten"],
                    "made_up": ["x"]}}


def engine(tmp_path, helper=None, backend=None):
    eng = Engine(cfg(tmp_path), helper=helper or GermanMac(), decider=ScriptedDecider([]))
    eng._planner = backend if backend is not None else False
    return eng


def test_pattern_for_bounds_cased_scripts_and_not_others():
    assert pattern_for("Löschen") == r"(?i)\bLöschen\b" and pattern_for("削除") == "削除" and pattern_for("  ") == ""
    assert language_of("zh-Hans-CN") == "zh" and language_of("en_US") == "en"
    assert languages_wanted({"languages": ["de-DE", "en-US", "de-AT", "ja"]}, ["en", "zh"]) == ["de", "ja"]


def test_a_german_mac_gets_german_words_once_and_keeps_them(tmp_path):
    back = FakeBackend([GERMAN])
    eng = engine(tmp_path, backend=back)
    a = Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {}, context="Ablage")
    assert eng._floor_hits(a) == ["delete"]
    assert eng._floor_hits(Affordance("m2", "menu", "press", "menu Ablage ▸ Nicht sichern", {})) == ["delete"]
    assert eng._floor_hits(Affordance("m3", "menu", "press", "menu Ablage ▸ Sichern", {})) == []   # not a substring of Nicht sichern
    assert len(back.prompts) == 1 and "de" in back.prompts[0] and "deletes, erases" in back.prompts[0]
    kept = json.loads((eng.grants.path.parent / "floor-words.de.json").read_text(encoding="utf-8"))
    assert kept["words"]["delete"][0] == "Löschen" and "made_up" not in kept["patterns"]
    # a second engine on the same Mac reads the file and asks nobody
    again = engine(tmp_path, backend=FakeBackend([]))
    assert again._floor_hits(a) == ["delete"]


def test_the_languages_the_seed_lists_cover_ask_nobody(tmp_path):
    back = FakeBackend([GERMAN])
    eng = engine(tmp_path, helper=FakeHelper(), backend=back)     # an English Mac
    assert eng._floor_hits(Affordance("m1", "menu", "press", "menu File ▸ Delete", {})) == ["delete"]
    assert back.prompts == [] and eng._floor_words() == {}


def test_no_planner_means_the_seed_lists_and_a_line_in_the_log(tmp_path, caplog):
    eng = engine(tmp_path, backend=None)
    with caplog.at_level(logging.WARNING, logger="macwork.words"):
        assert eng._floor_hits(Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {})) == []
    assert any("seed lists stand" in r.message for r in caplog.records)
    assert not list(eng.grants.path.parent.glob("floor-words.*"))


def test_a_planner_that_fails_is_not_asked_again_this_run(tmp_path):
    class Broken:
        name = "broken"
        def complete(self, system, prompt, schema):
            raise RuntimeError("no network")
    eng = engine(tmp_path, backend=Broken())
    a = Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {})
    assert eng._floor_hits(a) == [] and eng._floor_hits(a) == []
    assert eng.floor_words._mem == {"de": None}


def test_the_words_raise_the_bar_on_a_german_mac_the_way_they_do_on_an_english_one(tmp_path):
    """The point of it all: an unsure 'navigate' on Löschen is now held to the flagged bar."""
    from tests.test_floor_threshold import Unsure
    eng = Engine(cfg(tmp_path), helper=GermanMac(), decider=Unsure(p=0.8))
    eng._planner = FakeBackend([GERMAN])
    ctx = eng._ctx("", {}, {"pid": 1, "name": "X", "bundle_id": "com.x"}, "t")
    ctx.gate = eng.gate
    assert eng._floor("t", ctx, Affordance("m1", "menu", "press", "menu Ablage ▸ Löschen", {}, context="Ablage"), "w") == ["delete"]
    assert eng._floor("t", ctx, Affordance("m2", "menu", "press", "menu Ablage ▸ Öffnen", {}, context="Ablage"), "w") == []
