"""A prompt above every window, left by an earlier task, is seen, said, and stepped back from.

An app one suite launched made the system put up "Allow X to find devices on local networks?". It belongs to
no app the sweep quits; it stayed through the whole next suite, and every task there pressed Escape at it and
ended blocked. The harness now counts such windows, reports them before a task that starts under one, and
presses Escape at them in the sweep — never Allow, which is the person's decision.
"""

from macwork import evals


class Helper:
    def __init__(self, windows):
        self.windows, self.calls = windows, []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "screen.windows":
            return self.windows
        if method == "apps.running":
            return [{"pid": 1, "name": "TextEdit", "bundle_id": "com.apple.TextEdit"}]
        return {"ok": True}


class Eng:
    def __init__(self, windows):
        self.helper = Helper(windows)
        self.cfg = None
        self.tasks = {}

    def _host_bundles(self):
        return set()


DOC = {"pid": 1, "id": 10, "owner": "TextEdit", "layer": 0, "alpha": 1, "frame": [0, 0, 800, 600], "title": "Untitled"}
PROMPT = {"pid": 77, "id": 90, "owner": "UserNotificationCenter", "layer": 8, "alpha": 1, "frame": [400, 300, 420, 200],
          "title": "Allow \u201cGraphDB Desktop\u201d to find devices on local networks?"}
BANNER = {"pid": 78, "id": 91, "owner": "NotificationCenter", "layer": 24, "alpha": 1, "frame": [900, 40, 350, 90], "title": ""}
HIDDEN = {"pid": 79, "id": 92, "owner": "Spotlight", "layer": 23, "alpha": 0, "frame": [569, 253, 844, 577], "title": ""}


def test_a_dialog_above_every_window_counts_as_a_window_and_a_banner_does_not():
    wins = evals._real_windows(Eng([DOC, PROMPT, BANNER, HIDDEN]))
    assert wins == {1: {10}, 77: {90}}
    assert evals._dialogs_above(Eng([DOC, PROMPT, BANNER, HIDDEN]), {}) == evals._dialogs_above(Eng([PROMPT]), {}), \
        "a launcher's hidden panel (alpha 0) was taken for a dialog"


def test_a_prompt_that_was_not_there_before_is_a_leftover_owned_by_nobody_in_the_app_list():
    eng = Eng([DOC, PROMPT])
    left = evals._leftovers(eng, ({1: {10}}, {1}))
    assert [x for x in left if x.get("dialog")] == [{"app": "UserNotificationCenter", "pid": 77, "new_app": False, "windows": 1,
                                                     "ids": [90], "dialog": True, "title": PROMPT["title"]}]
    assert evals._dialogs_above(eng, {77: {90}}) == []          # up before the suite began: not the run's


def test_the_sweep_steps_back_from_it_with_escape_and_never_answers_it(monkeypatch):
    eng = Eng([DOC, PROMPT])
    monkeypatch.setattr(evals, "_discard_prompt", lambda *a, **k: False)
    evals.sweep(eng, ({1: {10}}, {1}))
    keys = [p for m, p in eng.helper.calls if m == "input.key"]
    assert keys == [{"combo": "escape"}]
    assert not any(m == "ax.perform" for m, p in eng.helper.calls), "the harness pressed a button on a system prompt"
