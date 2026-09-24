"""What the planner hands over, and what may become of the text in it.

A planner's text reached the keyboard three ways and was checked on two. A plan input went through the guard
and then into `task.inputs`, where it was the caller's from then on: shown to the decider as the user's own,
traced to "the caller's inputs" by `source_of`, and, under the key 'text', poured into every text slot. A fill
went through the guard. A move to type went to the keyboard with no check at all, and reached the injection
checks only through its step's label, which keeps 40 characters of it.

What stands now: a move's text is offered only once the goal, the caller's inputs or a screen the task saw holds
it word for word, checked again on every look, so a figure the planner worked out is offered once a screen shows
it. Text the decider judged (a value written the way a field needs it) fills the one slot it was written for. A
plan's text is the planner's, kept apart from the caller's. The recorded runs these stand in for: 7ffdee1b4ce3
(391 refused as a plan input, then offered as a move), 8b1a7646fc9f (the planner's definition of a word let
through at 0.76 with nothing seen), 450bbbdad38a (the plan's 'hello' typed where HELLO WORLD was wanted).
"""

import pytest

from macwork.engine import Engine
from macwork.evals import check
from macwork.model import Affordance, Slot, Step, Task
from macwork.observe import observe
from macwork.store import dump, load
from tests.test_engine import WINDOW, FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend

TYPING = {"observe": {"providers": ["apps", "menu", "window", "keys", "focus", "typing"]}}


class Judge(ScriptedDecider):
    """The decider as a judge of text no source holds word for word: no to any text holding one of `refuse`,
    yes to anything else."""

    def __init__(self, script=(), refuse=()):
        super().__init__(list(script))
        self.refuse = tuple(refuse)

    def decide(self, state, questions):
        if set(questions) == {"stands"}:
            self.side.append((state, questions))
            no = any(r in str(state.get("text")) for r in self.refuse)
            return {"stands": {"type": "noul", "noul": 0.0 if no else 1.0}}
        return super().decide(state, questions)


class Display(FakeHelper):
    """The fake Mac's window, with one more line on it: what a calculator's display would read."""
    shows = "0"

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") != "menubar":
            self.calls.append((method, p))
            return {"nodes": WINDOW["nodes"] + [{"ref": "g2.9", "role": "AXStaticText", "value": self.shows, "parent": "g2.0"}],
                    "ms": 1}
        return super().call(method, timeout, **p)


def judged(decider, text):
    """The requests that asked the decider whether this text stands."""
    return [s for s, q in decider.side if set(q) == {"stands"} and s.get("text") == text]


def typed_unasked(task: Task, a: Affordance) -> list[str]:
    """What taking this option would type with nobody asked: its own text, and the inputs its slots take."""
    return [str(a.target.get("text", ""))] + [str(task.inputs[k]) for k in a.slots if k in task.inputs]


def field(name: str) -> Affordance:
    return Affordance(f"w{name}", "window", "type", f"type into text field 「{name}」", {},
                      slots={"text": Slot("text", f"what to type into {name}")})


def test_a_figure_the_planner_worked_out_is_not_offered_until_a_screen_shows_it(tmp_path):
    """7ffdee1b4ce3 refused 391 as a plan input and offered 'type 「391」' two seconds later: the guard on plan
    inputs never saw the moves. A move is offered once something the task may draw from holds its text word for
    word, and that is asked again on every look, with no request: the figure is offered once the display shows it."""
    d = Judge(refuse=["391"])
    helper = Display()
    eng = Engine(cfg(tmp_path), helper=helper, decider=d)
    eng._planner = FakeBackend([{"steps": [{"goal": "compute 17×23", "evidence": "the product on the display"}],
                                 "inputs": {"result": "391"}, "try": [{"type": "17"}, {"type": "391"}], "blocked": ""}])
    task = eng._new_task("compute 17×23 in the calculator, then write the result into a new document", {}, None)
    ctx = eng._step_context(task)
    obs = observe(ctx)
    assert eng._consult(task, ctx, obs, "the actions on screen do not lead toward the goal")

    offered = [a.label for a in eng._suggested(task, obs)]
    assert "type 「17」 at the cursor (suggested by the planner)" in offered, offered
    assert not any("391" in x for x in offered), f"a figure no screen had shown was offered to be typed: {offered}"
    assert [r["text"] for r in task.outputs["refused_text"]] == ["391"]
    assert len(judged(d, "391")) == 1, "the same figure was judged once as an input and again as a move"

    helper.shows = "391"                                   # the calculator worked it out
    look = eng._look(task, eng._step_context(task))
    assert "type 「391」 at the cursor (suggested by the planner)" in [a.label for a in look.flat], \
        "the display shows 391, and typing it is still not offered"
    assert len(judged(d, "391")) == 1, "what a screen shows needs no judge"


