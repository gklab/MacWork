"""What can be done with an element, asked of the element.

The role lists in `observe.window` used to *be* the definition: a control whose role was not listed could not
be typed into, selected or read, however plainly it offered to be. A web input, a `contenteditable` group, a
custom control — invisible. Likewise `action_labels`: an AX action outside that table was discarded, which
hid `AXRaise`, `AXCancel`, `AXDelete` and every action an app defines for itself.

Measured on a real Mac, capability probing finds what the lists missed — typing targets 3 → 18 in an editor,
2 → 9 in a chat app. But it is not a replacement, it is a second source: the Mac's "this attribute is not
settable" is not the last word, because the engine can click a row or focus a field and type. So the two are
taken together, and these tests pin both directions.
"""

from macwork.model import Observation
from macwork.observe import Ctx, element_affordances
from tests.test_engine import FakeHelper, cfg


def _offer(tmp_path, nodes, **window):
    obs = Observation(app={"pid": 42, "name": "app"}, window=None, affordances=[])
    conf = {"observe": {"window": window}} if window else {}
    ctx = Ctx(cfg(tmp_path, config=conf), FakeHelper(), app={"pid": 42, "name": "app"})
    element_affordances(ctx, obs, [{"ref": "w.0", "role": "AXWindow", "depth": 0}] + nodes, "w")
    return {(a.verb, a.label) for a in obs.affordances}


def test_a_control_with_an_unlisted_role_can_still_be_typed_into(tmp_path):
    """A web input reports AXGroup and would never have been offered."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXGroup", "rdesc": "group", "title": "Message",
                                 "editable": True, "parent": "w.0"}])
    assert any(verb == "type" and "Message" in label for verb, label in offered)


def test_a_listed_role_is_still_offered_when_the_app_says_it_is_not_settable(tmp_path):
    """The engine focuses the field and types; a refused attribute set is not the last word."""
    node = {"ref": "w.1", "role": "AXTextField", "rdesc": "text field", "title": "Recipient", "parent": "w.0"}
    assert any(verb == "type" for verb, _ in _offer(tmp_path, [node]))


def test_a_settable_number_is_not_a_typing_target(tmp_path):
    """A scroll bar's position is settable too — it offered "type into the scroll bar (now: 0)"."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXScrollBar", "rdesc": "scroll bar", "value": "0",
                                 "parent": "w.0"}])
    assert not any(verb.startswith("type") for verb, _ in offered)


def test_selecting_works_from_either_source(tmp_path):
    capability = _offer(tmp_path, [{"ref": "w.1", "role": "AXCell", "rdesc": "cell", "title": "First Item",
                                    "selectable": True, "parent": "w.0"}])
    assert any(verb == "select" and "First Item" in label for verb, label in capability)

    by_role = _offer(tmp_path, [{"ref": "w.1", "role": "AXRow", "rdesc": "table row", "title": "Second Item", "parent": "w.0"}])
    assert any(verb == "select" and "Second Item" in label for verb, label in by_role)


def test_an_action_outside_the_table_is_offered_under_the_apps_own_name(tmp_path):
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXWindow", "rdesc": "Window", "title": "Document",
                                 "actions": ["AXRaise"], "action_desc": {"AXRaise": "Bring To Front"}, "parent": "w.0"}])
    assert any("Bring To Front" in label for _, label in offered), offered


