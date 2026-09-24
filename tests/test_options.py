"""What the decider is offered when there is more than one choice can hold: the screen in front first, the
planner's suggestions each on its own, a group the decider opened a page at a time, and nothing cut.

Found in this Mac's audit log. A task that worked out 17×23 in Calculator looked into the list of every
installed app (535 of them) while Calculator was not answering. The list stayed open for the rest of the
task and went first, and the budget cut the tail: on each of the next nine looks Calculator's window was up
with 59 controls, 102 to 195 apps stood in front of them, and not one of them was an option ("403 more
actions did not fit and are not listed"). The task ended asking to confirm a Return it had chosen from the
keys. In an earlier run of the same task, the planner's own options (type 17, type *, type 23 among them)
were cut on every look that had one.
"""

import math
import re

from macwork.engine import Engine
from macwork.model import Affordance
from macwork.observe import arrange
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend


def calculator(apps: int = 535):
    """The shape of that look: a keypad, two menus, the keys, every installed app, one suggestion to type."""
    keypad = [Affordance(f"w{i}", "window", "press", f"button 「{i}」", context="Keypad") for i in range(30)]
    menus = [Affordance(f"m{i}", "menu", "press", f"menu {'View' if i < 12 else 'Edit'} ▸ item {i}",
                        context="View" if i < 12 else "Edit") for i in range(24)]
    keys = [Affordance(f"k{i}", "keys", "key", f"press the key {i}") for i in range(13)]
    installed = [Affordance(f"i{i}", "app", "open", f"open app A{i}") for i in range(apps)]
    typed = Affordance("t0", "keys", "type", "type 「17」 at the cursor (suggested by the planner)", {"text": "17"})
    return keypad, menus, keys, installed, typed


def reachable(flat, folded):
    return {a.id for a in flat} | {m.id for _label, members in folded.values() for m in members}


def looked_into(affs, folded, group, pinned=frozenset()):
    """What `loop._open_group` keeps when the decider picks the entry for `group`: where its next page begins."""
    from macwork.observe import page_anchor, page_members
    return {group: page_anchor(page_members(affs, group, pinned), folded[group][1][0])}


def test_an_opened_group_bigger_than_the_budget_never_pushes_the_screen_out():
    keypad, menus, keys, installed, typed = calculator()
    affs = keypad + menus + keys + installed + [typed]
    flat, folded = arrange(affs, 199, {"app:open": None}, fold_over=60, pinned={"t0"})
    offered = {a.id for a in flat}
    assert offered >= {a.id for a in keypad}, "the screen in front was cut for the list the decider opened"
    assert "t0" in offered, "the planner's suggestion was cut"
    assert folded["app:open"][0].startswith("look into the rest of open another app")
    assert len(flat) + len(folded) <= 199
    assert reachable(flat, folded) == {a.id for a in affs}, "an action was dropped without a word"


def test_looking_further_reaches_every_app_a_page_at_a_time():
    keypad, menus, keys, installed, _typed = calculator()
    affs = keypad + menus + keys + installed
    seen, opened = set(), {"app:open": None}
    for _page in range(12):
        flat, folded = arrange(affs, 199, opened, fold_over=60)
        assert {a.id for a in keypad} <= {a.id for a in flat}, "a page of apps took the keypad's place"
        seen |= {a.id for a in flat if a.channel == "app"}
        if seen >= {a.id for a in installed}:
            break
        opened = looked_into(affs, folded, "app:open")          # what "look into the rest of" opens next
    assert seen == {a.id for a in installed}


def test_looking_further_reaches_every_control_when_labels_repeat():
    """Content has no identity, so its handle is its label, and labels repeat. A page anchored on the handle
    alone began again at the first member with that handle: page 1 came back, and the rest was never shown."""
    screen = [Affordance(f"w{i}", "window", "press", "button 「Archive」" if i % 11 == 0 else f"link 「message {i}」")
              for i in range(320)]
    shortcuts = [Affordance(f"s{i}", "shortcut", "run", "run shortcut 「Copy」" if i % 7 == 0 else f"run shortcut 「S{i}」")
                 for i in range(150)]
    for affs, group, budget, opened in ((screen, "screen", 199, {}), (shortcuts, "ch:shortcut", 50, {"ch:shortcut": None})):
        flat, folded = arrange(affs, budget, opened, fold_over=60)
        pages, seen = math.ceil(len(affs) / len(flat)), {a.id for a in flat}
        for _page in range(pages - 1):
            flat, folded = arrange(affs, budget, looked_into(affs, folded, group), fold_over=60)
            seen |= {a.id for a in flat}
        assert seen == {a.id for a in affs}, f"{group}: {len(affs) - len(seen)} never shown in {pages} pages"