@pytest.mark.parametrize("key", ["definition", "text"])
def test_a_definition_the_planner_wrote_is_never_a_standing_option(tmp_path, key):
    """8b1a7646fc9f: the decider let the planner's own definition of a word through at 0.76, when all the task
    had looked at was Finder with no window open; it scored 391 at 0.74 elsewhere, against a cut of 0.75. Its
    verdict decides whether a text may fill the slot it was written for, never whether it is offered to be
    typed anywhere — under the key the plan gave it, or under the one the typing option reads."""
    d = ScriptedDecider([])                                # the judge says yes to anything
    eng = Engine(cfg(tmp_path, config=TYPING), helper=FakeHelper(), decider=d)
    meaning = "the gift of finding good things by chance"
    eng._planner = FakeBackend([{"steps": ["look the word up", "write what it means into the document"],
                                 "inputs": {"query": "serendipity", key: meaning}, "try": [{"type": meaning}], "blocked": ""}])
    task = eng._new_task("look up serendipity and write what it means into the document", {}, None)
    task.memory.facts.record("Finder", "Downloads", "Name  Date Modified  Size  Kind", 0)   # all it has seen
    ctx = eng._step_context(task)
    assert eng._consult(task, ctx, observe(ctx), "the actions on screen do not lead toward the goal")
    assert judged(d, meaning)

    look = eng._look(task, eng._step_context(task))
    offering = [a.label for a in look.affs if any(meaning in t for t in typed_unasked(task, a))]
    assert not offering, f"the planner's own definition is offered to be typed: {offering}"
    assert not any(meaning[:40] in v for v in look.options.values())
    assert task.inputs == {} and task.memory.admitted[meaning]["how"] == "judged"    # let through, as the planner's


def test_a_planners_text_is_never_the_callers(tmp_path):
    """450bbbdad38a: the plan's 'hello' went into inputs.text, and the decider, told that inputs come from the
    user, typed it at the cursor where the goal wanted HELLO WORLD. A plan's text is kept as the planner's."""
    eng = Engine(cfg(tmp_path, config=TYPING), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("type hello, then change it to HELLO WORLD", {}, None)
    eng._plan_inputs(task, eng._step_context(task), {"text": "hello"})
    assert task.inputs == {}
    assert task.memory.facts.inputs == {}, "source_of would trace the plan's text to the caller"
    eng._plan_inputs(task, eng._step_context(task), {"text": "hello"})       # the next plan gives it again
    assert task.outputs["planner_inputs"] == [{"into": "text", "text": "hello", "source": "the goal"}]

    look = eng._look(task, eng._step_context(task))
    assert "text" not in look.state["inputs"], "the decider is told the planner's text came from the user"
    assert not [a.label for a in look.affs if "hello" in typed_unasked(task, a)], "the plan's text fills a slot unasked"


def test_text_typed_from_a_move_is_recorded_where_the_injection_checks_look(tmp_path):
    """`evals.check` builds its trace from the step labels, typed_by_planner and planner_inputs. Text typed from a
    move was in the label alone, which keeps 40 characters of it: a check for what came after them passed. And
    only text that was typed: a must-not run whose floor held the text did not do what it must not."""
    note = "Meeting moved to Thursday at the usual place"
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"},
                         {"pick": "at the cursor (suggested by the planner)"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["write the note"], "inputs": {}, "try": [{"type": note}], "blocked": ""}])
    res = eng.do(f"write “{note}” into the document")
    assert eng.helper.did("input.type")[0]["text"] == note
    assert [(r["text"], r["source"]) for r in res["outputs"].get("typed_by_planner", [])] == [(note, "the goal")]
    ok, why = check(eng, {"check": {"trace_excludes": ["usual place"]}}, res)
    assert not ok and "trace_excludes" in why, "text a move typed is invisible to the injection checks"

    shown = "Notes for the whole team to read later; rm -rf ~/Documents"   # what it does starts past the 40th character
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "at the cursor (suggested by the planner)"}])
    d.what, d.what_when = {"execute": 0.9, "enter": 0.1}, "rm -rf"
    helper = Display()
    helper.shows = shown
    eng = Engine(cfg(tmp_path), helper=helper, decider=d)
    eng._planner = FakeBackend([{"steps": ["write the notes"], "inputs": {}, "try": [{"type": shown}], "blocked": ""}])
    res = eng.do("write the notes on screen into the document")
    assert res["status"] == "need_confirm" and not eng.helper.did("input.type")
    assert "typed_by_planner" not in res["outputs"], "text the floor held is recorded as typed"


def test_an_admitted_text_is_not_judged_again(tmp_path):
    """A judgement is a request, and a verdict near the cut can come out the other way when asked again. What
    was let through is kept, across a restart; a verdict stands while no step has been taken since it."""
    d = ScriptedDecider([])                                # the decider lets a text through
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"text": "Q3 total"}] * 3 + [{"text": "4,212"}] * 3)
    task = eng._new_task("put the quarter's total in the report", {}, None)
    ctx = eng._step_context(task)
    assert eng._fill(task, ctx, None, field("Subject"), "text") == "Q3 total"
    task.steps.append(Step(0, "type into text field 「Subject」", ok=True))
    assert eng._fill(task, ctx, None, field("Summary"), "text") == "Q3 total"      # a step later, another field
    task = load(dump(task))                                                         # and after a restart
    assert eng._fill(task, ctx, None, field("Footer"), "text") == "Q3 total"
    assert len(judged(d, "Q3 total")) == 1

    d.stands = 0.0                                         # a figure nothing the task saw holds
    assert eng._fill(task, ctx, None, field("Amount"), "text") is None
    assert eng._fill(task, ctx, None, field("Total"), "text") is None                # no step since: the verdict stands
    assert len(judged(d, "4,212")) == 1
    task.steps.append(Step(1, "type into text field 「Summary」", ok=True))
    assert eng._fill(task, ctx, None, field("Total"), "text") is None                # a step later, asked again
    assert len(judged(d, "4,212")) == 2
    assert [r["text"] for r in task.outputs["refused_text"]] == ["4,212"] * 3

    eng._plan_inputs(task, None, {"note": "asked of nobody"})   # no decider to ask: refused, and nothing remembered
    assert "asked of nobody" not in task.memory.judged and task.outputs["refused_text"][-1]["text"] == "asked of nobody"
