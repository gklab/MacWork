"""What can be done with an element, asked of the element.

The role lists in `observe.window` used to *be* the definition: a control whose role was not listed could not
be typed into, selected or read, however plainly it offered to be. A web input, a `contenteditable` group, a
custom control — invisible. Likewise `action_labels`: an AX action outside that table was discarded, which
hid `AXRaise`, `AXCancel`, `AXDelete` and every action an app defines for itself.

Measured on a real Mac, capability probing finds what the lists missed — typing targets 3 → 18 in an editor,
2 → 9 in a chat app. But it is not a replacement, it is a second source: the Mac's "this attribute is not
settable" is not the last word, because the engine can click a row or focus a field and type. So the two are
taken together, and these tests pin both directions.
"""

from macwork.model import Observation
from macwork.observe import Ctx, element_affordances
from tests.test_engine import FakeHelper, cfg


def _offer(tmp_path, nodes, **window):
    obs = Observation(app={"pid": 42, "name": "app"}, window=None, affordances=[])
    conf = {"observe": {"window": window}} if window else {}
    ctx = Ctx(cfg(tmp_path, config=conf), FakeHelper(), app={"pid": 42, "name": "app"})
    element_affordances(ctx, obs, [{"ref": "w.0", "role": "AXWindow", "depth": 0}] + nodes, "w")
    return {(a.verb, a.label) for a in obs.affordances}


def test_a_control_with_an_unlisted_role_can_still_be_typed_into(tmp_path):
    """A web input reports AXGroup and would never have been offered."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXGroup", "rdesc": "group", "title": "消息",
                                 "editable": True, "parent": "w.0"}])
    assert any(verb == "type" and "消息" in label for verb, label in offered)


def test_a_listed_role_is_still_offered_when_the_app_says_it_is_not_settable(tmp_path):
    """The engine focuses the field and types; a refused attribute set is not the last word."""
    node = {"ref": "w.1", "role": "AXTextField", "rdesc": "文本栏", "title": "收件人", "parent": "w.0"}
    assert any(verb == "type" for verb, _ in _offer(tmp_path, [node]))


def test_a_settable_number_is_not_a_typing_target(tmp_path):
    """A scroll bar's position is settable too — it offered "type into the scroll bar (now: 0)"."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXScrollBar", "rdesc": "滚动条", "value": "0",
                                 "parent": "w.0"}])
    assert not any(verb.startswith("type") for verb, _ in offered)


def test_selecting_works_from_either_source(tmp_path):
    capability = _offer(tmp_path, [{"ref": "w.1", "role": "AXCell", "rdesc": "单元格", "title": "第一项",
                                    "selectable": True, "parent": "w.0"}])
    assert any(verb == "select" and "第一项" in label for verb, label in capability)

    by_role = _offer(tmp_path, [{"ref": "w.1", "role": "AXRow", "rdesc": "表格行", "title": "第二项", "parent": "w.0"}])
    assert any(verb == "select" and "第二项" in label for verb, label in by_role)


def test_an_action_outside_the_table_is_offered_under_the_apps_own_name(tmp_path):
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXWindow", "rdesc": "窗口", "title": "文稿",
                                 "actions": ["AXRaise"], "action_desc": {"AXRaise": "移到最前"}, "parent": "w.0"}])
    assert any("移到最前" in label for _, label in offered), offered


def test_an_extra_action_is_not_offered_where_a_named_one_already_reaches_the_element(tmp_path):
    """AXScrollToVisible sat on 428 of 441 elements that all had AXPress: 428 more options, no more ability."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXButton", "rdesc": "按钮", "title": "发送",
                                 "actions": ["AXPress", "AXScrollToVisible"],
                                 "action_desc": {"AXScrollToVisible": "滚动到可见"}, "parent": "w.0"}])
    assert any(verb == "press" and "发送" in label for verb, label in offered)
    assert not any("滚动到可见" in label for _, label in offered)


def test_an_element_that_both_takes_text_and_has_actions_offers_both(tmp_path):
    """A table cell takes text and has a context menu; finding more typing targets must not remove actions."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXCell", "rdesc": "单元格", "title": "备注",
                                 "editable": True, "actions": ["AXShowMenu"], "parent": "w.0"}])
    assert any(verb == "type" for verb, _ in offered)
    assert any("context menu" in label for _, label in offered)


def test_the_old_behaviour_is_one_setting_away(tmp_path):
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXGroup", "rdesc": "group", "title": "消息",
                                 "editable": True, "parent": "w.0"}], by_capability=False)
    assert not any(verb == "type" for verb, _ in offered)
