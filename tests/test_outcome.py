"""What an action did, said in the history — not only that it was carried out.

The history the decider reads was `button 「2」 -> ok (ui: content_changed)`. "ok" means the button went
down. It does not say the display went from 12× to 12×2, which is the one thing that would have shown the
decider its mistake — and in the stored run of "12×12" it made that mistake twice from the same screen.

Every project that drives a game well with this model puts the *result* of the last action in front of it:
the Mario controller's state carries "chosen action, duration, progress gained, observed outcome", and the
Minecraft agent's notes put their whole 49-minute -> 9-minute improvement down to "the harness now checks
action results" rather than to anything said to the model. The arithmetic stays in code; the model is
handed the fact.

The engine already holds both halves — the screen before and the screen after — at the moment it looks
again. This is only the subtraction.
"""

import pytest

from macwork.loop import what_changed


def changed(before, after, **kw):
    return what_changed(before_text=before, after_text=after, **kw)


# --------------------------------------------------------------------------- the calculator run

def test_one_line_turning_into_another_is_said_as_that():
    assert changed("‎12‎×", "‎12‎×‎2") == "「12×」 became 「12×2」"


def test_clearing_reads_as_the_reverse():
    assert changed("12×2", "12×") == "「12×2」 became 「12×」"


def test_invisible_marks_are_not_a_change():
    """The calculator's value is full of U+200E; a display that gained one has not changed."""
    assert changed("‎12", "12‎") == "nothing on screen changed"


# --------------------------------------------------------------------------- the general shapes

def test_something_appearing_is_named():
    out = changed("Inbox\nDrafts", "Inbox\nDrafts\nSave changes before closing?")
    assert "appeared" in out and "Save changes before closing?" in out


def test_something_going_away_is_named():
    out = changed("Inbox\nDrafts\nLoading…", "Inbox\nDrafts")
    assert "gone" in out and "Loading…" in out


def test_a_toggle_shows_as_what_it_is():
    """A status-bar menu: press it and a panel appears, press it again and it goes. A stored run pressed
    「Clock」 three times; the second press's outcome would have read as the first one undone."""
    opened = changed("Desktop", "Desktop\nMonday 21 September\nOpen Date & Time Settings…")
    closed = changed("Desktop\nMonday 21 September\nOpen Date & Time Settings…", "Desktop")
    assert "appeared" in opened and "gone" in closed


def test_a_new_window_is_said_first():
    out = changed("a", "b", before_window="Untitled", after_window="Open")
    assert out.startswith("window 「Untitled」 → 「Open」")


def test_landing_in_another_app_is_said_first_of_all():
    out = changed("a", "b", before_app="TextEdit", after_app="Finder", before_window="x", after_window="y")
    assert out.startswith("now in Finder")


def test_nothing_changing_is_said_plainly():
    assert changed("same", "same") == "nothing on screen changed"


# --------------------------------------------------------------------------- it stays small

def test_a_wall_of_new_text_is_summarised_not_pasted():
    after = "\n".join(f"line number {i} of a long document" for i in range(200))
    out = changed("", after)
    assert len(out) <= 200 and "more" in out


def test_one_long_line_is_cut():
    assert len(changed("", "x" * 5000)) <= 200


# --------------------------------------------------------------------------- it reaches the decider

def test_the_history_carries_the_outcome_when_there_is_one(tmp_path):
    from macwork.engine import Engine
    from macwork.model import Step, Task
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="x", id="t")
    task.steps.append(Step(0, "button 「2」", ok=True, events=["content_changed"], outcome="「12×」 became 「12×2」"))
    assert eng._history(task) == ["button 「2」 -> 「12×」 became 「12×2」"]


def test_a_step_with_no_outcome_yet_reads_as_before(tmp_path):
    from macwork.engine import Engine
    from macwork.model import Step, Task
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="x", id="t")
    task.steps.append(Step(0, "press it", ok=True, events=["AXValueChanged"]))
    assert eng._history(task) == ["press it -> ok (ui: AXValueChanged)"]


def test_a_failure_says_why(tmp_path):
    from macwork.engine import Engine
    from macwork.model import Step, Task
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="x", id="t")
    task.steps.append(Step(0, "type it", ok=False, events=[], error="the field holds 'abc', not what was typed"))
    assert "not what was typed" in eng._history(task)[0]


def test_an_app_that_reacted_without_changing_any_text_is_not_said_to_have_done_nothing(tmp_path):
    """Found by an older test: the UI posted AXValueChanged and no readable text moved. "Nothing changed"
    would have been false, and the decider would have been told a working action was dead."""
    from macwork.engine import Engine
    from macwork.loop import NOTHING_CHANGED
    from macwork.model import Step, Task
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = Task(goal="x", id="t")
    task.steps.append(Step(0, "press it", ok=True, events=["AXValueChanged"], outcome=NOTHING_CHANGED))
    line = eng._history(task)[0]
    assert "AXValueChanged" in line and "no text on screen changed" in line
