"""Where the keyboard goes, and whether what was typed landed there.

"Type at the cursor" is the only action whose target is in no app's Accessibility tree: it goes wherever the
*system* focus is. The engine typed into it blind — the decider was not told where the cursor was, and
afterwards `contract.kept` returned "nothing to read back", so a step that typed into the wrong window was
recorded as unverified rather than wrong. `ax.focused` had been registered in the helper for exactly this and
had no caller at all.
"""

from typing import Any

import pytest

from macwork.contract import kept
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, get_provider


class Helper:
    mode = "fake"

    def __init__(self, **focus: Any) -> None:
        self.focus = {"focused": {"role": "AXTextArea", "rdesc": "文本", "ref": "f1", "value": ""},
                      "pid": 42, "app": "文本编辑", "bundle_id": "com.apple.TextEdit", "secure_input": False} | focus
        self.calls: list[str] = []

    def call(self, method: str, timeout: float = 30.0, **p: Any) -> Any:
        self.calls.append(method)
        if method == "ax.focused":
            return self.focus
        if method == "ax.get":
            return {"value": "whatever the field holds"}
        raise AssertionError(method)


class Out:
    ok, error, target = True, None, {}


def ctx(helper: Helper, cfg: Any = None) -> Ctx:
    from macwork.config import Config
    return Ctx(cfg=cfg or Config.load(), helper=helper, app={"pid": 42, "name": "文本编辑"},
               goal="", inputs={"text": "会议改到周四"}, task="t", gate=None, cache={})


def look(helper: Helper) -> Observation:
    c = ctx(helper)
    obs = Observation(app=c.app, window=None, affordances=[])
    get_provider("focus")(c, obs)
    get_provider("typing")(c, obs)
    return obs


# --------------------------------------------------------------------------- what the decider is told

def test_the_cursor_s_whereabouts_reach_the_label():
    obs = look(Helper())
    assert obs.focused and obs.focused["role"] == "AXTextArea"
    assert any("光标" in a.label or "cursor is in" in a.label for a in obs.affordances), \
        [a.label for a in obs.affordances]


def test_a_cursor_in_another_app_is_said_so_because_the_text_would_land_there():
    obs = look(Helper(pid=999, app="访达"))
    typing = [a for a in obs.affordances if a.verb == "type"]
    assert typing and "访达" in typing[0].label, typing[0].label if typing else "no typing affordance"


def test_secure_input_is_reported_rather_than_typed_into():
    obs = look(Helper(secure_input=True))
    typing = [a for a in obs.affordances if a.verb == "type"]
    assert typing and ("password" in typing[0].label or "密码" in typing[0].label)


def test_what_the_focused_element_holds_is_never_put_in_an_affordance():
    """Focus sits in password fields, and this provider runs on every look."""
    helper = Helper()
    helper.focus["focused"]["value"] = "hunter2-the-actual-secret"
    obs = look(helper)
    assert "hunter2" not in repr(obs.focused)
    assert not any("hunter2" in a.label for a in obs.affordances)


# --------------------------------------------------------------------------- reading it back afterwards

def typed(text: str, helper: Helper, ref: str | None = None) -> tuple[bool | None, str]:
    a = Affordance("a", "keys", "type", "type at the cursor", {"ref": ref} if ref else {})
    return kept(ctx(helper), a, {"text": text}, Out(), [])


def test_text_typed_at_the_cursor_is_now_checked():
    helper = Helper()
    helper.focus["focused"]["value"] = "会议改到周四"
    ok, why = typed("会议改到周四", helper)
    assert ok is True and "ax.focused" in helper.calls, why


def test_typing_into_the_wrong_place_is_caught():
    helper = Helper()
    helper.focus["focused"]["value"] = "something else entirely"
    ok, why = typed("会议改到周四", helper)
    assert ok is False, why


def test_a_document_longer_than_the_readback_is_unverified_not_failed():
    """The worse mistake is calling a long file wrong because the typed line sits past the cut."""
    helper = Helper()
    helper.focus["focused"]["value"] = "x" * 5000
    ok, why = typed("会议改到周四", helper)
    assert ok is None, why


def test_nothing_is_read_back_while_secure_input_is_on():
    helper = Helper(secure_input=True)
    helper.focus["focused"]["value"] = "hunter2"
    ok, why = typed("hunter2", helper)
    assert ok is None and "could not be read" in why


def test_a_field_with_a_ref_is_still_read_through_that_ref():
    helper = Helper()
    ok, why = typed("whatever", helper, ref="r1")
    assert "ax.focused" not in helper.calls and ok is True, why


@pytest.mark.parametrize("focused", [None, {}])
def test_nothing_focused_is_not_a_failure(focused):
    helper = Helper(focused=focused)
    ok, _ = typed("会议改到周四", helper)
    assert ok is None
