"""What the numbers are allowed to claim.

Two ways the report overstated its own certainty:

  * the headline interval was a Wilson score over `runs` — tasks × repeats — which treats three runs of
    the same task as three independent trials. They are the most correlated observations in the set: same
    task, same app, same screen. With `repeat: 3` that narrows the reported interval by about √3 for
    nothing.
  * `compare` collapsed each task's repeats to a majority bit before testing, throwing away 3/3 → 1/3 as
    "no change", and pooled all six categories, so a safety regression in four must-not tasks could hide
    inside twenty-nine.
"""

import math

import pytest

from macwork.evals import _rate, _report, mcnemar, compare, wilson


def rows(spec: dict[str, list[bool]], category=lambda t: "A", **extra):
    return [{"id": t, "run": i + 1, "category": category(t), "passed": ok, "valid": True,
             "status": "done" if ok else "failed", "seconds": 1.0, "decider_calls": 1, "cost_usd": 0.0,
             "decider": "jev", **extra}
            for t, runs in spec.items() for i, ok in enumerate(runs)]


def report(spec, repeat=1, **kw):
    import pathlib
    return _report(pathlib.Path("evals/v2.yaml"), b"x", rows(spec, **kw), repeat, None)["summary"]


# --------------------------------------------------------------------------- the interval

def test_repeats_do_not_narrow_the_interval_as_if_they_were_new_tasks():
    once = report({f"t{i}": [i < 5] for i in range(10)}, repeat=1)
    thrice = report({f"t{i}": [i < 5] * 3 for i in range(10)}, repeat=3)
    w_once = once["ci95"][1] - once["ci95"][0]
    w_thrice = thrice["ci95"][1] - thrice["ci95"][0]
    assert w_thrice >= w_once * 0.9, \
        f"running the same ten tasks three times narrowed the interval {w_once:.2f} -> {w_thrice:.2f}"


def test_the_interval_is_over_tasks_and_says_so():
    s = report({f"t{i}": [i < 5] * 3 for i in range(10)}, repeat=3)
    assert s["ci95_over"] == "tasks", s.get("ci95_over")
    assert s["tasks"] == 10


def test_a_task_that_is_flaky_is_not_counted_as_a_pass_or_a_fail_twice():
    s = report({"steady": [True] * 3, "flaky": [True, False, False]}, repeat=3)
    assert s["passed_tasks"] == 1 and s["tasks"] == 2


def test_the_per_run_rate_is_still_reported():
    """It is the more useful number for "how often does this work"; it just is not the interval."""
    s = report({"a": [True, False, True]}, repeat=3)
    assert s["success_rate"] == pytest.approx(2 / 3, abs=0.01)


def test_wilson_itself_is_unchanged():
    lo, hi = wilson(14, 29)
    assert lo == pytest.approx(0.314, abs=0.005) and hi == pytest.approx(0.656, abs=0.005)


# --------------------------------------------------------------------------- the comparison

def rep(spec, sha="aa", decider="jev", repeat=1):
    return {"summary": {"suite_sha256": sha, "repeat": repeat},
            "rows": [{"id": t, "passed": ok, "valid": True, "status": "done" if ok else "failed",
                      "decider": decider, "category": "E must-not" if t.startswith("e") else "A nav"}
                     for t, runs in spec.items() for ok in runs]}


def test_a_rate_that_fell_without_flipping_is_not_reported_as_no_change():
    c = compare(rep({"a": [True] * 3}, repeat=3), rep({"a": [True, False, False]}, repeat=3))
    assert c["broke"] == ["a"] or c["rate_moved"], "3/3 -> 1/3 came back as nothing having moved"


def test_categories_are_compared_as_well_as_the_whole():
    before = rep({**{f"a{i}": [True] for i in range(20)}, **{f"e{i}": [True] for i in range(4)}})
    after = rep({**{f"a{i}": [True] for i in range(20)}, **{f"e{i}": [False] for i in range(4)}})
    c = compare(before, after)
    assert "by_category" in c
    e = c["by_category"]["E must-not"]
    assert e["broke"] == 4 and e["fixed"] == 0, e


def test_a_safety_regression_is_not_lost_in_the_pool():
    """Four must-not tasks breaking inside twenty-nine is what the pooled test cannot see."""
    before = rep({**{f"a{i}": [i < 10] for i in range(25)}, **{f"e{i}": [True] for i in range(4)}})
    after = rep({**{f"a{i}": [i < 14] for i in range(25)}, **{f"e{i}": [False] for i in range(4)}})
    c = compare(before, after)
    assert "must-not" in c["verdict"] or any("must-not" in w for w in c["warnings"]), c["verdict"]


def test_it_still_says_when_nothing_moved():
    same = {"a": [True], "b": [False]}
    assert "nothing moved" in compare(rep(same), rep(same))["verdict"]
