"""An app that is starting or busy answers Accessibility late or not at all — and a look it did not answer is not
a look at the app.

The helper marks such an app and says `not_answering`; a 0.2.0 helper also says whether it is still launching.
The windows provider turned that blank into facts: `open_windows: []`, "none: the app has no window open", and
"bring back the main window" for an app whose window was on its way. The loop then asked the decider about the
blank, the decider voted rethink on 144 of the 158 such looks since 22ce357 and wait on none, and 53 plans were
made right after one. Now a look waits for the app first; what it has on screen is asked of the window server,
which needs no answer from it; and nothing is planned, ended or advanced on a look nobody could read.

No new name is imported at module level: each test fails at the base of this change on its own assertion,
except where the name it covers is itself new.
"""

import json
import time

from macwork.engine import Engine
from macwork.model import Observation
from macwork.observe import Ctx, get_provider
from tests.test_engine import FakeHelper, ScriptedDecider, cfg
from tests.test_flex import FakeBackend

APP = {"pid": 42, "name": "TextEdit", "bundle_id": "com.apple.TextEdit", "path": "/System/Applications/TextEdit.app"}
FINDER = {"pid": 7, "name": "Finder", "bundle_id": "com.apple.finder", "path": "/System/Library/CoreServices/Finder.app"}
# the app's own window as the window server lists it, which needs no answer from the app
MAIN = {"pid": 42, "id": 501, "owner": "TextEdit", "title": "Untitled", "layer": 0, "ordinary": True,
        "frame": [100, 100, 600, 400], "alpha": 1, "on_screen": True}
# another process's prompt in front of it
PROMPT = {"pid": 99, "id": 900, "owner": "Example Agent", "layer": 8, "ordinary": True, "regular": False,
          "frame": [250, 200, 300, 150], "alpha": 1, "on_screen": True}
PROMPT_TREE = {"nodes": [
    {"ref": "p.0", "role": "AXWindow", "title": "Example Agent", "depth": 0},
    {"ref": "p.1", "role": "AXStaticText", "value": "Example Agent wants to use your keychain", "parent": "p.0"},
    {"ref": "p.2", "role": "AXButton", "rdesc": "button", "title": "Allow", "actions": ["AXPress"], "parent": "p.0"},
], "ms": 2}


class Starting(FakeHelper):
    """A Mac whose app (pid 42) does not answer Accessibility until it has been asked `answer_on` times with
    ask_now (None: never; 0: it answers from the start). As a 0.2.0 helper does, every not-answering reply says
    whether the app is still launching, and an answered one says `launching` while it is; an `old` helper says
    neither. `windowless`: the app answers but has no window yet."""

    def __init__(self, answer_on=None, launching=False, launched_ago=1.0, screen=(), old=False, windowless=False):
        super().__init__()
        self.answer_on, self.launching, self.old, self.windowless = answer_on, launching, old, windowless
        self.launched = time.time() - launched_ago
        self.screen = list(screen)
        self.asked_now = 0

    def answering(self):
        return self.answer_on is not None and self.asked_now >= self.answer_on

    def call(self, method, timeout=30.0, **p):
        if method == "apps.running":
            self.calls.append((method, p))
            return [{**APP, "launched": self.launched}, FINDER]
        if method == "ax.snapshot" and p.get("pid") == 99:
            self.calls.append((method, p))
            return PROMPT_TREE
        if method == "ax.snapshot" and p.get("pid") == 42 and not p.get("ref"):
            self.asked_now += bool(p.get("ask_now"))
            said = {} if self.old else {"launching": self.launching}
            if not self.answering():
                self.calls.append((method, p))
                return {"nodes": [], "ms": 0, "truncated": False, "not_answering": True, **said}
            if self.windowless and p.get("scope") in ("windows", "focused_window"):
                self.calls.append((method, p))
                return {"nodes": [], "ms": 1, "truncated": False, **({"launching": True} if self.launching and not self.old else {})}
            reply = super().call(method, timeout, **p)
            return {**reply, "launching": True} if self.launching and not self.old else reply
        return super().call(method, timeout, **p)

    def asked_with_ask_now(self):
        return [p for m, p in self.calls if m == "ax.snapshot" and p.get("ask_now")]


