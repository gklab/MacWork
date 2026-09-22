"""The working set: a task may only write what it saw.

The guard is `source_of` — a value whose pieces are nowhere in the facts came from nobody, so it is refused.
What these protect: the guard is only as wide as the tokenizer. A script it cannot see at all yields no
pieces, and "made of nothing" used to read as "nothing to check" — so on a Russian, Korean, Greek or Arabic
screen the planner could invent any value and have it typed.
"""

from macwork.facts import Facts, pieces


def test_every_script_is_made_of_something():
    for text in ("Иванов", "한국어", "مرحبا", "Ελλάδα", "ábc"):
        assert pieces(text), f"{text!r} came out as made of nothing"
    assert pieces("計算器") == ["計", "算", "器"]        # ideographs stay one piece each
    assert pieces("abc 3.14") == ["abc", "3.14"]        # numbers keep their decimals
    assert pieces("   —  ") == []                        # genuinely no value


def test_a_value_nobody_saw_is_refused_whatever_the_script():
    facts = Facts(inputs={}, goal="найти телефон")
    assert facts.source_of("Иванов") is None
    assert facts.source_of("서울") is None

    facts.record("Контакты", "Окно", "Иванов Пётр", 0)
    assert facts.source_of("Иванов") == "Контакты (Окно)"


def test_the_goal_and_the_callers_inputs_count_as_seen():
    facts = Facts(inputs={"text": "Ελλάδα"}, goal="найти Иванов")
    assert facts.source_of("Иванов") == "the goal"
    assert facts.source_of("Ελλάδα") == "the caller's inputs"


def test_what_the_task_found_out_is_reported_even_when_it_did_not_finish(tmp_path):
    """The answer used to be written only on the way out through "done". A task asked which item cost the
    most read the file, had every figure in its facts, spent 39 steps in Numbers' sort options and ended
    `failed` with an empty answer — having known it all along."""
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    from tests.test_flex import FakeBackend
    from macwork.engine import Engine

    class Listing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("visible_only") is False:
                return {"nodes": [{"ref": "w.0", "role": "AXWindow", "depth": 0},
                                  {"ref": "w.1", "role": "AXStaticText", "parent": "w.0",
                                   "value": "订书机,9,214\n显示器,1,1790\n鼠标垫,14,96"}]}
            return super().call(method, timeout, **p)

    # it reads the list, then wanders until the step budget is gone: the ending is not "done"
    d = ScriptedDecider([{"pick": "read all the text", "wants_answer": 1.0}] +
                        [{"pick": "press the tab key"} for _ in range(8)])
    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 4},
                                       "observe": {"providers": ["readall", "keys"], "readall": {"offer_over": 0}}}),
                 helper=Listing(), decider=d)
    # wandering on one unchanged screen asks the planner for a route first (it has none), then the answer
    eng._planner = FakeBackend([{"steps": [], "inputs": {}}, {"answer": "显示器"}])

    res = eng.do("这些里面金额最高的是哪一项？")
    assert res["outputs"].get("answer") == "显示器", f"it knew, and said nothing: {res['outputs']}"
    # …and a question that has been answered is not a failure, however badly the rest of it went. What it
    # gave itself as a reason is kept, so the run is still readable as what it was.
    assert res["status"] == "done" and res["outputs"]["unfinished_but_answered"]
