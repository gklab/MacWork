"""What the loop remembers about a step is judged from that step, on what was measured.

Two things were remembered falsely. "Led back" was judged again on every look, against every screen ever seen: a
second look at the screen a step had just led to — after a look into a group, a sub-goal ticked off, a plan —
found that screen already seen and flagged the step as a circle. On 09-22..23, 79 of the 109 circle entries were
added on looks with no step in between. And a progress judgement below `progress_bad` was stored as "no effect"
under the screen's structure, which does not change with what a window holds: in aff49b2812ff a calculator's 「=」
turned 「12+30×4」 into 132, was judged 0.19, and was withheld on the display 「1」 two steps later. "No effect" is
now what the screen measured (a broken promise), on the exact screen it was measured on.
"""

from macwork.engine import Engine
from macwork.loop import exact_state
from macwork.model import Step
from tests.english_mac import EnglishMac
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


class Opens(EnglishMac):
    """Every press opens a window never seen before: the screen a step leads to is new. (Its title differs by a
    letter: a screen's signature leaves digits out, so 「Untitled 2」 and 「Untitled 3」 would be one screen.)"""

    def __init__(self):
        super().__init__()
        self.n = 0

    def call(self, method, timeout=30.0, **p):
        if method == "ax.perform":
            self.n += 1
        if method == "ax.fingerprint":
            self.calls.append((method, p))
            return {"fingerprint": self.n}
        if method == "ax.snapshot" and p.get("scope") != "menubar":
            window = super().call(method, timeout, **p)
            window["nodes"][0]["title"] = "Untitled" + (f" {'ABCDEFGHIJ'[self.n]}" if self.n else "")
            return window
        return super().call(method, timeout, **p)


class Calculator(FakeHelper):
    """A display and five keys. 「=」 with nothing to work out changes nothing at all: no event, no text."""
    KEYS = {"k1": "1", "k2": "2", "kx": "×", "k3": "3", "ke": "="}

    def __init__(self, display="12"):
        super().__init__()
        self.display, self.moved = display, False

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot":
            self.calls.append((method, p))
            if p.get("scope") == "menubar":
                return {"nodes": [], "ms": 1}
            nodes = [{"ref": "c0", "role": "AXWindow", "title": "Calculator", "depth": 0},
                     {"ref": "cd", "role": "AXStaticText", "value": self.display, "parent": "c0"}]
            nodes += [{"ref": ref, "role": "AXButton", "title": key, "actions": ["AXPress"], "parent": "c0"}
                      for ref, key in self.KEYS.items()]
            return {"nodes": nodes, "ms": 1}
        if method == "ax.perform":
            self.calls.append((method, p))
            before, key = self.display, self.KEYS.get(p.get("ref"))
            if key == "=":
                if "×" in self.display and not self.display.endswith("×"):
                    a, b = self.display.split("×")[-2:]
                    self.display = str(int(a) * int(b))
            elif key:
                self.display += key
            self.moved = self.display != before
            return {"ok": True}
        if method == "ax.wait":
            self.calls.append((method, p))
            moved, self.moved = self.moved, False
            return {"events": [{"name": "AXValueChanged", "ms": 5}] if moved else [], "timed_out": not moved}
        return super().call(method, timeout, **p)


def calculator(tmp_path, helper, script):
    return Engine(cfg(tmp_path, config={"observe": {"providers": ["window"]}}), helper=helper, decider=ScriptedDecider(script))


def offered(questions, text):
    return any(text in v for v in questions["action"]["criteria"].values())


# --------------------------------------------------------------------------- led back

def test_a_second_look_at_the_screen_a_step_led_to_is_not_a_circle(tmp_path):
    d = ScriptedDecider([{"pick": "menu File ▸ New"}, {"pick": "look into"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"engine": {"max_options": 12}}), helper=Opens(), decider=d)
    res = eng.do("make a new document")
    assert "went_in_circles" not in d.seen[2][0], d.seen[2][0].get("went_in_circles")
    assert not eng.tasks[res["task_id"]].steps[0].led_back


