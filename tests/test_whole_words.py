"""A pseudonym stands for a value where that value is a word — not wherever its letters occur.

The redactor used to replace every value it had learned as a raw substring of every later string. On a real
Mac that put pseudonyms inside words: 「Saf」, learned from a label the engine itself had cut at 40 characters,
went out as 'open app ⟦PERSON_5⟧ari浏览器'; 「单元格」 ("cell"), learned from a tab, went out inside the goal as
'在第一个⟦PERSON_2⟧里输入 42'. Replayed over 3,479 recorded step requests with the on-device tagger, a pseudonym
sat inside a word in 75.3% of looks before and 15.4% after (a value found glued that way, or in a script without
capitals a name even on its own), and the goal changed in 407 looks before and none after; no value was hidden
that was not hidden before.

Where a word ends is one rule for the whole engine (model.joined_at): at a space, punctuation or an underscore,
where letters with capitals meet letters without, and where letters meet digits. A name still has to be hidden
where it is glued into a longer word the way names are — the privacy guards below.
"""

import json

from macwork.config import Config
from macwork.model import Change
from macwork.privacy import Audit, Gate, Redactor


def tagger(found: dict[str, list[tuple[str, str]]]):
    """Stands in for the on-device tagger: `found` maps a text it is shown to what it calls names in it."""
    def entities(texts):
        return [[{"type": kind, "text": value} for value, kind in found.get(t, [])] for t in texts]
    return entities


def redactor(ents, protect=None):
    return Redactor(Config.load(), entities=ents, protect=(lambda: protect) if protect is not None else None)


# --------------------------------------------------------------------------- scripts with spaces

def test_a_value_is_not_replaced_inside_a_longer_word():
    r = redactor(tagger({"a Service of Saf": [("Saf", "PERSON")]}))
    r.value(["「Add to Reading List」 — a Service of Saf"])          # what a cut summary taught it
    assert r.text("open app Safari") == "open app Safari"
    assert r.text("「Look Up」 — a Service of Dictionary") == "「Look Up」 — a Service of Dictionary"


def test_where_the_value_is_a_word_it_is_still_replaced():
    """A privacy guard: an apostrophe, an underscore, a digit and a change of script each end a word."""
    r = redactor(tagger({"call Emily Johnson now": [("Emily Johnson", "PERSON")]}))
    r.text("call Emily Johnson now")
    for text in ("Re: Emily Johnson's notes", "Emily Johnson_CV.pdf", "Emily Johnson2024", "发给Emily Johnson"):
        assert "Emily Johnson" not in r.text(text), text


def test_a_name_with_a_one_letter_ending_is_still_hidden():
    """A privacy guard. Swedish, Norwegian, Danish and German write a genitive without an apostrophe, and a
    plural adds a letter too: a name with capitals followed by exactly one lowercase letter is still the name."""
    r = redactor(tagger({"I met Lars Andersen yesterday.": [("Lars Andersen", "PERSON")]}))
    r.text("Lars Andersen")                                           # a bare label: learned through a carrier
    assert "Lars Andersen" not in r.text("AirDrop: Lars Andersens iPhone")


def test_a_name_the_user_listed_is_hidden_inside_a_longer_word_too():
    """A privacy guard. `privacy.redact.names` is the person's own list: hidden wherever it occurs, glued into
    a longer word too, whatever the tagger finds."""
    r = Redactor(Config.load(overrides={"privacy": {"redact": {"names": ["王芳", "Emily"]}}}), entities=tagger({}))
    for text in ("来自王芳的消息", "EmilyNotes.txt"):
        sent = r.text(text)
        assert "王芳" not in sent and "Emily" not in sent, sent


# --------------------------------------------------------------------------- scripts without spaces

def test_a_word_that_holds_the_same_letters_is_left_whole():
    """「单元格」 ("cell") was tagged a person as a bare tab label. The tagger calls it one in the second neutral
    sentence and not in the carrier, so it is not a name on its own, and the goal that holds it goes out whole."""
    r = redactor(tagger({"单元格": [("单元格", "PERSON")], "单元格的电话是多少？": [("单元格", "PERSON")]}))
    r.text("单元格")
    assert r.text("在第一个单元格里输入 42") == "在第一个单元格里输入 42"


def test_a_name_found_glued_into_text_is_replaced_there():
    """A privacy guard: where a name was found glued, it is replaced where it sits that way, in any string —
    also where it was found in a run of one script of a mixed clause, read in a sentence of its own."""
    r = redactor(tagger({"来自王芳的消息": [("王芳", "PERSON")], "我和李娜加入群聊开会。": [("李娜", "PERSON")]}))
    assert "王芳" not in r.text("来自王芳的消息")
    assert "王芳" not in r.text("回复：来自王芳的消息")                  # the same place, in another string
    assert "李娜" not in r.text("Invite 李娜加入群聊")
    assert "李娜" not in r.text("Re: Invite 李娜加入群聊")


