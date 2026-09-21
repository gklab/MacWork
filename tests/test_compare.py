"""Did anything actually change between two eval runs?

`evals/v2.yaml, 29 tasks: 12/29 -> 14/29` reads like progress and is not evidence of any. Two tasks
flipping one way and none the other is a different thing from five flipping one way and three the other,
and the totals cannot tell them apart — the tasks that did not move carry no information about whether the
change did anything, which is exactly what a paired test uses and a difference of totals throws away.

The reports were also being written and then left on whichever machine produced them, so no second run
could be held against a first at all.
"""

import json

import pytest

from macwork.evals import compare, compare_files, format_compare, mcnemar


def report(passed: dict[str, list[bool]], sha: str = "aaaa", decider: str = "jev", repeat: int = 1) -> dict:
    rows = [{"id": tid, "passed": ok, "valid": True, "status": "done" if ok else "failed", "decider": decider}
            for tid, runs in passed.items() for ok in runs]
    return {"summary": {"suite_sha256": sha, "repeat": repeat}, "rows": rows}


# --------------------------------------------------------------------------- the test itself

@pytest.mark.parametrize("b,c,expected", [(0, 0, 1.0), (1, 0, 1.0), (2, 0, 0.5), (5, 3, 0.727), (9, 1, 0.021)])
def test_the_exact_test_matches_its_definition(b, c, expected):
    assert mcnemar(b, c) == pytest.approx(expected, abs=0.001)


def test_it_is_symmetric_because_chance_has_no_preferred_direction():
    assert mcnemar(7, 2) == mcnemar(2, 7)


# --------------------------------------------------------------------------- what it says about a run

def test_two_more_passes_out_of_twenty_nine_is_not_evidence_of_anything():
    """The number this was written for."""
    before = report({f"t{i}": [i < 12] for i in range(29)})
    after = report({f"t{i}": [i < 14] for i in range(29)})
    c = compare(before, after)
    assert c["fixed"] == ["t12", "t13"] and c["broke"] == []
    assert c["p_mcnemar"] == 0.5 and "chance" in c["verdict"]


def test_a_real_improvement_is_said_to_be_one():
    before = report({f"t{i}": [False] for i in range(20)})
    after = report({f"t{i}": [i < 9] for i in range(20)})
    c = compare(before, after)
    assert c["p_mcnemar"] < 0.05 and "more than chance" in c["verdict"]


def test_the_same_total_can_still_hide_a_lot_moving():
    """12 -> 12 with six tasks swapping places is not "no change", and a total says it is."""
    before = report({**{f"a{i}": [True] for i in range(6)}, **{f"b{i}": [False] for i in range(6)}})
    after = report({**{f"a{i}": [False] for i in range(6)}, **{f"b{i}": [True] for i in range(6)}})
    c = compare(before, after)
    assert c["before"]["passed"] == c["after"]["passed"] == 6
    assert len(c["fixed"]) == 6 and len(c["broke"]) == 6


def test_which_tasks_moved_is_named_in_both_directions():
    c = compare(report({"keep": [True], "fix": [False], "break": [True]}),
                report({"keep": [True], "fix": [True], "break": [False]}))
    assert c["fixed"] == ["fix"] and c["broke"] == ["break"]


def test_nothing_moving_is_said_plainly():
    same = {"a": [True], "b": [False]}
    assert "nothing moved" in compare(report(same), report(same))["verdict"]


# --------------------------------------------------------------------------- refusing a meaningless comparison

def test_a_different_suite_is_two_different_questions():
    c = compare(report({"a": [True]}, sha="1111"), report({"a": [True]}, sha="2222"))
    assert any("different suites" in w for w in c["warnings"])


def test_a_different_decider_measures_the_model_not_the_change():
    c = compare(report({"a": [False]}, decider="jev"), report({"a": [True]}, decider="local:x"))
    assert any("different deciders" in w for w in c["warnings"])


def test_tasks_present_in_only_one_run_are_left_out_and_said_so():
    c = compare(report({"a": [True], "gone": [True]}), report({"a": [True], "new": [False]}))
    assert c["tasks"] == 1 and any("only one of the runs" in w for w in c["warnings"])


def test_a_run_that_errored_is_not_an_outcome():
    """A task the decider could not be reached for says nothing about ability, and must not count as a fail."""
    before = report({"a": [True]})
    after = report({"a": [True]})
    after["rows"].append({"id": "b", "passed": False, "valid": False, "status": "error", "decider": "jev"})
    before["rows"].append({"id": "b", "passed": True, "valid": True, "status": "done", "decider": "jev"})
    assert compare(before, after)["broke"] == []


# --------------------------------------------------------------------------- repeats

def test_with_repeats_a_task_is_judged_by_where_most_of_its_runs_went():
    c = compare(report({"a": [True, True, False]}, repeat=3), report({"a": [False, False, True]}, repeat=3))
    assert c["broke"] == ["a"]


def test_a_rate_that_moved_without_flipping_is_still_reported():
    c = compare(report({"a": [True, True, True]}, repeat=3), report({"a": [True, True, False]}, repeat=3))
    assert c["broke"] == [] and c["rate_moved"] == [{"id": "a", "before": "3/3", "after": "2/3"}]


# --------------------------------------------------------------------------- plumbing

def test_it_reads_the_reports_the_harness_writes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(report({"t": [False]})), encoding="utf-8")
    b.write_text(json.dumps(report({"t": [True]})), encoding="utf-8")
    assert compare_files(a, b)["fixed"] == ["t"]


def test_the_summary_a_person_reads_names_the_tasks_and_the_doubt():
    out = format_compare(compare(report({f"t{i}": [i < 12] for i in range(29)}),
                                 report({f"t{i}": [i < 14] for i in range(29)})))
    assert "12/29 → 14/29" in out and "t12" in out and "chance" in out
