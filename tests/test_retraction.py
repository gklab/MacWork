"""A choice the decider made, and then took back itself.

From a real run — "use the calculator to work out 12×12 …", nine steps where three would do:

    screen 12×    chose 「2」        wrong: the next digit of 12×12 is 1
    screen 12×2   chose 「Clear」    it saw that, and took it back
    screen 12×    chose 「2」        the same screen, the same choice, the same mistake
    screen 12×2   chose 「Clear」
    screen 12×    chose 「1」        third time

A System-1 model gives the same answer to the same state, and nothing in the state said "you chose 「2」
from exactly here before, and then undid it". The history said `「2」 -> ok` — and ok means the button
went down, not that it was the right button.

The circle detection that existed could not help. It blames the step that *led back* — 「Clear」 — so the
state told the decider to stop correcting itself, and after three of them would have taken 「Clear」 away.
And it is keyed on the screen's structure, which on a calculator never changes, so here it never fired.

What identifies the moment is the *exact* state — structure and what the screen says — and what is to
blame is the action that left it, not the one that came back. 36% of all steps measured on this Mac were
circling; this is the shape most of them have.
"""

from typing import Any

import pytest

from macwork.model import Step, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def engine(tmp_path, **over):
    from macwork.engine import Engine
    return Engine(cfg(tmp_path, **over), helper=FakeHelper(), decider=ScriptedDecider([]))


def walked(eng, task: Task, *moves: tuple[str, str]) -> None:
    """(exact state the step was taken from, what was chosen)"""
    for here, action in moves:
        eng._note_return(task, here)
        task.steps.append(Step(len(task.steps), action, ok=True, events=["content_changed"], before=here))


# --------------------------------------------------------------------------- the calculator run, replayed

def test_the_action_that_left_is_what_is_remembered_not_the_one_that_came_back(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="12×12", id="t1")
    walked(eng, task, ("s:12×", "button 「2」"), ("s:12×2", "button 「Clear」"))
    eng._note_return(task, "s:12×")                       # back where it chose 「2」
    assert task.memory.retracted.get("s:12×") == ["button 「2」"]
    assert "button 「Clear」" not in str(task.memory.retracted), "the correction was blamed for the mistake"


def test_the_decider_is_told_what_it_took_back_from_this_exact_screen(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="12×12", id="t2")
    walked(eng, task, ("s:12×", "button 「2」"), ("s:12×2", "button 「Clear」"))
    eng._note_return(task, "s:12×")
    assert eng._retracted_here(task, "s:12×") == ["button 「2」"]
    assert eng._retracted_here(task, "s:12×2") == [], "a different screen inherited another one's history"


def test_taken_back_twice_it_is_no_longer_offered_from_there(tmp_path):
    """Once is a fact worth telling. Twice from the same exact state is enough: a System-1 model will
    choose it a third time, because the state it is answering has not changed."""
    eng = engine(tmp_path)
    task = Task(goal="12×12", id="t3")
    walked(eng, task, ("s:12×", "button 「2」"), ("s:12×2", "button 「Clear」"),
           ("s:12×", "button 「2」"), ("s:12×2", "button 「Clear」"))
    eng._note_return(task, "s:12×")
    assert "button 「2」" in eng._withdrawn_here(task, "s:12×")
    assert "button 「2」" not in eng._withdrawn_here(task, "s:12×2"), \
        "withdrawn everywhere, when 「2」 is exactly what 12×1 needs next"


def test_once_is_told_but_still_offered(tmp_path):
    """The first attempt may have failed for a reason that has passed; the decider stays the judge."""
    eng = engine(tmp_path)
    task = Task(goal="x", id="t4")
    walked(eng, task, ("s:a", "press 「Next」"), ("s:b", "press 「Back」"))
    eng._note_return(task, "s:a")
    assert eng._retracted_here(task, "s:a") == ["press 「Next」"]
    assert eng._withdrawn_here(task, "s:a") == set()


# --------------------------------------------------------------------------- what it must not mistake for one

def test_an_action_with_no_effect_is_someone_else_s_business(tmp_path):
    """Pressing something and staying put is `no_effect`, recorded elsewhere; a retraction is a detour."""
    eng = engine(tmp_path)
    task = Task(goal="x", id="t5")
    walked(eng, task, ("s:a", "press 「Nothing」"))
    eng._note_return(task, "s:a")
    assert task.memory.retracted == {}


def test_arriving_somewhere_new_records_nothing(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t6")
    walked(eng, task, ("s:a", "one"), ("s:b", "two"))
    eng._note_return(task, "s:c")
    assert task.memory.retracted == {}


def test_looking_twice_without_acting_counts_once(tmp_path):
    """A look can be repeated before any step is taken (the screen moved, the decider asked to wait)."""
    eng = engine(tmp_path)
    task = Task(goal="x", id="t7")
    walked(eng, task, ("s:a", "go"), ("s:b", "back"))
    eng._note_return(task, "s:a")
    eng._note_return(task, "s:a")
    assert task.memory.retracted["s:a"] == ["go"]


def test_a_failed_step_is_not_a_choice_that_was_taken_back(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t8")
    eng._note_return(task, "s:a")
    task.steps.append(Step(0, "go", ok=False, events=[], before="s:a"))
    task.steps.append(Step(1, "other", ok=True, events=["x"], before="s:b"))
    eng._note_return(task, "s:a")
    assert task.memory.retracted == {}


# --------------------------------------------------------------------------- the key itself

def test_the_exact_state_is_the_same_in_the_next_process(tmp_path):
    """`hash()` of a string is salted per process, and tasks survive a restart — a key made with it would
    quietly never match again."""
    import inspect

    from macwork.loop import exact_state
    # conftest forbids starting processes, so the property is checked at its source: the digest must not be
    # the built-in hash, and a known input must give a known output in every process there will ever be
    import ast
    import textwrap
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(exact_state))))
             if isinstance(n, ast.Call)}
    assert "hash" not in calls, "the key is made with the per-process salted hash"
    assert exact_state("sig1", "12×") == "sig1:" + format(__import__("zlib").crc32("12×".encode()), "08x")


def test_it_survives_the_store(tmp_path):
    from macwork.store import dump, load
    task = Task(goal="x", id="t9")
    task.memory.retracted = {"s:a": ["go", "go"]}
    assert load(dump(task)).memory.retracted == {"s:a": ["go", "go"]}
