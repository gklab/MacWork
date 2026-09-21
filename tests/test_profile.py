"""Where the time goes, from tasks that actually ran.

Every change to the engine's speed or caution in this round was made without a way to see what it did, and
the first real eval had to overturn three of them. `macwork profile` reads the task store and reports the
two things wall-clock time is made of — what a step costs, and how many steps were wasted.

Its very first run found something else: the numbers were nonsense (a median decide of 29 ms), because
every Engine a unit test builds had been persisting its fake tasks into the person's real task store —
704 of the 715 tasks in it.
"""

import json
import sqlite3

from macwork.profile import format_profile, profile


def store(tmp_path, tasks):
    db = tmp_path / "tasks.db"
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text primary key, updated real, state text)")
    for i, t in enumerate(tasks):
        con.execute("insert into tasks values (?, ?, ?)", (f"t{i}", float(i), json.dumps(t)))
    con.commit()
    con.close()
    return db


def step(sig, **timing):
    return {"ok": True, "before": f"{sig}:1", "decision": {"timing": timing}}


def test_it_says_which_stage_a_step_is_made_of(tmp_path):
    db = store(tmp_path, [{"steps": [step("a", observe=300, decide=1000, act=10, wait=200)] * 4}])
    p = profile(db)
    assert p["share_of_a_step"]["decide"] > 0.6, "the stage that dominates has to be visible as dominating"
    assert p["stages"]["decide"]["median_ms"] == 1000


def test_wasted_steps_are_counted_because_they_cost_as_much_as_slow_ones(tmp_path):
    db = store(tmp_path, [{"steps": [step("a"), step("b"), step("a")], "memory": {"circles": ["x", "y"]}}])
    assert profile(db)["wasted"]["circling_steps"] == 2


def test_steps_on_an_unchanged_screen_are_the_ceiling_for_deciding_in_sequences(tmp_path):
    """Only a step that follows another on the same screen could have shared its round trip."""
    db = store(tmp_path, [{"steps": [step("calc")] * 5 + [step("other")]}])
    s = profile(db)["same_screen"]
    assert s["continuations"] == 4 and s["in_stretches_of_3_or_more"] == 4


def test_nothing_to_read_says_so_rather_than_printing_zeros(tmp_path):
    assert "no stored tasks" in format_profile(profile(tmp_path / "missing.db"))


def test_a_corrupt_row_does_not_take_the_report_down(tmp_path):
    db = store(tmp_path, [{"steps": [step("a", decide=5)]}])
    con = sqlite3.connect(db)
    con.execute("insert into tasks values ('bad', 9.0, 'not json')")
    con.commit()
    con.close()
    assert profile(db)["tasks"] == 1


def test_tests_do_not_write_to_the_real_task_store(tmp_path):
    """The defect the first run of this command found."""
    import os
    from pathlib import Path

    from macwork.config import Config
    from macwork.store import Store
    real = Path("~/Library/Application Support/macwork/tasks.db").expanduser()
    assert Store(Config.load()).path != real, "a test-built Engine would persist into the person's own store"
    assert "MACWORK__ENGINE__STORE" in os.environ