def look(tmp_path, helper, providers, **config):
    """One observation through the named providers, as a look would take it."""
    c = cfg(tmp_path, config={"observe": {"providers": providers}, **config})
    ctx = Ctx(c, helper, app=dict(APP), running=helper.call("apps.running"))
    obs = Observation(app=ctx.app, window=None, affordances=[])
    for name in providers:
        get_provider(name)(ctx, obs)
    return obs


def engine(tmp_path, helper, script, providers=("apps", "menu", "window", "windows", "keys"), **config):
    over = {"observe": {"providers": list(providers)},
            "engine": {"open_front_s": 0.05, "ready_poll_ms": 1, "wait_s": 0.01, **config.pop("engine", {})}, **config}
    return Engine(cfg(tmp_path, config=over), helper=helper, decider=ScriptedDecider(script))


def audit(tmp_path, kind):
    path = tmp_path / "audit.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [r for r in rows if r.get("kind") == kind]


def reopened(obs):
    return [a for a in obs.affordances if (a.channel, a.verb) == ("app", "reopen")]


# ----------------------------------------------------------------- what a look says of an app that did not answer
def test_a_timeout_is_not_an_empty_window_list(tmp_path):
    obs = look(tmp_path, Starting(screen=[MAIN]), ["windows"])
    assert not reopened(obs), "the main window was offered back while it was on screen"
    assert obs.notes["open_windows"] != [], "a timeout was reported as the app having no window"
    assert len(obs.notes["open_windows"]) == 1 and "「Untitled」" in obs.notes["open_windows"][0] \
        and "not answering" in obs.notes["open_windows"][0]
    assert obs.notes["window_stand_in"]["id"] == 501 and obs.notes["window_not_answering"] is True


def test_not_answering_and_nothing_on_screen_claims_nothing(tmp_path):
    """Nothing of its own on screen — a status item and a toolbar strip are not its windows — leaves what it
    has open unknown: not an empty list, and no main window to bring back."""
    status_item = {**MAIN, "id": 502, "layer": 25, "ordinary": False, "frame": [900, 0, 30, 24]}
    strip = {**MAIN, "id": 503, "frame": [0, 60, 1512, 33]}
    obs = look(tmp_path, Starting(screen=[status_item, strip]), ["windows"])
    assert "open_windows" not in obs.notes, "an app that did not answer was said to have no window open"
    assert not reopened(obs)
    assert obs.notes["window_not_answering"] is True and obs.notes["window_stand_in"] is None


def test_an_app_still_launching_is_not_offered_its_main_window_back(tmp_path):
    obs = look(tmp_path, Starting(answer_on=0, launching=True, windowless=True), ["window", "windows"])
    assert obs.notes["open_windows"] == [] and not reopened(obs), "a window on its way was offered back"
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert eng._evidence(obs)["open_windows"] == "none yet: the app is still starting"


def test_a_launch_that_never_finishes_is_not_starting_for_ever(tmp_path):
    """Apple documents that some processes never report finished launching: one launched longer ago than
    engine.open_front_s is taken as launched, and an app with no window is offered it back as before."""
    from macwork.onscreen import app_readiness, unreadable
    h = Starting(answer_on=0, launching=True, launched_ago=60, windowless=True)
    obs = look(tmp_path, h, ["window", "windows"])
    assert "app_launching" not in obs.notes and unreadable(obs) == ""
    assert len(reopened(obs)) == 1
    state = app_readiness(h, 42, h.call("apps.running"), launch_bound_s=6)
    assert state["launching"] is False and state["ready"] is True
    recent = app_readiness(Starting(answer_on=0, launching=True, launched_ago=1, windowless=True), 42,
                           Starting(launched_ago=1).call("apps.running"), launch_bound_s=6)
    assert recent["launching"] is True and recent["ready"] is False, "the guard: a recent launch is still starting"