def test_a_step_that_did_lead_back_is_still_a_circle(tmp_path):
    class Dialog(EnglishMac):
        """New opens a dialog; Escape closes it and the window is as it was."""
        def __init__(self):
            super().__init__()
            self.open = False

        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform":
                self.open = True
            if method == "input.key" and p.get("combo") == "escape":
                self.open = False
            if method == "ax.fingerprint":
                self.calls.append((method, p))
                return {"fingerprint": int(self.open)}
            if method == "ax.snapshot" and p.get("scope") != "menubar":
                window = super().call(method, timeout, **p)
                window["nodes"][0]["title"] = "New Document" if self.open else "Untitled"
                return window
            return super().call(method, timeout, **p)

    d = ScriptedDecider([{"pick": "menu File ▸ New"}, {"pick": "press the escape key"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=Dialog(), decider=d)
    eng.do("make a new document")
    assert d.seen[2][0].get("went_in_circles") == ["press the escape key"]


def test_second_looks_do_not_make_a_task_look_stuck(tmp_path):
    """Each step opens a window never seen before and is judged progress; after each, the decider waits and looks
    again before it chooses the next one."""
    d = ScriptedDecider([{"pick": "Refresh"}, {"pick": "Refresh", "move": "wait"}] * 4 + [{"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"engine": {"wait_s": 0}}), helper=Opens(), decider=d)
    res = eng.do("refresh the list four times")
    steps = eng.tasks[res["task_id"]].steps
    assert [s.action for s in steps] == ["button 「Refresh」"] * 4, "four steps, each looked at twice"
    assert not any("no_progress" in state for state, _ in d.seen), "a task getting somewhere was called stuck"
    assert not any(s.led_back for s in steps)


# --------------------------------------------------------------------------- no effect

def test_what_did_nothing_on_one_display_is_offered_on_another(tmp_path):
    seen = {}

    def later(state, questions):
        seen["offered"] = offered(questions, "「=」")
        return {"pick": "「=」"} if seen["offered"] else {"pick": "done"}

    helper = Calculator("12")
    eng = calculator(tmp_path, helper, [{"pick": "「=」"}, {"pick": "「×」"}, {"pick": "「3」"}, later, {"pick": "done"}])
    eng.do("work out 12×3")
    assert not offered(eng.decider.seen[1][1], "「=」"), "on the display where it did nothing, it is not offered"
    assert any("「=」" in x for x in eng.decider.seen[1][0]["tried_here_without_effect"])
    assert seen["offered"] and helper.display == "36", "the display changed: 「=」 is an action again"


def test_a_step_judged_no_progress_is_not_a_step_that_did_nothing(tmp_path):
    seen = {}

    def later(state, questions):
        seen["offered"] = offered(questions, "「=」")
        return {"pick": "done"}

    helper = Calculator("12×3")
    eng = calculator(tmp_path, helper, [{"pick": "「=」"}, {"pick": "「×」", "progress": 0.1}, {"pick": "「2」"}, later])
    eng.do("work out 12×3×2")
    assert eng.tasks[next(iter(eng.tasks))].steps[0].decision["progress_after"] == 0.1 and helper.display == "36×2"
    assert seen["offered"], "one low progress judgement took 「=」 away from every display"

    class Toggles(EnglishMac):
        """Refresh swaps what the window says between two lines: every press visibly changes it."""
        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform":
                self.text = "4 items" if self.text == "3 items" else "3 items"
            return super().call(method, timeout, **p)

    seen.clear()

    def back(state, questions):
        seen["offered"] = offered(questions, "button 「Refresh」")
        return {"pick": "done"}

    eng = Engine(cfg(tmp_path), helper=Toggles(), decider=ScriptedDecider([{"pick": "Refresh"}, {"pick": "Refresh", "progress": 0.1}, back]))
    eng.do("refresh the list")
    assert seen["offered"], "back on the very screen it was taken from, a step judged no progress is still an action"


def test_what_did_nothing_is_kept_under_the_steps_own_handle_after_a_redo(tmp_path):
    """A decision the screen moved under is made again, and the step before it is learned from a second time, from
    the record a redo rebuilds: it names the step's label, not its handle. An action remembered by where it is
    (axpath|…) was then kept under a name nothing looks it up by, and offered again on the screen it did nothing on."""
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    task = eng._new_task("refresh it", {}, None)
    ctx = eng._step_context(task)
    look = eng._look(task, ctx)
    here, handle = exact_state(look.sig, look.obs.screen_text), "axpath|AXWindow[0]/AXButton[1]"
    task.steps.append(Step(0, "button 「Reload」", key=handle, before=here, promise="changed", kept=False))
    task.prev = {"sig": look.sig, "label": "button 「Reload」", "ok": True, "events": [], "app": ctx.app, "screen": None}
    eng._learn_from_prev(task, ctx.app, look.sig, look.obs)
    assert task.memory.withheld_reason(look.sig, here, handle, "", 2) == "no_effect"


def test_a_suggestion_that_did_nothing_on_one_screen_is_offered_on_another(tmp_path):
    """The planner's tries were filtered against everything that had done nothing, on any screen: a key that did
    nothing in one window was dropped from every route that suggested it again."""
    class Quiet(EnglishMac):
        """A key press changes nothing at all; Refresh opens another window."""
        def __init__(self):
            super().__init__()
            self.title, self.moved = "Untitled", False

        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform":
                self.title, self.moved = "Results", True
            if method == "ax.wait":
                self.calls.append((method, p))
                moved, self.moved = self.moved, False
                return {"events": [{"name": "AXWindowCreated", "ms": 5}] if moved else [], "timed_out": not moved}
            if method == "ax.snapshot" and p.get("scope") != "menubar":
                window = super().call(method, timeout, **p)
                window["nodes"][0]["title"] = self.title
                return window
            return super().call(method, timeout, **p)

    seen = {}

    def last(state, questions):
        seen["offered"] = offered(questions, "press cmd+j (suggested by the planner)")
        return {"pick": "done"}

    d = ScriptedDecider([{"pick": "press cmd+j"}, {"pick": "Refresh"}, {"pick": "Refresh", "move": "rethink"}, last])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}), helper=Quiet(), decider=d)
    eng._planner = FakeBackend([{"steps": ["try the shortcut"], "inputs": {}, "try": [{"keys": "cmd+j"}]},
                                {"steps": ["try the shortcut from the results"], "inputs": {}, "try": [{"keys": "cmd+j"}]}])
    res = eng.do("show the results")
    steps = eng.tasks[res["task_id"]].steps
    assert steps[0].kept is False and not offered(d.seen[1][1], "press cmd+j"), "it did nothing, and is withheld there"
    assert seen["offered"], "suggested again for another window, and dropped because it once did nothing elsewhere"


# --------------------------------------------------------------------------- what the decider is told

def test_the_decider_is_told_why_each_action_is_left_out(tmp_path):
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    task = eng._new_task("write a note", {}, None)
    first = eng._look(task, eng._step_context(task))
    here = exact_state(first.sig, first.obs.screen_text)
    by = lambda text: next(a for a in first.affs if text in a.label)   # noqa: E731
    new, delete, export, recent = by("File ▸ New"), by("Delete Document"), by("Export"), by("Recent Items")
    field = next(a for a in first.affs if a.verb == "type")
    assert field.handle().startswith("axpath|"), "a field with no title of its own is remembered by where it is"
    task.memory.note_no_effect(here, new.handle())
    task.memory.note_no_progress(first.sig, delete.handle())
    task.memory.note_failed(first.sig, export.handle(), here)
    task.memory.note_withdrawn(here, recent.handle())
    task.memory.note_withdrawn(here, recent.handle())
    task.memory.note_withdrawn(here, field.handle())                      # once: told, still offered
    state = eng._look(task, eng._step_context(task)).state
    assert state["tried_here_without_effect"] == [new.label]
    assert state["got_nowhere_from_here"] == [delete.label]
    assert state["could_not_be_done_here"] == [export.label]
    assert state["taken_back_from_here"] == [recent.label]
    told = state["chosen_from_this_exact_screen_before_then_came_back"]
    assert field.label in told and not any(x.startswith(("axpath|", "ax|")) for x in told), "by name, not by handle"


# --------------------------------------------------------------------------- what progress is asked against

def test_progress_is_asked_against_what_the_step_was_taken_to_reach(tmp_path):
    """Once a sub-goal is ticked off, the next one's evidence, asked about the step that finished the last one,
    reads as no progress."""
    plan = {"steps": [{"goal": "make a document", "evidence": "a window titled Untitled"},
                      {"goal": "name it", "evidence": "a window titled Notes"}], "inputs": {}}
    d = ScriptedDecider([{"pick": "menu File ▸ New"}, {"pick": "Refresh", "step_done": 0.95, "step_evidence": 0.9},
                         {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"planner": {"when": "always", "second_opinion_on_done": False}}),
                 helper=EnglishMac(), decider=d)
    eng._planner = FakeBackend([plan])
    res = eng.do("make a new document called Notes")
    assert res["plan"]["at"] == 1, "the first sub-goal was ticked off after the first step"
    asked = [q["progress"]["instructions"] for _, q in d.seen if "progress" in q]
    assert len(asked) >= 2 and all('"a window titled Untitled"' in x for x in asked[:2]), asked
