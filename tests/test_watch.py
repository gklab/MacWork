"""Something happening on the Mac starting a task.

The engine could be asked to do a thing and could be resumed, but nothing ever woke it: the daemon sat
there and every task began because a person started one. What was missing is not a scheduler — macOS has
one — but "when this happens, do that".

A trigger is a *caller*, like a person or an MCP client. It starts the loop from outside and so inherits
the safety floor, the facts, the redaction and the budgets without re-implementing any of them; nothing in
`watch.py` drives the Mac. Which apps and folders matter is not known here and must not be — it comes from
the person's own file.
"""

from typing import Any

import pytest
import yaml

from macwork.config import Config
from macwork.watch import Trigger, Watcher, load_triggers, subscription


class Engine:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.submitted: list[tuple[str, dict[str, Any]]] = []
        self.watched: dict[str, Any] = {}
        self.helper = self
        self.cfg = Config.load()

    def call(self, method: str, timeout: float = 30.0, **p: Any) -> Any:
        if method == "events.watch":
            self.watched = p
            return {"watching": p, "kinds": ["app.launched", "file.changed"]}
        if method == "events.poll":
            after = p.get("after", 0)
            out = [e for e in self.events if e["seq"] > after]
            return {"events": out, "seq": max([e["seq"] for e in self.events], default=0),
                    "oldest": min([e["seq"] for e in self.events], default=1), "dropped": 0}
        raise AssertionError(method)

    def submit(self, goal: str, inputs: dict[str, Any] | None = None, app: str | None = None) -> dict[str, Any]:
        self.submitted.append((goal, inputs or {}))
        return {"task_id": f"t{len(self.submitted)}", "status": "queued", "position": len(self.submitted)}


def rules(*raw: dict[str, Any]) -> list[Trigger]:
    return [Trigger(r, i + 1) for i, r in enumerate(raw)]


def cfg_with(tmp_path, doc) -> Config:
    path = tmp_path / "triggers.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return Config.load(overrides={"config": {"watch": {"file": str(path)}}})


# --------------------------------------------------------------------------- the rules are the person's

def test_rules_come_from_a_file_that_is_not_shipped(tmp_path):
    cfg = cfg_with(tmp_path, {"triggers": [{"when": "screen.unlocked", "do": "看一眼日历"}]})
    assert [t.goal for t in load_triggers(cfg)] == ["看一眼日历"]


def test_no_file_is_no_triggers_rather_than_an_error():
    assert load_triggers(Config.load(overrides={"config": {"watch": {"file": "/nonexistent/x.yaml"}}})) == []


def test_a_rule_with_no_goal_is_refused_when_it_is_read_not_when_it_fires():
    with pytest.raises(ValueError, match="needs a 'do'"):
        Trigger({"when": "app.launched"}, 1)


def test_a_misspelt_condition_is_refused_rather_than_never_matching():
    with pytest.raises(ValueError, match="bundleid"):
        Trigger({"when": {"bundleid": "com.example"}, "do": "x"}, 1)


# --------------------------------------------------------------------------- matching

def test_the_kind_of_event_can_be_a_pattern():
    t = rules({"when": "app.*", "do": "x"})[0]
    assert t.matches({"kind": "app.launched"}) and not t.matches({"kind": "file.changed"})


def test_a_folder_is_matched_as_a_glob_so_a_subtree_needs_no_new_syntax():
    t = rules({"when": {"event": "file.changed", "path": "~/D/*.pdf"}, "do": "x"})[0]
    assert t.matches({"kind": "file.changed", "path": "~/D/发票.pdf"})
    assert not t.matches({"kind": "file.changed", "path": "~/D/notes.txt"})


def test_every_condition_given_has_to_hold():
    t = rules({"when": {"event": "app.launched", "bundle_id": "com.example.thing"}, "do": "x"})[0]
    assert not t.matches({"kind": "app.launched", "bundle_id": "com.other"})


def test_one_that_is_switched_off_matches_nothing():
    assert not rules({"when": "app.*", "do": "x", "enabled": False})[0].matches({"kind": "app.launched"})


# --------------------------------------------------------------------------- only subscribe to what is needed

def test_watching_a_folder_does_not_also_poll_the_clipboard():
    """A stream and a poll each cost something; neither should start because another rule needed apps."""
    import os
    sub = subscription(Config.load(), rules({"when": {"event": "file.changed", "path": "~/D"}, "do": "x"}))
    # expanded, because the helper watches an absolute path and the matcher has to compare against the
    # same one — the two deriving it separately is what made the documented example never fire
    assert sub["paths"] == [os.path.expanduser("~/D")] and not sub["clipboard_every_s"] and not sub["apps"]


def test_what_a_rule_does_need_is_subscribed_to():
    sub = subscription(Config.load(), rules({"when": "clipboard.changed", "do": "x"},
                                            {"when": "screen.unlocked", "do": "y"}))
    assert sub["clipboard_every_s"] and sub["screen_lock"] and sub["paths"] == []


def test_a_glob_in_the_path_is_cut_back_to_the_folder_to_watch():
    sub = subscription(Config.load(), rules({"when": {"event": "file.changed", "path": "/tmp/a/*.pdf"}, "do": "x"}))
    assert sub["paths"] == ["/tmp/a"]


