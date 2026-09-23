"""What the window server says is on screen needs no app to answer for it.

Accessibility asks the app, and an app that is launching or busy answers late or not at all; the window server
lists every window's owner, level, frame and visibility without asking it. These are the rules for reading that
list: which windows are an app's own, which ones float over it for an input method, and when two frames are one
window — a rule act and tidy each had a copy of.
"""

from macwork.helper import HelperError
from macwork.onscreen import app_windows, input_method_panel, ordinary, same_place
from tests.test_engine import FakeHelper


def test_an_ordinary_window_is_what_the_window_server_says_or_layer_zero():
    assert ordinary({"layer": 0}) and ordinary({})                    # an older helper gives the layer only
    assert not ordinary({"layer": 3}) and not ordinary({"layer": 25})
    assert ordinary({"layer": 3, "ordinary": True})                    # a newer one says so itself
    assert not ordinary({"layer": 0, "ordinary": False})


def test_an_input_methods_floating_window_is_a_panel_and_its_settings_window_is_not():
    assert input_method_panel({"input_method": True, "layer": 24})     # its candidates, over the app
    assert not input_method_panel({"input_method": True, "layer": 0})  # its settings: a window like any other
    assert not input_method_panel({"layer": 24})                        # above the app, but no input method's


SCREEN = [
    {"pid": 42, "id": 1, "layer": 0, "alpha": 1, "frame": [0, 0, 800, 600], "title": "Untitled"},
    {"pid": 7, "id": 2, "layer": 0, "alpha": 1, "frame": [0, 0, 900, 700], "title": "Downloads"},    # another app's
    {"pid": 42, "id": 3, "layer": 25, "alpha": 1, "frame": [100, 100, 300, 200]},                   # above its level
    {"pid": 42, "id": 4, "layer": 0, "alpha": 0, "frame": [0, 0, 640, 480]},                        # not drawn
    {"pid": 42, "id": 5, "layer": 0, "alpha": 1, "frame": [0, 0, 700, 500], "on_screen": False},    # another Space
    {"pid": 42, "id": 6, "layer": 0, "alpha": 1, "frame": [0, 0, 100, 30]},                         # a strip
    {"pid": 42, "id": 8, "layer": 0, "alpha": 1, "frame": [40, 40, 400, 300], "title": "Inspector"},
]


class Newer(FakeHelper):
    """Answers for the one process it is asked about, as a helper that knows the `pid` parameter does."""
    screen = SCREEN


class Older(FakeHelper):
    """Knows neither `pid` nor `all`: every window of every process, on every Space."""

    def call(self, method, timeout=30.0, **p):
        if method == "screen.windows":
            self.calls.append((method, p))
            return list(SCREEN)
        return super().call(method, timeout, **p)


def test_the_apps_windows_on_screen_are_its_ordinary_visible_ones_big_enough():
    for helper in (Newer(), Older()):
        assert app_windows(helper, 42) == [{"id": 1, "title": "Untitled", "frame": [0, 0, 800, 600], "layer": 0},
                                           {"id": 8, "title": "Inspector", "frame": [40, 40, 400, 300], "layer": 0}]
        assert helper.did("screen.windows")[-1] == {"pid": 42, "all": False}
        assert [w["id"] for w in app_windows(helper, 42, all_spaces=True)] == [1, 5, 8]      # front to back

    class Silent(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "screen.windows":
                raise HelperError("timeout", "no reply")
            return super().call(method, timeout, **p)
    assert app_windows(Silent(), 42) == []


def test_two_frames_within_three_points_are_the_same_place():
    assert same_place([0, 0, 800, 600], [3, -3, 797, 603])
    assert not same_place([0, 0, 800, 600], [4, 0, 800, 600])
    assert same_place([0, 0, 800, 600], [4, 0, 800, 600], slack=5)
    assert not same_place(None, [0, 0, 800, 600]) and not same_place([0, 0, 800, 600], [])
