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
    said = format_profile(profile(tmp_path / "missing.db"))
    assert "no stored tasks" in said and "0 tasks" not in said


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


# --------------------------------------------------------------------------- the audit's step records

def steps_of(path):
    return [r for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()) if r["kind"] == "step"]


def test_every_step_leaves_one_timing_record(tmp_path):
    from macwork.engine import Engine
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "done"}]))
    res = eng.do("make a new document")
    recs = steps_of(tmp_path / "audit.jsonl")
    assert [(r["n"], r.get("end")) for r in recs] == [(0, None), (1, "done")], "one per step, and one for the looks after it"
    step = recs[0]
    assert step["channel"] == "menu" and step["ok"] is True and step["looks"] == 1 and step["decisions"] >= 1
    assert step["observe_ms"] + step["decide_ms"] + step["act_ms"] + step["wait_ms"] <= step["wall_ms"]
    assert all(isinstance(v, int) for v in step["providers"].values())
    assert not any(isinstance(v, str) and len(v) > 12 for r in recs for v in r.values() if v not in (r["task"],)), \
        "a step record holds numbers and names of kinds, never a label or text"
    timing = res["timing"]
    assert timing["steps"] == 1 and timing["looks"] == sum(r["looks"] for r in recs)
    assert timing["wall_ms"] == sum(r["wall_ms"] for r in recs) and timing["observe_ms"] == sum(r["observe_ms"] for r in recs)
    assert "timing" not in res["outputs"], "an output check reads outputs: where the time went is not an output"
    assert eng.store.get(res["task_id"]).timing == timing, "the task was kept without the looks after its last action"


def test_planner_time_is_in_the_step_it_delayed(tmp_path):
    import time
    from macwork.engine import Engine
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg

    class Slow:
        name, local = "slow-cloud", False

        def complete(self, system, prompt, schema):
            time.sleep(0.03)
            return {"steps": [{"goal": "make it", "evidence": "a new document"}], "inputs": {}, "try": [], "blocked": ""}

    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}), helper=FakeHelper(),
                 decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "done"}]))
    eng._planner = Slow()
    eng.do("make a new document")
    first = steps_of(tmp_path / "audit.jsonl")[0]
    assert first["planner_calls"] == 1 and first["planner_ms"] >= 30 and first["looks"] == 2
    assert first["planner_ms"] <= first["wall_ms"]


def test_profile_reads_the_audit_and_says_how_much_was_the_planner(tmp_path):
    from macwork.profile import audit_files, format_timing, profile_audit
    audit = tmp_path / "audit.jsonl"
    older = tmp_path / "audit.1.jsonl"
    step = {"kind": "step", "task": "t1", "looks": 1, "observe_ms": 300, "decide_ms": 600, "decide_net_ms": 500,
            "act_ms": 50, "wait_ms": 50, "planner_ms": 0, "planner_calls": 0, "decisions": 2, "providers": {"menu": 120, "window": 60}}
    # the planner is bursty: one step waits 20 s on it and nine do not wait at all
    older.write_text("\n".join(json.dumps({**step, "n": i, "wall_ms": 1000}) for i in range(9)) + "\n", encoding="utf-8")
    audit.write_text(json.dumps({**step, "n": 9, "looks": 2, "wall_ms": 21000, "planner_ms": 20000, "planner_calls": 1}) + "\n"
                     + json.dumps({"kind": "step", "task": "t1", "n": 10, "end": "done", "looks": 1, "wall_ms": 1000}) + "\n"
                     + json.dumps({"kind": "decide", "task": "t1"}) + "\n", encoding="utf-8")
    p = profile_audit(audit_files(audit))
    assert (p["steps"], p["tasks"], p["planner_calls"]) == (10, 1, 1)
    assert p["share_of_task_time"]["planner"] == round(20000 / 31000, 3), "from sums: a median step waits on no planner"
    assert p["stages"]["planner"]["median_ms"] == 0 and p["stages"]["decide"]["median_ms"] == 600
    assert p["slowest_providers"][0]["provider"] == "menu"
    said = format_timing(p)
    assert f"{p['share_of_task_time']['planner']:.0%}" in next(x for x in said.splitlines() if x.startswith("planner"))
    assert profile_audit(audit_files(audit), {"another-task"})["steps"] == 0


def test_the_audit_is_read_a_line_at_a_time(tmp_path, monkeypatch):
    """Each file of the audit grows to 64 MB before it rotates, and `macwork profile` reads four of them — and so
    does `macwork doctor` whenever the task store has nothing. Each was read whole into memory, then split."""
    import pathlib
    from macwork.profile import audit_files, profile_audit
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({"kind": "step", "task": "t1", "n": 0, "looks": 1, "wall_ms": 1000, "observe_ms": 300}) + "\n"
                     + json.dumps({"kind": "decide", "task": "t1", "state": {"screen_text": "x" * 1000}}) + "\n", encoding="utf-8")

    def whole(self, *a, **k):
        raise AssertionError(f"{self.name} was read whole")
    monkeypatch.setattr(pathlib.Path, "read_text", whole)
    assert profile_audit(audit_files(audit))["steps"] == 1


