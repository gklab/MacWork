"""An action that is complete is not an option, and one that returns something says what it returns.

Both come from one real failure. Asked what a 17-line file ended with, with the file open and its first 9
lines on screen, a run was offered eight ways to open the file again and a bare "read the text of …"; it put
0.03 on reading, scrolled instead, and ran out of steps. The same recorded request with the read labelled by
what it returns — all 17 lines, and the window showing only part — put 0.72 on it, 5 times out of 5.
Neither fix knows anything about that file, that app or that task: what is open is what the app's window
reports, and what a file holds is measured.
"""

import os

from macwork.engine import Engine
from macwork.model import Observation
from macwork.observe import PROVIDERS, Ctx
from macwork.store import dump, load
from tests.english_mac import WINDOW, EnglishMac as FakeHelper
from tests.test_engine import ScriptedDecider, cfg

TEXTEDIT = {"pid": 42, "name": "TextEdit", "bundle_id": "com.apple.TextEdit"}


def long_file(tmp_path, lines=17):
    path = tmp_path / "long.txt"
    path.write_text("".join(f"line {i} of the file\n" for i in range(1, lines + 1)), encoding="utf-8")
    return path


def offered(tmp_path, path, *, document=None, screen="", helper=None):
    obs = Observation(app=None, window=None, affordances=[])
    obs.screen_text = screen
    if document:
        obs.notes["window_document"] = str(document)
    ctx = Ctx(cfg(tmp_path), helper or FakeHelper(), goal=f"what does {path} end with?", running=[], app=TEXTEDIT)
    PROVIDERS["files"](ctx, obs)
    return obs.affordances


class Openers(FakeHelper):
    openers = [{"name": "TextEdit", "path": "/System/Applications/TextEdit.app", "bundle_id": "com.apple.TextEdit"},
               {"name": "Xcode", "path": "/Applications/Xcode.app", "bundle_id": "com.apple.dt.Xcode"}]


# ----------------------------------------------------------------- what a read returns
def test_a_read_says_how_much_it_returns(tmp_path):
    path = long_file(tmp_path)
    read = next(a for a in offered(tmp_path, path) if a.verb == "read")
    assert "all 17 lines of it at once" in read.label
    assert "shows only part" not in read.label, "nothing is open: there is no window to compare with"


def test_it_says_so_when_the_window_does_not_reach_the_end(tmp_path):
    path = long_file(tmp_path)
    top = "line 1 of the file\nline 2 of the file"
    read = next(a for a in offered(tmp_path, path, document=path, screen=top) if a.verb == "read")
    assert "the window in front shows only part of it" in read.label


def test_it_does_not_say_so_when_the_end_is_on_screen(tmp_path):
    path = long_file(tmp_path, lines=3)
    read = next(a for a in offered(tmp_path, path, document=path, screen=path.read_text()) if a.verb == "read")
    assert "shows only part" not in read.label


def test_a_file_that_is_not_text_is_not_described_by_lines(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.7\x00\xff\xfe binary")
    read = next(a for a in offered(tmp_path, path) if a.verb == "read")
    assert "returns everything it says at once" in read.label and "lines" not in read.label


def test_a_file_is_measured_again_only_when_it_has_changed(tmp_path):
    path = long_file(tmp_path)
    ctx = Ctx(cfg(tmp_path), FakeHelper(), goal=f"read {path}", running=[])
    labels = []
    for grow in (False, False, True):
        if grow:
            path.write_text(path.read_text() + "one more\n", encoding="utf-8")
            os.utime(path, ns=(1, 1))          # a different modified time even on a coarse clock
        obs = Observation(app=None, window=None, affordances=[])
        PROVIDERS["files"](ctx, obs)
        labels.append(next(a for a in obs.affordances if a.verb == "read"))
    assert "all 17 lines" in labels[1].label and "all 18 lines" in labels[2].label
    assert labels[0].yields == labels[1].yields != labels[2].yields


# ----------------------------------------------------------------- what is already done
def test_a_file_already_open_in_front_is_not_offered_for_opening(tmp_path):
    path = long_file(tmp_path)
    closed = [a.label for a in offered(tmp_path, path, helper=Openers()) if a.verb == "open"]
    opened = [a.label for a in offered(tmp_path, path, document=path, helper=Openers()) if a.verb == "open"]
    assert len(closed) == 3
    assert opened == ["open long.txt with Xcode"], "another app is a different act; this one is done"


def test_it_is_the_file_not_the_name_that_counts(tmp_path):
    path = long_file(tmp_path)
    other = tmp_path / "elsewhere" / "long.txt"
    other.parent.mkdir()
    other.write_text("a different file with the same name", encoding="utf-8")
    assert any(a.verb == "open" and "whichever app" in a.label for a in offered(tmp_path, path, document=other))


def test_the_window_provider_reports_the_document(tmp_path):
    class Showing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") != "menubar":
                self.calls.append((method, p))
                nodes = [dict(WINDOW["nodes"][0], document="/tmp/long.txt"), *WINDOW["nodes"][1:]]
                return {**WINDOW, "nodes": nodes}
            return super().call(method, timeout, **p)

    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["window"](Ctx(cfg(tmp_path), Showing(), app=TEXTEDIT, running=[]), obs)
    assert obs.notes["window_document"] == "/tmp/long.txt"


# ----------------------------------------------------------------- in the loop
class Reads(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "file.read_text":
            self.calls.append((method, p))
            return {"kind": "text", "text": open(p["path"], encoding="utf-8").read()}
        return super().call(method, timeout, **p)


class Harmless(ScriptedDecider):
    what = {"navigate": 1.0}           # these tests are not about the floor


def run(tmp_path, script, goal):
    decider = Harmless(script)
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["files", "window"]}}), helper=Reads(), decider=decider)
    return eng, decider, eng.do(goal)


def test_what_has_been_read_is_not_offered_for_reading_again(tmp_path):
    path = long_file(tmp_path)
    _, decider, res = run(tmp_path, [{"pick": "read the text of"}, {"pick": "done", "done": 0.95}], f"what does {path} end with?")
    first, second = (list(q["action"]["criteria"].values()) for _, q in decider.seen[:2])
    assert any("read the text of" in o for o in first)
    assert not any("read the text of" in o for o in second), "it holds the whole file already"
    assert res["status"] == "done"


def test_a_file_that_changed_since_can_be_read_again(tmp_path):
    path = long_file(tmp_path)

    def rewrite(state, questions):
        path.write_text("rewritten\nentirely\n", encoding="utf-8")
        os.utime(path, ns=(1, 1))
        return {"pick": "Refresh"}     # anything: the point is the look that follows

    _, decider, _ = run(tmp_path, [{"pick": "read the text of"}, rewrite, {"pick": "done", "done": 0.95}],
                        f"what does {path} end with?")
    third = list(decider.seen[2][1]["action"]["criteria"].values())
    assert any("read the text of" in o and "all 2 lines" in o for o in third)


def test_what_a_task_holds_survives_being_stored(tmp_path):
    path = long_file(tmp_path)
    eng, _, res = run(tmp_path, [{"pick": "read the text of"}, {"pick": "done", "done": 0.95}], f"read {path}")
    task = eng.store.get(res["task_id"])
    assert task.memory.yielded and isinstance(task.memory.yielded, set)
    again = load(dump(task))
    assert again.memory.yielded == task.memory.yielded and isinstance(again.memory.yielded, set)
