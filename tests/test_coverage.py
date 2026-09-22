"""How much of a window the tree says nothing about is measured over the window, not guessed from its chrome.

A spreadsheet in a Qt app reported 35 actionable nodes in its toolbar, two 16-pixel unlabeled nodes, and
nothing at all where the cells were: not a scroll area, not a group, nothing. Every signal the vision provider
had — few nodes, a hollow container — looked at what the tree *offered* and so saw a well-described window.
The share of the window's area that no described node covers is a fact about the window itself, and where it
is most of the window, the screen is read on the first look. When even the screen shows nothing there, that
is a fact too: the decider is told, and a task that ends on it ends with a cause, not with "no route".
"""

from macwork.engine import Engine
from macwork.model import Observation
from macwork.observe import PROVIDERS, Ctx, undescribed_share
from tests.test_engine import FakeHelper, ScriptedDecider, cfg

ACTIONS = {"AXPress", "AXConfirm"}
TEXT = {"AXTextField"}
WIN = [0, 0, 1000, 800]


def node(ref, role, frame, parent="w", **more):
    return {"ref": ref, "role": role, "frame": frame, "parent": parent, **more}


def test_the_share_counts_what_describes_and_not_what_merely_encloses():
    toolbar = [node(f"b{i}", "AXButton", [i * 50, 0, 40, 40], actions=["AXPress"], title=f"b{i}") for i in range(20)]
    window = [{"ref": "w", "role": "AXWindow", "frame": WIN}]
    assert undescribed_share(window + toolbar, WIN, ACTIONS, TEXT) > 0.9           # a strip of buttons at the top
    hollow = node("g", "AXGroup", [0, 40, 1000, 760])                              # an empty container over the rest
    assert undescribed_share(window + toolbar + [hollow], WIN, ACTIONS, TEXT) > 0.9, "an empty container describes nothing"
    labelled_leaf = node("t", "AXStaticText", [0, 40, 1000, 760], value="a page of text")
    assert undescribed_share(window + toolbar + [labelled_leaf], WIN, ACTIONS, TEXT) < 0.1
    assert undescribed_share(window, None, ACTIONS, TEXT) == 0.0


class Sheet(FakeHelper):
    """A window whose grid is in the tree as nothing: a toolbar, a name box, a formula bar, and 80% of blank."""

    def __init__(self, boxes):
        super().__init__()
        self.boxes, self.read = boxes, []

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "focused_window":
            nodes = [{"ref": "w", "role": "AXWindow", "title": "sales.csv", "frame": WIN}]
            nodes += [node(f"b{i}", "AXButton", [i * 40, 0, 36, 36], rdesc="button", title=f"tool {i}", actions=["AXPress"]) for i in range(30)]
            nodes += [node("name", "AXTextField", [0, 40, 80, 24], rdesc="text field", value="A1", title="Name Box", actions=["AXConfirm"]),
                      node("bar", "AXTextField", [90, 40, 900, 24], rdesc="text field", value="Item", title="Formula Bar", actions=["AXConfirm"])]
            return {"nodes": nodes, "ms": 3}
        if method == "screen.ocr":
            self.read.append(p)
            return {"boxes": self.boxes, "frame": WIN, "ms": 20, "direction": "ltr"}
        if method == "ax.fingerprint":
            return {"fingerprint": "grid"}
        return super().call(method, timeout, **p)


def look(tmp_path, helper):
    c = cfg(tmp_path)
    ctx = Ctx(c, helper, app={"pid": 42, "name": "Sheet"}, running=[])
    obs = Observation(app=ctx.app, window=None, affordances=[])
    obs.notes["open_windows"] = ["sales.csv"]
    PROVIDERS["window"](ctx, obs)
    PROVIDERS["vision"](ctx, obs)
    return obs


def test_a_grid_the_tree_holds_as_nothing_is_read_on_the_first_look(tmp_path):
    cells = [{"text": t, "frame": [x, y, 60, 18], "conf": 0.9} for t, x, y in
             [("Item", 20, 100), ("Amount", 300, 100), ("Monitor", 20, 130), ("1790", 300, 130),
              ("Keyboard", 20, 160), ("399", 300, 160), ("Total", 20, 190)]]
    h = Sheet(cells)
    obs = look(tmp_path, h)
    assert obs.notes["undescribed_share"] >= 0.35 and h.read, "the window was left unread"
    labels = [a.label for a in obs.affordances]
    assert any(a.channel == "pointer" and "1790" in a.label for a in obs.affordances), labels
    assert any("empty place" in x and "Total" in x for x in labels), "the cell where the total goes is not a place"
    assert "1790" in obs.screen_text and "Name Box" in " ".join(labels) or "A1" in obs.screen_text
    assert "window_unreadable" not in obs.notes


def test_a_window_that_shows_nothing_readable_says_so(tmp_path):
    h = Sheet([])
    obs = look(tmp_path, h)
    assert h.read and obs.notes["window_unreadable"] >= 0.35
    assert "shows nothing that can be read" in obs.screen_text


def test_a_task_that_ends_on_such_a_window_ends_with_that_cause(tmp_path):
    from macwork.model import Task
    eng = Engine(cfg(tmp_path), helper=Sheet([]), decider=ScriptedDecider([]))
    task = Task(goal="fill in the total")
    ctx = eng._ctx("fill in the total", {}, {"pid": 42, "name": "Sheet"}, "t")
    ctx.gate = eng.gate
    obs = look(tmp_path, Sheet([]))
    res = eng._diagnose(task, ctx, obs, {"screen_text": obs.screen_text}, reason="no route to the goal was found", cause="no_route")
    assert res["status"] == "failed" and res["cause"] == "window_unreadable" and "could not be read" in res["reason"]
    assert "invariants_broken" not in res["outputs"]
