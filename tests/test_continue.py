"""Two ways a task was told it had got nowhere when it had.

  * `_can_continue` required the last step to have produced Accessibility events, and reading a file,
    reading a window in full, running a Shortcut or taking the clipboard produce none — those channels
    return `wait=False` on purpose. So a long task that ended its turn on a *read* was scored `failed`
    instead of handed back to be continued. The comment on the line below it reads "whether the task is
    getting somewhere is the decider's judgement, not a count of UI events".
  * `read_all` walks the focused window's Accessibility tree. For a window whose tree holds only its
    chrome — a browser, a canvas — that is the toolbar and the address bar, and the loop recorded it as
    "read in full" and made it a fact the task may then write back as the document's contents.
"""

from typing import Any

import pytest

from macwork.loop import Spent
from macwork.model import Affordance, Step, Task
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def engine(tmp_path):
    from macwork.engine import Engine
    return Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))


def spent_run(task: Task, step: Step) -> bool:
    task.steps.append(step)
    return step.ok


# --------------------------------------------------------------------------- handing a long task back

def test_a_step_that_read_something_counts_as_having_got_somewhere(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="read the long one", id="t1")
    task.steps.append(Step(0, "read all the text in this window", ok=True, events=[], produced=True))
    e = eng.cfg.section("engine")
    assert eng._can_continue(task, e, Spent("this turn's 12 steps")), \
        "a task that just read a whole document was told it had got nowhere"


def test_a_step_that_did_nothing_at_all_still_stops_it(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t2")
    task.steps.append(Step(0, "press something", ok=True, events=[], produced=False))
    assert not eng._can_continue(task, eng.cfg.section("engine"), Spent("this turn's 12 steps"))


def test_a_failed_step_stops_it(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t3")
    task.steps.append(Step(0, "press something", ok=False, events=["AXValueChanged"], produced=True))
    assert not eng._can_continue(task, eng.cfg.section("engine"), Spent("this turn's 12 steps"))


def test_a_ui_event_still_counts_as_it_always_did(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t4")
    task.steps.append(Step(0, "press something", ok=True, events=["AXValueChanged"], produced=False))
    assert eng._can_continue(task, eng.cfg.section("engine"), Spent("this turn's 12 steps"))


def test_the_whole_task_ceiling_is_still_the_end_of_it(tmp_path):
    eng = engine(tmp_path)
    task = Task(goal="x", id="t5")
    task.steps.append(Step(0, "read", ok=True, events=[], produced=True))
    assert not eng._can_continue(task, eng.cfg.section("engine"), Spent("48 steps over all its turns", whole_task=True))


def test_a_step_records_whether_it_produced_anything():
    import inspect

    from macwork import loop
    assert "produced=" in inspect.getsource(loop.LoopMixin._perform), \
        "nothing tells a later step that this one produced something"


# --------------------------------------------------------------------------- reading a window that has no text in it

class ThinTree:
    """A browser's window: the toolbar is in the tree and the page is not."""

    mode = "fake"

    def __init__(self, nodes):
        self.nodes = nodes

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot":
            return {"nodes": self.nodes}
        if method == "apps.frontmost":
            return {"app": {"pid": 1, "name": "X"}}
        raise AssertionError(method)


CHROME = [{"ref": "r1", "role": "AXButton", "title": "Back"},
          {"ref": "r2", "role": "AXButton", "title": "Reload"},
          {"ref": "r3", "role": "AXTextField", "value": "https://example.com/a-very-long-article"}]


def read_all(tmp_path, nodes):
    from macwork.act import get_channel
    from macwork.config import Config
    from macwork.observe import Ctx
    ctx = Ctx(cfg=Config.load(), helper=ThinTree(nodes), app={"pid": 1, "name": "X"}, goal="", inputs={},
              task="t", gate=None, cache={})
    return get_channel("window")(ctx, Affordance("t", "window", "read_all", "read all", {"pid": 1}), {})


def test_a_window_whose_tree_holds_only_its_chrome_says_so(tmp_path):
    out = read_all(tmp_path, CHROME)
    page = (out.output or {}).get("read_window") or {}
    assert page.get("thin"), "the toolbar came back as the document, with nothing to say it was not"


def test_a_real_document_does_not_claim_to_be_thin(tmp_path):
    nodes = [{"ref": f"r{i}", "role": "AXStaticText", "value": f"paragraph {i} " + "word " * 40} for i in range(30)]
    out = read_all(tmp_path, nodes)
    assert not ((out.output or {}).get("read_window") or {}).get("thin")


def test_what_it_found_is_still_returned_either_way(tmp_path):
    out = read_all(tmp_path, CHROME)
    assert out.ok and "Reload" in ((out.output or {}).get("read_window") or {}).get("text", "")