def test_an_opened_list_on_a_big_screen_still_gets_a_page():
    """Room for the screen first, but not all of it: an opened list is owed a page, or looking into it would show
    nothing on a screen as big as the budget."""
    _keypad, menus, keys, installed, _typed = calculator()
    screen = [Affordance(f"w{i}", "window", "press", f"link 「item {i}」", context="Content") for i in range(250)]
    flat, folded = arrange(screen + menus + keys + installed, 199, {"app:open": None}, fold_over=60, min_page=60)
    assert sum(a.channel == "app" for a in flat) >= 60, "the list the decider opened showed no page of itself"
    assert flat[:100] == screen[:100], "the screen's first part does not come first"
    assert folded["screen"][0].startswith("look into the rest of the window")
    assert folded["app:open"][0].startswith("look into the rest of open another app")
    assert len(flat) + len(folded) <= 199


def test_a_planner_suggestion_is_offered_on_its_own_not_with_its_whole_group():
    keypad, menus, keys, installed, _typed = calculator()
    wanted = installed[400]
    flat, folded = arrange(keypad + menus + keys + installed, 199, {}, fold_over=60, pinned={wanted.id})
    assert wanted in flat and sum(a.channel == "app" for a in flat) == 1
    assert folded["app:open"][0].startswith("look into open another app (534 options")


