"""Looking at the window, the way a person does after pressing something.

In a window that draws itself the Accessibility tree says nothing, so the engine could not tell an action
that moved the world from one that did nothing. A person can, without knowing what the picture is of: they
see it change. `screen.glance` is that look — a coarse grid of brightness, on-device — and these tests are
about what the engine may conclude from two of them.
"""

import base64

from macwork import sight
from macwork.engine import Engine
from macwork.helper import HelperError
from tests.test_controls import CONTROLS, Harmless, Steered
from tests.test_engine import cfg

N = 8


def glance(cells=None, frame=(0, 0, 800, 600)):
    grid = bytearray([100] * (N * N))
    for i, v in (cells or {}).items():
        grid[i] = v
    return {"grid": N, "cells": base64.b64encode(bytes(grid)).decode(), "frame": list(frame)}


# ----------------------------------------------------------------- the measurement
def test_the_same_picture_is_no_change():
    assert sight.compare(glance(), glance(), 2) == {"share": 0.0, "cells": 0, "where": ""}


def test_a_caret_blinking_is_not_a_change():
    assert sight.compare(glance(), glance({5: 102}), 2)["cells"] == 0
    assert sight.compare(glance(), glance({5: 103}), 2)["cells"] == 1


def test_it_says_how_much_and_roughly_where():
    corner = sight.compare(glance(), glance({0: 200, 1: 200, 8: 200}), 2)
    assert round(corner["share"], 3) == round(3 / 64, 3) and corner["where"] == "at the top left"
    assert sight.compare(glance(), glance({i: 0 for i in range(64)}), 2)["where"] == "across the window"
    assert sight.compare(glance(), glance({27: 0, 28: 0}), 2)["where"] == "in the middle"
    assert sight.compare(glance(), glance({62: 0, 63: 0}), 2)["where"] == "at the bottom right"


def test_it_says_where_on_screen_the_picture_changed():
    """In screen points, where what the step drew can be read: the changed cells' box and one cell around it,
    cut to the window. A panel in a described window was read and dropped, or not read at all, because only
    the words "at the top left" said where it was."""
    at = (40, 90, 800, 600)                  # 8×8 cells of 100×75 points
    corner = sight.compare(glance(frame=at), glance({0: 200, 1: 200, 8: 200, 9: 200}, frame=at), 2)
    assert corner.get("region") == [40, 90, 300, 225], "the box of cells 0-1 × 0-1, one cell more, cut at the edge"
    middle = sight.compare(glance(frame=at), glance({27: 0, 28: 0}, frame=at), 2)
    assert middle.get("region") == [40 + 200, 90 + 150, 400, 225]
    assert sight.compare(glance(frame=at), glance(frame=at), 2) == {"share": 0.0, "cells": 0, "where": ""}
    # …and whether that part has changed since: only the cells there are compared, part for part, though
    # the window has moved
    moved = (140, 190, 800, 600)
    assert sight.compare(glance(frame=at), glance({63: 0}, frame=moved), 2, within=[40, 90, 300, 225])["cells"] == 0
    assert sight.compare(glance(frame=at), glance({9: 0}, frame=moved), 2, within=[40, 90, 300, 225])["cells"] == 1


def test_pictures_of_different_things_are_not_compared():
    assert sight.compare(glance(), glance(frame=(0, 0, 1024, 768)), 2) is None, "the window was resized"
    assert sight.compare(glance(frame=(0, 0, 800, 600)), glance(frame=(40, 90, 800, 600)), 2) is not None, "only moved"
    assert sight.compare(None, glance(), 2) is None
    assert sight.compare(glance(), {"grid": N, "cells": "not base64!", "frame": [0, 0, 800, 600]}, 2) is None


def test_the_decider_is_told_a_fact():
    assert sight.describe(sight.compare(glance(), glance({i: 0 for i in range(24)}), 2)) == \
        "the picture in the window changed (38% of it, at the top)"
    assert "under 1%" in sight.describe({"share": 0.004, "cells": 4, "where": "in the middle"})
    assert sight.describe(None) == ""


# ----------------------------------------------------------------- in the loop
class Seeing(Steered):
    """A self-drawn window the engine can glance at. `moves`: does a hold change the picture?"""

    def __init__(self, moves=True, sighted=True):
        super().__init__()
        self.moves, self.sighted, self.frames = moves, sighted, 0

    def call(self, method, timeout=30.0, **p):
        if method == "screen.glance":
            self.calls.append((method, p))
            if not self.sighted:
                raise HelperError("no_permission", "Screen Recording is not granted")
            return glance({i: 30 for i in range(self.frames % 40, self.frames % 40 + 12)})
        if method == "input.hold" and self.moves:
            self.frames += 3
        if method == "ax.perform" and self.paints:
            self.frames += 5
        return super().call(method, timeout, **p)

    paints = False            # does pressing the window's button repaint it, without a word to Accessibility?


def run(tmp_path, script, helper):
    decider = Harmless(script)
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window", "sight", "controls"]}}), helper=helper, decider=decider)
    return decider, eng.do("walk to the door", inputs={"controls": CONTROLS})


FORWARD = "move forward — hold w for 1.2 s"


def offered(decider, n, what):
    return any(what in o for o in decider.seen[n][1]["action"]["criteria"].values())


def test_a_hold_that_moved_the_world_is_seen_to(tmp_path):
    decider, res = run(tmp_path, [{"pick": FORWARD}] * 6 + [{"pick": "done", "done": 0.95}], Seeing(moves=True))
    assert res["status"] == "done" and all(offered(decider, n, "move forward") for n in range(7))
    line = decider.seen[1][0]["history"][-1]
    assert "the picture in the window changed (" in line and "no text on screen did" in line
    assert "does not show" not in line, "it is not blind any more, and must not say it is"


def test_a_hold_that_moved_nothing_is_withdrawn_like_any_other_action(tmp_path):
    """Walking into a wall. Blind, the engine could not tell and kept offering it; now it can."""
    decider, _ = run(tmp_path, [{"pick": FORWARD}, {"pick": "done", "done": 0.95}], Seeing(moves=False))
    assert not offered(decider, 1, FORWARD)
    assert offered(decider, 1, "look left"), "only the one that did nothing"
    assert FORWARD in decider.seen[1][0]["tried_here_without_effect"]


def test_without_a_picture_it_is_as_careful_as_before(tmp_path):
    """No Screen Recording permission, an older helper: nothing is concluded from what could not be seen."""
    decider, res = run(tmp_path, [{"pick": FORWARD}] * 5 + [{"pick": "done", "done": 0.95}], Seeing(moves=False, sighted=False))
    assert res["status"] == "done" and all(offered(decider, n, "move forward") for n in range(6))
    assert "does not show in anything the engine can read" in decider.seen[1][0]["history"][-1]


def test_a_button_that_only_repaints_the_window_did_something(tmp_path):
    """No Accessibility event and no text changed — by the old evidence, "nothing happened"."""
    helper = Seeing()
    helper.paints = True
    decider, _ = run(tmp_path, [{"pick": "Refresh"}, {"pick": "done", "done": 0.95}], helper)
    assert offered(decider, 1, "Refresh")
    assert "the picture in the window changed" in decider.seen[1][0]["history"][-1]


def test_and_one_that_repaints_nothing_still_did_not(tmp_path):
    decider, _ = run(tmp_path, [{"pick": "Refresh"}, {"pick": "done", "done": 0.95}], Seeing())
    assert not offered(decider, 1, "Refresh")