# ----------------------------------------------------------------- what the decider and the planner are told
def test_the_decider_is_told_the_app_is_starting_not_that_it_has_no_window(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    obs = look(tmp_path, Starting(launching=True, screen=[MAIN]), ["window", "windows"])
    said = eng._evidence(obs)
    text = json.dumps(said, ensure_ascii=False)
    assert "none: the app has no window open" not in text and "nothing of its window can be read" not in text
    assert said["app_not_answering"].startswith("it is still starting: it does not answer Accessibility yet")
    assert "its window 「Untitled」 is on screen" in said["app_not_answering"]
    assert "Untitled" in said["open_windows"][0]
    busy = eng._evidence(look(tmp_path, Starting(), ["window", "windows"]))
    assert busy["app_not_answering"].startswith("it is busy") and busy["app_not_answering"].endswith("none of its windows is on screen yet")
    assert "open_windows" not in busy
    # the focused window's own snapshot says so too, where the window server is not asked
    alone = eng._evidence(look(tmp_path, Starting(launching=True, screen=[MAIN]), ["window"]))
    assert alone["app_not_answering"] == "it is still starting: it does not answer Accessibility yet, so its controls and menus cannot be read"


def test_the_planner_is_told_the_same(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    task = eng._new_task("write a note", {}, None)
    ctx = Ctx(eng.cfg, FakeHelper(), app=dict(APP))
    blind = look(tmp_path, Starting(launching=True, screen=[MAIN]), ["window", "windows"])
    for acting in (True, False):
        brief = eng._brief(task, ctx, blind, acting=acting)
        assert brief["app_not_answering"] == eng._evidence(blind)["app_not_answering"], f"acting={acting}"
        assert brief["open_windows"] == eng._evidence(blind)["open_windows"]
    starting = look(tmp_path, Starting(answer_on=0, launching=True, windowless=True), ["window", "windows"])
    for acting in (True, False):
        assert eng._brief(task, ctx, starting, acting=acting)["open_windows"] == "none yet: the app is still starting"


# ----------------------------------------------------------------- the look waits for the app
def test_the_decider_is_not_asked_until_the_app_answers(tmp_path):
    h = Starting(answer_on=2, launching=True, screen=[MAIN])
    eng = engine(tmp_path, h, [{"pick": "done"}], engine={"open_front_s": 2})
    res = eng.do("make a new document")
    assert not [s for s, q in eng.decider.seen if "app_not_answering" in s], "the decider was asked about a look the app did not answer"
    assert res["status"] == "done" and res["outputs"]["budget"]["ready_waits"]["used"] == 1
    waits = audit(tmp_path, "ready_wait")
    assert len(waits) == 1 and waits[0]["answered"] is True and waits[0]["why"] == "starting"
    assert waits[0]["app"] == "com.apple.TextEdit" and waits[0]["task"] == res["task_id"]
    assert len(h.asked_with_ask_now()) == 2, "the app was not asked again at once while the look waited"


def test_the_wait_is_bounded_and_not_repeated_for_an_app_that_stays_silent(tmp_path):
    h = Starting(screen=[MAIN])
    asked_by_then = []

    def escape(state, questions):     # what had been asked with ask_now by the time each decision was asked for
        asked_by_then.append(len(h.asked_with_ask_now()))
        return {"pick": "escape"}
    eng = engine(tmp_path, h, [escape, escape], engine={"max_steps": 2})
    res = eng.do("close the panel")
    assert res["outputs"]["budget"]["ready_waits"]["used"] == 1
    waits = audit(tmp_path, "ready_wait")
    assert len(waits) == 1 and waits[0]["answered"] is False and waits[0]["why"] == "busy"
    assert waits[0]["ms"] >= 50, "the wait ended before engine.open_front_s"
    assert asked_by_then[0] >= 1 and asked_by_then == [asked_by_then[0]] * 2 == [len(h.asked_with_ask_now())] * 2, \
        "a look after the first waited again for an app that had stayed silent through a whole wait"
    assert all("does not answer Accessibility yet" in s["app_not_answering"] for s, q in eng.decider.seen)


def test_an_older_helper_is_not_polled(tmp_path):
    """A helper older than 0.2.0 never says `launching` and ignores ask_now, keeping its 20 s cooldown: polling
    it would only add engine.open_front_s to every launch."""
    h = Starting(old=True, screen=[MAIN])
    eng = engine(tmp_path, h, [{"pick": "escape"}, {"pick": "escape"}], engine={"max_steps": 2, "open_front_s": 5})
    res = eng.do("close the panel")
    assert res["outputs"]["budget"]["ready_waits"]["used"] == 0
    assert not h.asked_with_ask_now() and not audit(tmp_path, "ready_wait")
    assert len(eng.decider.seen) == 2


# ----------------------------------------------------------------- nothing is judged on a look nobody could read
def test_a_blind_look_is_never_planned_on_and_ends_with_its_own_cause(tmp_path):
    """The planner is not asked while a wait is still possible. Once none is — the look waited its whole bound
    and the decider's waits are spent — it may be, told that the app does not answer: for an app that never
    does, a way around it is the only way on."""
    h = Starting(screen=[MAIN])
    back = FakeBackend([{"steps": ["open the TextEdit window"], "inputs": {}}] * 4)
    asked_by_then = []

    def rethink(state, questions):       # how often the planner had been asked when each decision was asked for
        asked_by_then.append(len(back.prompts))
        return {"pick": "escape", "move": "rethink"}
    eng = engine(tmp_path, h, [rethink] * 3, planner={"when": "always"},
                 engine={"max_waits": 1, "max_steps": 2, "checkpoint": False})
    eng._planner = back
    res = eng.do("close the panel")
    assert asked_by_then[0] == 0, "the planner was asked for a route while the app could still be waited for"
    assert len(back.prompts) == 1 and "does not answer Accessibility yet" in back.prompts[0]
    ledger = res["outputs"]["budget"]
    assert ledger["waits"]["used"] == 1 and ledger["fruitless"]["used"] == 0 and ledger["ready_waits"]["used"] == 1
    assert len(eng.decider.seen) == 3 and len(h.did("input.key")) == 2, "the first rethink was not a wait, or a wait acted"
    assert res["cause"] == "app_not_answering" and "TextEdit is busy" in res["reason"]
    assert "no route" not in res["reason"]


def test_a_blocked_vote_on_a_silent_app_still_stops_and_says_why(tmp_path):
    """A stop is never turned into an action, whatever the look could read: it waits while the decider may,
    and then stops, naming the app that did not answer."""
    h = Starting(screen=[PROMPT, MAIN])
    eng = engine(tmp_path, h, [{"pick": "escape", "move": "blocked"}] * 2,
                 providers=("apps", "menu", "window", "overlays", "windows", "keys"), engine={"max_waits": 1})
    res = eng.do("close the panel")
    assert res["status"] == "blocked" and res["cause"] == "app_not_answering" and "TextEdit is busy" in res["reason"]
    assert all(s.get("covered_by") for s, q in eng.decider.seen), "the other process's prompt was not in front of the decider"
    assert res["outputs"]["budget"]["waits"]["used"] == 1 and len(eng.decider.seen) == 2
    pressed = [m for m, p in h.calls if (m.startswith("input.") and m != "input.idle") or m == "ax.perform"]
    assert not pressed, f"something was pressed: {pressed}"      # input.idle only asks how long the Mac has been idle


def test_a_sub_goal_does_not_advance_on_a_blind_look(tmp_path):
    class GoesSilent(Starting):          # it answers until the first action, and never again
        def call(self, method, timeout=30.0, **p):
            if method == "ax.perform":
                self.answer_on = None
            return super().call(method, timeout, **p)
    h = GoesSilent(answer_on=0, screen=[MAIN])
    eng = engine(tmp_path, h, [{"pick": "New Document"}, {"pick": "escape", "step_done": 0.95}, {"pick": "done"}],
                 planner={"when": "always", "second_opinion_on_done": False})
    eng._planner = FakeBackend([{"steps": ["make a document", "close it"], "inputs": {}}])
    res = eng.do("make a new document and close it")
    assert res["plan"]["at"] == 0, "a sub-goal was judged done on a look the app did not answer"
    assert [s for s, q in eng.decider.seen if "app_not_answering" in s and "step_done" in q]