def test_the_planner_pins_what_it_suggested_and_nothing_else(tmp_path):
    c = cfg(tmp_path, config={"engine": {"max_options": 8}})
    d = ScriptedDecider([{"pick": "Send", "move": "rethink"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(c, helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["x"], "inputs": {}, "try": [{"action": "menu File ▸ New Document (⌘N)"}]}])
    assert eng.do("make a new document")["status"] == "done"
    offered = list(d.seen[1][1]["action"]["criteria"].values())
    assert "menu File ▸ New Document (⌘N) — suggested by the planner" in offered
    assert not any(v.startswith("menu File ▸ Delete Document") for v in offered), "the whole menu came with it"


def test_the_loop_says_when_more_places_than_options_leave_an_action_out(tmp_path):
    """Every place keeps an entry of its own, so only more places than there are options can leave an action
    out, and the loop checks every look for that (`invariants.check_options`). Here the places alone fill the
    budget, and the planner's suggestion, offered on its own, is the action left out: the run says so."""
    c = cfg(tmp_path, config={"engine": {"max_options": 4}, "planner": {"when": "always"}})
    eng = Engine(c, helper=FakeHelper(), decider=ScriptedDecider([{"pick": "done"}]))
    eng._planner = FakeBackend([{"steps": ["write it"], "inputs": {}, "try": [{"type": "hello"}]}])
    res = eng.do("write hello")
    said = res["outputs"].get("invariants_broken") or []
    assert any(x.startswith("1 action(s) neither offered nor inside a group") and "type 「hello」" in x for x in said), \
        f"an action left out of the options went unsaid: {said}"


def test_an_opened_group_closes_once_a_step_is_taken(tmp_path):
    c = cfg(tmp_path, config={"engine": {"max_options": 30, "fold_groups_over": 10}})
    d = ScriptedDecider([{"pick": "look into keys"}, {"pick": "New Document"}, {"pick": "done"}])
    assert Engine(c, helper=FakeHelper(), decider=d).do("make a new document")["status"] == "done"
    opened, after = (list(d.seen[i][1]["action"]["criteria"].values()) for i in (1, 2))
    assert "press the return key" in opened, "the group was not opened"
    assert not any(v.startswith("press the") for v in after), "still open after a step was taken"
    assert any(v.startswith("look into keys to press") for v in after)


def test_an_opened_group_closes_when_the_window_in_front_changes(tmp_path):
    class Retitled(FakeHelper):
        title = "Untitled"

        def call(self, method, timeout=30.0, **p):
            got = super().call(method, timeout, **p)
            if method == "ax.snapshot" and p.get("scope") == "focused_window" and got.get("nodes"):
                return {**got, "nodes": [{**got["nodes"][0], "title": self.title}] + got["nodes"][1:]}
            return got

    h = Retitled()

    def open_keys_then_the_window_changes(state, questions):
        h.title = "Another"                    # a different window comes to the front; the task did nothing
        return {"pick": "look into keys"}
    c = cfg(tmp_path, config={"engine": {"max_options": 30, "fold_groups_over": 10}})
    d = ScriptedDecider([open_keys_then_the_window_changes, {"pick": "done"}])
    Engine(c, helper=h, decider=d).do("make a new document")
    after = list(d.seen[1][1]["action"]["criteria"].values())
    assert d.seen[1][0]["window"] == "Another"
    assert not any(v.startswith("press the") for v in after), "opened on one window, still open on another"


class Calculator(FakeHelper):
    """Calculator in front with a keypad of 30 buttons, and 535 apps installed. Until `answering`, the app does
    not answer Accessibility: no window can be read."""
    answering = True

    def call(self, method, timeout=30.0, **p):
        app = {"pid": 42, "name": "Calculator", "bundle_id": "com.apple.calculator", "path": "/System/Applications/Calculator.app"}
        if method == "apps.installed":
            self.calls.append((method, p))
            return [{"name": f"App {i}", "file": f"App {i}", "path": f"/Applications/App {i}.app", "bundle_id": f"test.app{i}"}
                    for i in range(535)]
        if method == "apps.frontmost":
            return {"app": app}
        if method == "apps.running":
            return [app]
        if method == "ax.snapshot" and p.get("scope") == "focused_window":
            self.calls.append((method, p))
            if not self.answering:
                return {"nodes": [], "not_answering": True}
            return {"nodes": [{"ref": "c.0", "role": "AXWindow", "title": "Calculator", "depth": 0}] +
                    [{"ref": f"c.{i + 1}", "role": "AXButton", "rdesc": "button", "title": str(i), "actions": ["AXPress"],
                      "parent": "c.0"} for i in range(30)]}
        return super().call(method, timeout, **p)


def test_a_look_into_every_app_does_not_hide_the_window_that_came_up(tmp_path):
    """The run itself, offline: the list of every app opened while the app was not answering, then its window."""
    h = Calculator()
    h.answering = False

    def blind(state, questions):
        h.answering = True                     # the window comes up while the decider looks into the apps
        return {"pick": "look into open another app"}
    d = ScriptedDecider([blind, {"pick": "button 「7」"}, {"pick": "done"}])
    res = Engine(cfg(tmp_path), helper=h, decider=d).do("work out 7 in the calculator")
    after = list(d.seen[1][1]["action"]["criteria"].values())
    assert sum(v.startswith("button 「") for v in after) == 30, "the keypad was not offered"
    assert res["steps"] == ["button 「7」"] and "invariants_broken" not in res["outputs"]


def test_on_the_same_window_the_opened_list_takes_only_the_room_the_screen_leaves(tmp_path):
    d = ScriptedDecider([{"pick": "look into open another app"}, {"pick": "button 「7」"}, {"pick": "done"}])
    res = Engine(cfg(tmp_path), helper=Calculator(), decider=d).do("work out 7 in the calculator")
    opened = list(d.seen[1][1]["action"]["criteria"].values())
    assert sum(v.startswith("button 「") for v in opened) == 30, "the keypad was pushed out by the list"
    assert sum(v.startswith("open app App") for v in opened) >= 60
    assert any(v.startswith("look into the rest of open another app") for v in opened)
    assert len(opened) <= 200 and "invariants_broken" not in res["outputs"]


class Form(FakeHelper):
    """A window of 40 buttons, more than the options hold (`engine.max_options` 30 below)."""

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "focused_window":
            self.calls.append((method, p))
            return {"nodes": [{"ref": "f.0", "role": "AXWindow", "title": "Form", "depth": 0}] +
                    [{"ref": f"f.{i + 1}", "role": "AXButton", "rdesc": "button", "title": f"f{i}", "actions": ["AXPress"],
                      "parent": "f.0"} for i in range(40)]}
        return super().call(method, timeout, **p)


def test_the_rest_of_the_window_is_offered_after_looking_into_it_and_stays_open_across_steps(tmp_path):
    """The tail of a screen too big for one choice is one "look into the rest of the window" away, and it stays
    open while the task works in that window: it is where the work is, not a menu to close after one pick."""
    shown: list[set[str]] = []                 # the buttons each look offered

    def seen(questions):
        values = questions["action"]["criteria"].values()
        shown.append({m.group(1) for v in values for m in [re.match(r"button 「(f\d+)」", v)] if m})
        return questions["action"]["criteria"]

    def look_into_the_rest(state, questions):
        crit = seen(questions)
        return {"pick": next((k for k, v in crit.items() if v.startswith("look into the rest of the window")), "done")}

    def press_one_from_the_rest(state, questions):
        seen(questions)
        rest = sorted(shown[1] - shown[0], key=lambda b: int(b[1:]))
        return {"pick": f"button 「{rest[0]}」" if rest else "done"}

    def done(state, questions):
        seen(questions)
        return {"pick": "done"}
    d = ScriptedDecider([look_into_the_rest, press_one_from_the_rest, done])
    res = Engine(cfg(tmp_path, config={"engine": {"max_options": 30}}), helper=Form(), decider=d).do("fill in the form")
    tail = {f"f{i}" for i in range(40)} - shown[0]
    assert tail, "the window fitted: there was nothing to look further into"
    assert len(shown) == 3, "the rest of the window was not one look away"
    assert tail <= shown[1], "looking into the rest of the window did not show its rest"
    assert len(res["steps"]) == 1 and res["steps"][0].split("「")[1].split("」")[0] in tail
    assert tail <= shown[2], "the rest of the window closed after a step taken in it"
    assert "invariants_broken" not in res["outputs"]
