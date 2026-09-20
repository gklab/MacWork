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
