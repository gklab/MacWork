"""What the engine keys its memory on.

Memory was keyed on the full label: a field that read 「type into Search (now: hello)」 was a new action
after every keystroke, so nothing remembered about it ever matched; a control relabelled by a language
switch or a state change was a stranger. A handle is the identity where the Mac gives one — an
Accessibility identifier, or, for a control the app gave none, where it sits — and the steady name only
where there is nothing else.
"""

from macwork.config import Config
from macwork.engine import Engine
from macwork.model import Affordance, Observation, Step
from macwork.observe import Ctx, _structural_identity, _tree, element_affordances
from tests.english_mac import EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

CONTENT = {"AXRow", "AXCell", "AXImage", "AXStaticText", "AXLink", "AXHeading", "AXList", "AXOutline", "AXTable", "AXWebArea", "AXGroup", "AXMenuItem"}


def test_a_control_without_an_identifier_is_known_by_where_it_sits():
    nodes = [{"ref": "w", "role": "AXWindow"},
             {"ref": "t", "role": "AXToolbar", "parent": "w"},
             {"ref": "b1", "role": "AXButton", "title": "Back", "parent": "t"},
             {"ref": "b2", "role": "AXButton", "title": "Forward", "parent": "t"},
             {"ref": "s", "role": "AXTextField", "parent": "t"}]
    by_ref, kids = _tree(nodes)
    assert _structural_identity(by_ref["b1"], by_ref, kids, CONTENT) == "axpath|AXWindow[0]/AXToolbar[0]/AXButton[0]"
    assert _structural_identity(by_ref["b2"], by_ref, kids, CONTENT) == "axpath|AXWindow[0]/AXToolbar[0]/AXButton[1]"
    assert _structural_identity(by_ref["s"], by_ref, kids, CONTENT) == "axpath|AXWindow[0]/AXToolbar[0]/AXTextField[0]"
    relabelled = {**by_ref["b1"], "title": "Zurück"}
    assert _structural_identity(relabelled, by_ref, kids, CONTENT) == _structural_identity(by_ref["b1"], by_ref, kids, CONTENT)


def test_content_keeps_its_name_as_its_identity():
    """A row's place changes with every sort; a button inside a row has no stable place either."""
    nodes = [{"ref": "w", "role": "AXWindow"}, {"ref": "o", "role": "AXOutline", "parent": "w"},
             {"ref": "r", "role": "AXRow", "parent": "o"}, {"ref": "b", "role": "AXButton", "title": "Open", "parent": "r"}]
    by_ref, kids = _tree(nodes)
    assert _structural_identity(by_ref["r"], by_ref, kids, CONTENT) == ""
    assert _structural_identity(by_ref["b"], by_ref, kids, CONTENT) == ""


def test_window_controls_come_out_with_handles_and_rows_with_names(tmp_path):
    ctx = Ctx(Config.load(), FakeHelper(), app={"pid": 1, "name": "App"}, goal="", inputs={}, task="t", gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    nodes = [{"ref": "w", "role": "AXWindow", "title": "w"},
             {"ref": "b", "role": "AXButton", "title": "Refresh", "actions": ["AXPress"], "parent": "w"},
             {"ref": "f", "role": "AXTextField", "title": "Search", "value": "hello", "editable": True, "parent": "w"},
             {"ref": "o", "role": "AXOutline", "parent": "w"},
             {"ref": "r", "role": "AXRow", "selectable": True, "parent": "o"},
             {"ref": "rt", "role": "AXStaticText", "value": "note.txt", "parent": "r"}]
    element_affordances(ctx, obs, nodes, "w")
    by_label = {a.label: a for a in obs.affordances}
    button = next(a for a in obs.affordances if "Refresh" in a.label)
    field = next(a for a in obs.affordances if a.verb == "type")
    row = next(a for a in obs.affordances if a.verb == "select")
    assert button.handle().startswith("axpath|") and field.handle().startswith("axpath|")
    assert row.key == "" and row.handle() == "select row 「note.txt」", "content: its name"
    assert "(now: hello)" in field.label and "now:" not in field.handle(), "what it shows right now is not who it is"


def test_what_did_nothing_stays_known_when_the_field_shows_something_else(tmp_path):
    """The same field, another keystroke later, was a new action to the memory."""
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    task = eng._new_task("search", {}, None)
    look = eng._look(task, eng._step_context(task))
    field = next(a for a in look.affs if a.verb == "type")
    task.memory.no_effect.add(f"{look.sig}|{field.handle()}")
    eng.helper.text = "3 items"          # the screen is otherwise the same
    task.steps.append(Step(0, field.label.replace("(now: ", "(now: xyz"), key=field.key, before=f"{look.sig}:0"))
    again = eng._look(task, eng._step_context(task))
    assert field.label in again.state.get("tried_here_without_effect", []) or \
        not any(a.verb == "type" and a.handle() == field.handle() for a in again.flat), "still withheld, whatever it shows now"


def test_a_step_has_the_same_handle_as_the_action_it_took():
    a = Affordance("w1", "window", "type", "type into text field 「Search」 (now: hi)", {}, key="axpath|AXWindow[0]/AXTextField[0]")
    s = Step(0, a.label, key=a.key)
    assert s.handle == a.handle() == "axpath|AXWindow[0]/AXTextField[0]"
    b = Affordance("w2", "window", "press", "button 「OK」 (selected)", {})
    assert Step(1, b.label).handle == b.handle() == "button 「OK」"


def test_memory_has_one_way_in_and_one_way_out():
    """The keys were built by hand in six places and read back in five; one still used the label a day
    after the rest had moved to handles. Nothing outside these methods spells a key."""
    from macwork.model import Memory
    m = Memory()
    m.note_no_effect("sigA", "button 「Refresh」")
    m.note_failed("sigA", "menu File ▸ Open…", "sigA:123")
    m.note_withdrawn("sigA:123", "button 「2」")
    m.note_withdrawn("sigA:123", "button 「2」")
    m.decline("switch to app Finder")
    ask = lambda handle, state="sigA:123", approval="": m.withheld_reason("sigA", state, handle, approval, limit=2)  # noqa: E731
    assert ask("button 「Refresh」") == "no_effect"
    assert ask("menu File ▸ Open…") == "failed" and ask("menu File ▸ Open…", state="sigA:999") is None, "failed: only while the screen is as it was"
    assert ask("button 「2」") == "withdrawn" and ask("button 「2」", state="sigB:1") is None, "withdrawn: only from that exact state"
    assert ask("switch to app Finder") == "declined" and ask("anything", approval="switch to app Finder") == "declined"
    assert ask("button 「New」") is None
    assert m.no_effect_handles() == {"button 「Refresh」"}
    assert m.withdrawn_from("sigA:123", limit=3) == set(), "told once, withdrawn at the limit"