def test_an_extra_action_is_not_offered_where_a_named_one_already_reaches_the_element(tmp_path):
    """AXScrollToVisible sat on 428 of 441 elements that all had AXPress: 428 more options, no more ability."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXButton", "rdesc": "button", "title": "Send",
                                 "actions": ["AXPress", "AXScrollToVisible"],
                                 "action_desc": {"AXScrollToVisible": "Scroll To Visible"}, "parent": "w.0"}])
    assert any(verb == "press" and "Send" in label for verb, label in offered)
    assert not any("Scroll To Visible" in label for _, label in offered)


def test_an_element_that_both_takes_text_and_has_actions_offers_both(tmp_path):
    """A table cell takes text and has a context menu; finding more typing targets must not remove actions."""
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXCell", "rdesc": "cell", "title": "Notes",
                                 "editable": True, "actions": ["AXShowMenu"], "parent": "w.0"}])
    assert any(verb == "type" for verb, _ in offered)
    assert any("context menu" in label for _, label in offered)


def test_the_old_behaviour_is_one_setting_away(tmp_path):
    offered = _offer(tmp_path, [{"ref": "w.1", "role": "AXGroup", "rdesc": "group", "title": "Message",
                                 "editable": True, "parent": "w.0"}], by_capability=False)
    assert not any(verb == "type" for verb, _ in offered)


# --- not paying for what cannot be read -------------------------------------------------------------------

def test_an_empty_tree_is_not_retried_as_a_sparse_one(tmp_path):
    """Manual accessibility unlocks a *thin* Electron tree. It cannot conjure a window that is not open —
    asking again just waits out the same timeouts: measured at 1516 ms for nothing, then 2018 ms more."""
    from macwork.model import Observation
    from macwork.observe import window

    class NoWindow(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "focused_window":
                self.calls.append((method, p))
                return {"nodes": [], "ms": 1516}
            return super().call(method, timeout, **p)

    ctx = Ctx(cfg(tmp_path), NoWindow(), app={"pid": 42, "name": "app"})
    window(ctx, Observation(app=ctx.app, window=None, affordances=[]))
    assert len(ctx.helper.did("ax.snapshot")) == 1, "it asked twice for a window that is not there"


def test_reading_the_screen_is_skipped_when_there_is_no_window(tmp_path):
    """The capture waits for a window that does not exist, and an empty tree reads as "sparse"."""
    from macwork.model import Observation
    from macwork.observe import vision

    obs = Observation(app={"pid": 42}, window=None, affordances=[], notes={"open_windows": []})
    ctx = Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "app"})
    vision(ctx, obs)
    assert not ctx.helper.did("screen.ocr")


def test_an_app_that_does_not_answer_says_so_to_the_decider(tmp_path):
    from macwork.model import Observation
    from macwork.observe import window

    class Silent(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "focused_window":
                return {"nodes": [], "ms": 0, "not_answering": True}
            return super().call(method, timeout, **p)

    obs = Observation(app={"pid": 42}, window=None, affordances=[])
    window(Ctx(cfg(tmp_path), Silent(), app={"pid": 42, "name": "app"}), obs)
    assert obs.notes["window_not_answering"] is True


def test_the_whole_window_can_be_read_into_facts(tmp_path):
    """`screen_text` is a capped summary of what is visible. The thing a goal is about is usually below the
    fold — and this is not about the web: a browser is an app with a lot of text in it, like any other."""
    from macwork.act import get_channel
    from macwork.model import Affordance

    long_page = [{"ref": "w.0", "role": "AXWindow", "depth": 0}] + [
        {"ref": f"w.{i}", "role": "AXStaticText", "value": f"paragraph {i} contents", "parent": "w.0"} for i in range(1, 40)]

    class Paged(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("visible_only") is False:
                self.calls.append((method, p))
                return {"nodes": long_page}
            return super().call(method, timeout, **p)

    ctx = Ctx(cfg(tmp_path), Paged(), app={"pid": 42, "name": "Reading"})
    out = get_channel("window")(ctx, Affordance("t0", "window", "read_all", "read all", {"pid": 42}), {})

    assert out.ok and "paragraph 39 contents" in out.output["read_window"]["text"]
    asked = ctx.helper.did("ax.snapshot")[0]
    assert asked["visible_only"] is False, "what is scrolled away is the point"
    assert asked["actions"] is False, "it is being read, not operated"


def test_acting_step_by_step_needs_no_decider_when_classification_is_off(tmp_path):
    """The step level is documented as "the caller drives". Building the gate unconditionally meant the
    switch did nothing and this level could not be used without an API key at all."""
    from macwork.engine import Engine

    engine = Engine(cfg(tmp_path, policy={"confirm": {"classify_step_level": False}}), helper=FakeHelper())
    seen = engine.observe()
    pressable = next(a for a in seen["affordances"] if a["verb"] == "press" and not a.get("slots"))
    assert engine.act(pressable["id"])["ok"] is True      # would have raised DeciderError


# --------------------------------------------------------------------------- a path the user named

def test_a_path_the_goal_names_can_be_opened_read_or_shown(tmp_path):
    """Three real tasks failed the same way: the goal named an absolute path, and the engine drove Finder's
    "Go to Folder" — opening it, pressing Return, opening it again — because nothing offered the path itself.
    The Mac can be asked whether that path exists, and what to do with it follows from the answer."""
    from macwork.observe import PROVIDERS

    (tmp_path / "docs").mkdir()
    (tmp_path / "keep.txt").write_text("keep me", encoding="utf-8")
    obs = Observation(app=None, window=None, affordances=[])
    ctx = Ctx(cfg(tmp_path), FakeHelper(), goal=f"Delete {tmp_path}/keep.txt", running=[])
    PROVIDERS["files"](ctx, obs)

    verbs = {a.verb for a in obs.affordances}
    assert verbs == {"open", "reveal", "read", "trash"}, [a.label for a in obs.affordances]
    assert all(str(tmp_path / "keep.txt") == a.target["path"] for a in obs.affordances)


def test_the_apps_the_system_says_can_open_it_are_offered_by_name(tmp_path):
    """"Open it in Safari" can only be followed if Safari is an option: a real task was told to open a page in
    Safari, took the one "open it" on offer, and the Mac's default browser got it instead. Which apps can open
    a file is the system's answer, not a table of extensions kept here."""
    from macwork.observe import PROVIDERS

    (tmp_path / "news.html").write_text("<title>Morning News</title>", encoding="utf-8")
    helper = FakeHelper()
    helper.openers = [{"name": "Safari", "bundle_id": "com.apple.Safari", "path": "/Applications/Safari.app"},
                      {"name": "Chrome", "bundle_id": "com.google.Chrome", "path": "/Applications/Chrome.app"}]
    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["files"](Ctx(cfg(tmp_path), helper, goal=f"open in Safari: {tmp_path}/news.html", running=[]), obs)

    with_safari = next(a for a in obs.affordances if "Safari" in a.label)
    assert with_safari.target["app"] == "/Applications/Safari.app"
    assert sum(1 for a in obs.affordances if a.verb == "open") == 3      # the plain one, plus the two named


def test_a_path_that_is_not_there_is_not_offered(tmp_path):
    from macwork.observe import PROVIDERS

    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["files"](Ctx(cfg(tmp_path), FakeHelper(), goal=f"Open {tmp_path}/nope.txt", running=[]), obs)
    assert obs.affordances == []


def test_a_folder_the_goal_names_is_offered_without_a_way_to_read_it(tmp_path):
    from macwork.observe import PROVIDERS

    (tmp_path / "docs").mkdir()
    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["files"](Ctx(cfg(tmp_path), FakeHelper(), goal=f"{tmp_path}/docs how many files are in it？", running=[]), obs)
    assert {a.verb for a in obs.affordances} == {"open", "reveal", "trash"}   # a folder has no text to read
    assert any("folder" in a.label for a in obs.affordances)