def test_a_name_is_still_hidden_where_the_tagger_misses_it_in_running_text():
    """A privacy guard. Learned from a bare label (a contact list row), a value the tagger calls a name in two
    neutral sentences of its own is a name, and it is hidden glued into running text the tagger finds nothing
    in."""
    r = redactor(tagger({"吴军": [("吴军", "PERSON")], "我和吴军开会。": [("吴军", "PERSON")],
                         "吴军的电话是多少？": [("吴军", "PERSON")]}))
    r.text("吴军")
    assert "吴军" not in r.text("来自吴军的消息")


def test_a_name_found_in_running_text_is_hidden_wherever_it_is_glued():
    """A privacy guard. The tagger does not call every given name a name in both neutral sentences (「一诺」 is
    one it does not), but one it found inside running text is a name there, and is hidden glued."""
    r = redactor(tagger({"发送给 一诺": [("一诺", "PERSON")], "一诺的电话是多少？": [("一诺", "PERSON")]}))
    r.text("发送给 一诺")
    assert "一诺" not in r.text("给一诺发消息")


# --------------------------------------------------------------------------- this Mac's own names

def test_a_replacement_never_cuts_a_name_of_this_mac_in_two():
    """The real tagger calls 「小红」 of the app 「小红鼠VPN」 a person, and the reading of a menu run 「menu Keynote」
    a person; neither may take a piece out of the app's name."""
    r = redactor(tagger({"我和小红鼠开会。": [("小红", "PERSON")], "I met menu Keynote yesterday.": [("menu Keynote", "PERSON")]}),
                 protect=["小红鼠VPN", "Keynote讲演"])
    assert r.value(["open app 小红鼠VPN", "menu Keynote讲演 ▸ 设置…"]) == ["open app 小红鼠VPN", "menu Keynote讲演 ▸ 设置…"]


def test_a_shorter_name_of_this_mac_is_not_cut_where_a_longer_one_is_glued_on():
    """「Safari浏览器」 and 「Safari」 are both this Mac's names. In 「Safari浏览器扩展…」 the longer one is glued into a
    longer word and the shorter one stands as a word; 「Safar」, cut out of a label, may not take a piece of it."""
    r = redactor(tagger({"a Service of Safar": [("Safar", "PERSON")]}), protect=["Safari浏览器", "Safari"])
    r.text("a Service of Safar")
    for text in ("menu Safari浏览器 ▸ Safari浏览器扩展…", "menu 帮助 ▸ Safari浏览器帮助 (⌘?)"):
        assert r.text(text) == text


# --------------------------------------------------------------------------- mixed scripts

def test_a_clause_in_two_scripts_is_read_one_script_at_a_time():
    """Read whole, 「用 Numbers 打开」 was taken for English and its 「打开」 ("open") for a person, so all 14 tasks of
    09-20..23 whose goal began so sent it as '用 Numbers ⟦PERSON_2⟧ ~/…' (the number varying). Each run is read in
    a sentence of its own script."""
    r = redactor(tagger({"用 Numbers 打开": [("打开", "PERSON")]}))
    goal = "用 Numbers 打开 ~/Library/Caches/sales.csv"
    assert r.text(goal) == goal


# --------------------------------------------------------------------------- what was left in place

def test_what_was_left_in_place_is_on_the_audit_log(tmp_path):
    cfg = Config.load(overrides={"config": {"audit": {"path": str(tmp_path / "a.jsonl")}}})

    class Decider:
        def decide(self, state, questions):
            return {}
    r = Redactor(cfg, entities=tagger({"a Service of Saf": [("Saf", "PERSON")]}))
    r.text("a Service of Saf")
    Gate(Decider(), Audit(cfg)).decide(r, {"app": "Safari"}, {"q": {"type": "noul", "instructions": "ok?"}})
    sent = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()]
    decide = next(x for x in sent if x["kind"] == "decide")
    assert decide["state"]["app"] == "Safari"
    assert decide["kept"] == {"glued": {"PERSON": 1}, "in_a_name": {}, "replaced_glued": {}}


# --------------------------------------------------------------------------- the engine's own cuts

def test_what_a_step_changed_is_never_told_with_a_word_cut_in_two():
    """The history tells the decider what each step changed, each piece cut to fit, and 13 of the 24 cuts found
    whole again in the step requests of 09-20..23 fell inside a word (「SERENDIPITY Definition & Mean…」). A piece
    cut inside a word reads like a word of its own: from the 'look into' summaries, also cut by length, the
    redactor learned 「Saf」 out of 「Safari」."""
    change = Change(window_before="Downloads", window_after="The Unarchiver finished extracting",
                    appeared=["The Unarchiver finished extracting the Selection of files"])
    assert change.describe() == ("window 「Downloads」 → 「The Unarchiver finished…」, "
                                 "appeared: The Unarchiver finished extracting the…")
    assert change.describe(60) == "window 「Downloads」 → 「The Unarchiver finished…」, appeared:…"