def test_a_reports_own_rows_are_enough_to_profile_it():
    from macwork.profile import format_timing, profile_report
    rows = [{"id": "a", "timing": {"steps": 3, "looks": 5, "wall_ms": 30000, "observe_ms": 3000, "decide_ms": 6000,
                                   "act_ms": 300, "wait_ms": 700, "planner_ms": 15000, "planner_calls": 2, "decisions": 9}},
            {"id": "b", "timing": {"steps": 1, "looks": 1, "wall_ms": 10000, "observe_ms": 1000, "decide_ms": 2000,
                                   "act_ms": 100, "wait_ms": 100, "planner_ms": 5000, "planner_calls": 1, "decisions": 3}},
            {"id": "c", "status": "invalid"}]
    p = profile_report({"rows": rows})
    assert (p["source"], p["tasks"], p["steps"], p["looks_per_step"]) == ("report", 2, 4, 1.5)
    assert p["share_of_task_time"]["planner"] == 0.5 and p["share_of_task_time"]["decide"] == 0.2
    assert "from the report" in format_timing(p)


def test_an_empty_store_says_why(tmp_path):
    """On 09-23 `macwork profile -n 50` said "no stored tasks to read (engine.persist is off, or nothing has run
    yet)". Persisting was on: the store drops a task 30 minutes after it was last touched, and the run it was
    asked about was hours older than that."""
    said = format_profile(profile(tmp_path / "missing.db"))
    assert "engine.tasks_ttl_s" in said and "audit" in said


def test_a_step_whose_channel_failed_is_not_charged_the_last_ones_act_and_wait(tmp_path):
    """A channel that raises hands back no act or wait time of its own, and the step took the one before it's:
    a key that could not be posted read as taking as long as the menu command before it."""
    import time
    from macwork.engine import Engine
    from macwork.helper import HelperError
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg

    class SlowThenStuck(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform" and p.get("ref") == "g1.6":        # File ▸ New Document takes its time
                time.sleep(0.05)
            if method == "input.key" and p.get("combo") == "escape":     # and the key after it cannot be posted
                self.calls.append((method, p))
                raise HelperError("event_failed", "the key could not be posted")
            return super().call(method, timeout, **p)

    eng = Engine(cfg(tmp_path), helper=SlowThenStuck(),
                 decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "press the escape key"}, {"pick": "done"}]))
    res = eng.do("make a new document")
    menu, key = steps_of(tmp_path / "audit.jsonl")[:2]
    assert menu["act_ms"] >= 50 and key["ok"] is False
    assert (key["act_ms"], key["wait_ms"]) == (0, 0), key
    assert "act" not in eng.tasks[res["task_id"]].steps[1].decision["timing"], "the stored step took the last one's act too"


def test_profile_reads_one_runs_tasks_and_falls_back_to_its_rows(tmp_path, capsys, monkeypatch):
    """`macwork profile --report R.json`: that run's tasks from the audit, and the report's own rows once the
    audit has rotated past them."""
    from macwork import cli
    audit = tmp_path / "audit.jsonl"
    rec = {"kind": "step", "task": "mine", "n": 0, "looks": 1, "wall_ms": 2000, "observe_ms": 500, "decide_ms": 1000,
           "act_ms": 100, "wait_ms": 100, "planner_ms": 0, "planner_calls": 0, "decisions": 2}
    audit.write_text(json.dumps(rec) + "\n" + json.dumps({**rec, "task": "someone-else", "wall_ms": 9000}) + "\n", encoding="utf-8")
    monkeypatch.setenv("MACWORK__AUDIT__PATH", str(audit))
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"rows": [{"id": "a", "task_id": "mine"}]}), encoding="utf-8")
    assert cli.main(["profile", "--report", str(report), "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["source"], got["tasks"], got["task_seconds"]) == ("audit", 1, 2.0), "another task's steps were read"
    report.write_text(json.dumps({"rows": [{"id": "a", "task_id": "rotated-away", "timing": {
        "steps": 2, "looks": 2, "wall_ms": 4000, "observe_ms": 800, "decide_ms": 1600, "act_ms": 100, "wait_ms": 100,
        "planner_ms": 1000, "planner_calls": 1, "decisions": 4}}]}), encoding="utf-8")
    assert cli.main(["profile", "--report", str(report)]) == 0
    said = capsys.readouterr().out
    assert "from the report" in said and "25%" in next(x for x in said.splitlines() if x.startswith("planner"))


def test_the_doctor_reads_the_audit_when_the_store_has_nothing(tmp_path, capsys, monkeypatch):
    """`macwork doctor` sets the decider's round trip beside a step on this Mac, and took the step from the task
    store: with the store empty the line said nothing about the step."""
    import argparse

    import macwork.engine as engine
    from macwork import cli
    from macwork.config import Config

    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps({"kind": "step", "task": "t1", "n": i, "looks": 1, "wall_ms": 1500, "observe_ms": 300,
                                           "decide_ms": 800, "act_ms": 100, "wait_ms": 200}) for i in range(3)) + "\n",
                     encoding="utf-8")
    monkeypatch.setenv("MACWORK__AUDIT__PATH", str(audit))

    class Decider:
        def route_ms(self):
            return 280.0

    class Eng:
        cfg = Config.load()
        helper = None
        decider = Decider()

        def status(self):
            return {"helper": {"ax_trusted": True}, "decider": {"kind": "jev", "using": "jev"}}

    monkeypatch.setattr(engine, "Engine", lambda cfg: Eng())      # cmd_doctor imports it inside the function
    cli.cmd_doctor(Config.load(), argparse.Namespace(planners=False, ask=False))
    assert "about 20% of a 1400 ms step on this Mac" in capsys.readouterr().err