# --------------------------------------------------------------------------- firing

def test_a_matching_event_hands_work_to_the_engine(tmp_path):
    eng = Engine([{"seq": 1, "kind": "app.launched", "app": "计算器"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "app.launched", "do": "记一笔"}]}))
    w.start()
    w.seq = 0
    assert [f["trigger"] for f in w.tick()] and eng.submitted[0][0] == "记一笔"


def test_what_happened_is_handed_over_as_an_input(tmp_path):
    """It is text from outside like any other, and takes the same road through redaction."""
    eng = Engine([{"seq": 1, "kind": "file.changed", "path": "/tmp/发票.pdf"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "file.changed", "do": "归档"}]}))
    w.start()
    w.seq = 0
    w.tick()
    assert eng.submitted[0][1]["event"]["path"] == "/tmp/发票.pdf"


def test_nothing_matching_starts_nothing(tmp_path):
    eng = Engine([{"seq": 1, "kind": "screen.locked"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "app.launched", "do": "x"}]}))
    w.start()
    w.seq = 0
    assert w.tick() == [] and eng.submitted == []


def test_a_folder_being_written_to_does_not_start_one_task_per_file(tmp_path):
    eng = Engine([{"seq": i, "kind": "file.changed", "path": f"/tmp/{i}.pdf"} for i in range(1, 6)])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "file.changed", "do": "归档", "debounce_s": 60}]}))
    w.start()
    w.seq = 0
    assert len(w.tick()) == 1


def test_being_away_too_long_is_said_rather_than_passed_over(tmp_path, caplog):
    import logging
    eng = Engine([{"seq": 900, "kind": "app.launched"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "app.launched", "do": "x"}]}))
    w.start()
    w.seq = 5
    with caplog.at_level(logging.WARNING, logger="macwork.watch"):
        w.tick()
    assert "missed" in caplog.text


def test_it_starts_from_now_rather_than_replaying_the_backlog(tmp_path):
    """Whatever happened before watching began is not something to act on."""
    eng = Engine([{"seq": 1, "kind": "app.launched"}, {"seq": 2, "kind": "app.launched"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": "app.launched", "do": "x"}]}))
    w.start()
    assert w.tick() == []


def test_nothing_here_drives_the_mac():
    """It is a caller. If it acted itself it would be going round the floor, the facts and the budgets."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("macwork/watch.py").read_text())
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("self.engine.act", "self.engine.do", "self.engine._execute", "self.engine._run"):
        assert forbidden not in called, f"{forbidden} drives the Mac from a caller"
    helper_calls = {ast.unparse(n.args[0]) for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("helper.call") and n.args}
    assert helper_calls <= {"'events.watch'", "'events.poll'"}, helper_calls


# --------------------------------------------------------------------------- paths the two sides agree on

def test_the_example_in_the_docs_actually_fires(tmp_path):
    """docs/triggers.md leads with `path: ~/Downloads/*.pdf`. The Swift side expands `~` before it watches
    the folder, so the events come back as `/Users/<you>/Downloads/x.pdf` — and the matcher compared that
    against the unexpanded pattern. The watch was set on the right folder and nothing ever matched."""
    import os
    t = rules({"when": {"event": "file.changed", "path": "~/Downloads/*.pdf"}, "do": "归档"})[0]
    real = os.path.expanduser("~/Downloads/发票.pdf")
    assert t.matches({"kind": "file.changed", "path": real})


def test_it_still_does_not_match_a_different_folder(tmp_path):
    import os
    t = rules({"when": {"event": "file.changed", "path": "~/Downloads/*.pdf"}, "do": "x"})[0]
    assert not t.matches({"kind": "file.changed", "path": os.path.expanduser("~/Documents/发票.pdf")})
    assert not t.matches({"kind": "file.changed", "path": os.path.expanduser("~/Downloads/notes.txt")})


def test_an_absolute_pattern_is_unaffected():
    t = rules({"when": {"event": "file.changed", "path": "/tmp/box/*.pdf"}, "do": "x"})[0]
    assert t.matches({"kind": "file.changed", "path": "/tmp/box/a.pdf"})


def test_the_folder_watched_and_the_folder_matched_are_the_same_one(tmp_path):
    """The two sides derive the path separately; if they ever disagree the rule is silently dead."""
    import os
    t = rules({"when": {"event": "file.changed", "path": "~/Downloads/*.pdf"}, "do": "x"})[0]
    watched = subscription(Config.load(), [t])["paths"][0]
    assert os.path.isabs(watched), f"the helper is asked to watch {watched!r}, which is not a path"
    assert t.matches({"kind": "file.changed", "path": watched + "/a.pdf"})


def test_a_pattern_that_is_not_a_regex_does_not_take_the_batch_down(tmp_path):
    """`matches` tries the pattern as a regex after fnmatch; a glob like `a[b` is not one, and an
    exception there loses every event in that poll, not just the one rule."""
    eng = Engine([{"seq": 1, "kind": "file.changed", "path": "/tmp/a[b.pdf"}])
    w = Watcher(eng, cfg_with(tmp_path, {"triggers": [{"when": {"event": "file.changed", "path": "/tmp/a[b*"},
                                                       "do": "x"}]}))
    w.start()
    w.seq = 0
    assert w.tick()
