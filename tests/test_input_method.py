"""An input method in the way of typing.

The helper types through an ASCII keyboard layout, so that an input method cannot compose the keys, and it gave
the person's own input source back as soon as the last key was posted. An app still a few keys behind then read
the last of them through the input method: 21 of 265 typing steps on 09-20..23 left the end of their text
composing. Reading the field back passed them, because the value holds the composition too, and the next look
took the input method's candidate panel for a prompt in front of the app: 48 looks in 27 tasks, 168 candidates
offered as options, 4 reports that it was left for the person to answer.

Helper protocol 0.2.0 keeps the layout until the keys have landed and says how (`landed`, `handback_ms`,
`switched`), says what an element is still composing (`marked`), and marks an input method's windows
(`input_method`). These are the engine's side of each. Nothing here needs a Mac, and no input method is named:
the one below is 'Example Input', composing the 'lo' of 'hello'.
"""

import json

from macwork.act import get_channel
from macwork.engine import Engine
from macwork.helper import HelperError
from macwork.model import Affordance
from macwork.observe import Ctx
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

INTO_FIELD = Affordance("w3", "window", "type", "type into text field 「Recipient」", {"ref": "g2.3", "pid": 42})
AT_CURSOR = Affordance("y0", "keys", "type", "type the given text at the cursor", {})


# ----------------------------------------------------------------- the keyboard handed back once the keys are read

class HandsBack(FakeHelper):
    """Helper 0.2.0's reply to input.type: it had to select the ASCII layout, and the keys were read 14 ms after
    the last one. The menus' search field here takes no value, so a menu search types its words."""

    def call(self, method, timeout=30.0, **p):
        if method == "input.type":
            super().call(method, timeout, **p)
            return {"ok": True, "chars": len(p.get("text", "")), "switched": True, "landed": "read", "handback_ms": 14}
        if method == "ax.set" and p.get("ref") == "search":
            self.calls.append((method, p))
            raise HelperError("ax_error", "the field does not take a value")
        return super().call(method, timeout, **p)


class Ignores(HandsBack):
    """The keys were read, and the field still shows its old text, however it is written to."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.get":
            self.calls.append((method, p))
            return {"value": "/old/path"}
        return super().call(method, timeout, **p)


def test_typing_asks_the_helper_to_hold_the_layout_until_the_keys_are_read(tmp_path):
    ctx = Ctx(cfg(tmp_path, config={"input": {"handback_ms": 1234, "handback_quiet_ms": 321}}), HandsBack(),
              app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    ways = [INTO_FIELD,
            Affordance("w4", "window", "type_submit", "type into text field 「Search」 and press Return", {"ref": "g2.3", "pid": 42}),
            AT_CURSOR,
            Affordance("m0", "menusearch", "search", "search the menus", {"pid": 42, "menu_ref": "help", "field_ref": "search"})]
    outs = [get_channel(a.channel)(ctx, a, {"text": "hello"}) for a in ways]
    sent = ctx.helper.did("input.type")
    assert len(sent) == len(ways)
    assert all(p.get("handback_ms") == 1234 and p.get("quiet_ms") == 321 for p in sent), sent
    assert [o.typed for o in outs[:3]] == [{"ok": True, "chars": 5, "switched": True, "landed": "read", "handback_ms": 14}] * 3
    # a value set behind the app's back that did not take is typed after all, and the same way
    ctx = Ctx(cfg(tmp_path, config={"input": {"handback_ms": 1234, "handback_quiet_ms": 321, "set_value_first": True}}),
              Ignores(), app={"pid": 42, "name": "TextEdit"}, goal="", inputs={}, task="t", gate=None, cache={})
    out = get_channel("window")(ctx, INTO_FIELD, {"text": "hello"})
    assert [(p.get("handback_ms"), p.get("quiet_ms")) for p in ctx.helper.did("input.type")] == [(1234, 321)]
    assert out.typed == outs[0].typed


def test_each_typing_step_is_recorded_by_how_it_was_handed_back_never_by_its_text(tmp_path):
    text = "someone.unusual@example.com"
    eng = Engine(cfg(tmp_path), helper=HandsBack(), decider=ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}]))
    res = eng.do("fill in the recipient", {"text": text}, app="TextEdit")
    assert res["status"] == "done"
    recs = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [r for r in recs if r["kind"] == "step"]
    typed = steps[0]
    assert [typed.get(k) for k in ("typed_landed", "typed_handback_ms", "typed_switched", "typed_kept")] == ["read", 14, True, True]
    assert "typed_landed" not in steps[-1], "the looks after the last step typed nothing"
    assert "typed" not in {r["kind"] for r in recs}, "no record of its own: the step's record says it"
    # the floor's record of what the action was carries its text, as it did before; the record of how the keys
    # were handed back never does
    assert not any(text in json.dumps(r, ensure_ascii=False) for r in steps)
    assert not any(isinstance(v, str) and len(v) > 12 for v in typed.values() if v != typed["task"])

    eng = Engine(cfg(tmp_path / "ignored"), helper=Ignores(), decider=ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}]))
    eng.do("fill in the recipient", {"text": text}, app="TextEdit")
    failed = [json.loads(line) for line in (tmp_path / "ignored" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
              if json.loads(line)["kind"] == "step"][0]
    assert failed["ok"] is False and (failed.get("typed_landed"), failed.get("typed_kept")) == ("read", False), \
        "a step that broke its promise still says how its keys were handed back"
