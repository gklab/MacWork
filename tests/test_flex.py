"""The parts that make unseen apps workable: scripting dictionaries, vision naming, learning, routines, planners."""

import json

import pytest

from macwork.act import _as_literal
from macwork.appmodel import AppModels, parse_sdef, signature
from macwork.engine import Engine
from macwork.model import Affordance, Observation
from macwork.observe import Ctx, element_affordances, vision
from macwork.planner import OpenAICompatPlanner, Planning, _json_from
from macwork.privacy import Audit, Redactor
from tests.test_engine import worded, FakeHelper, ScriptedDecider, cfg

SDEF = """<?xml version="1.0"?><!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd">
<dictionary><suite name="Standard Suite"><command name="quit" code="aevtquit"/></suite>
<suite name="Player Suite"><command name="playpause" code="hookPlPs" description="toggle play"/>
<command name="open location" code="GURLGURL"><direct-parameter type="text" description="the URL"/></command>
<command name="play track" code="x"><direct-parameter type="track"/></command>
<command name="secret" code="y" hidden="yes"/></suite></dictionary>"""


def test_parse_sdef_and_literals():
    m = parse_sdef(SDEF)
    names = [c["name"] for c in m["commands"]]
    assert names == ["quit", "playpause", "open location", "play track"]            # hidden skipped
    assert m["commands"][2]["direct"] == {"type": "text", "optional": False, "desc": "the URL"}
    assert _as_literal('a "b" \\c', "text") == '"a \\"b\\" \\\\c"'                  # quoted, never raw code
    assert _as_literal("/tmp/x", "file") == 'POSIX file "/tmp/x"'
    with pytest.raises(ValueError):
        _as_literal("1; do shell script", "integer")


def test_signature_ignores_numbers():
    a = signature({"bundle_id": "x"}, "Calc 12", ["display 123", "button 「7」"])
    b = signature({"bundle_id": "x"}, "Calc 99", ["display 456", "button 「7」"])
    assert a == b and a != signature({"bundle_id": "x"}, "Other", ["display 1"])


def test_element_affordances_offers_every_configured_action_and_sets_aside_unlabeled(tmp_path):
    nodes = [{"ref": "r0", "role": "AXWindow", "title": "W"},
             {"ref": "r1", "role": "AXRow", "title": "Report.pdf", "actions": ["AXPress", "AXShowMenu"], "parent": "r0"},
             {"ref": "r2", "role": "AXButton", "actions": ["AXPress", "AXShowMenu"], "frame": [10, 10, 20, 20], "parent": "r0"},
             {"ref": "r3", "role": "AXButton", "subrole": "AXCloseButton", "rdesc": "close button", "actions": ["AXPress"], "parent": "r0"},
             {"ref": "r4", "role": "AXScrollArea", "desc": "list", "actions": ["AXScrollDownByPage"], "parent": "r0"}]
    obs = Observation(app=None, window=None, affordances=[])
    element_affordances(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 1}), obs, nodes, "w")
    labels = [a.label for a in obs.affordances]
    assert "row 「Report.pdf」" in labels and "open the context menu of row 「Report.pdf」" in labels
    assert "close button 「close button」" in labels                                        # a subrole names itself
    assert "scroll down scrollarea 「list」" in labels
    assert [u["ref"] for u in obs.notes["unlabeled"]] == ["r2"]                    # buttons expose AXShowMenu everywhere: not offered


class OCRHelper(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "screen.ocr":
            return {"frame": [0, 0, 300, 300], "boxes": [{"text": "Export", "frame": [12, 12, 10, 10]}, {"text": "words on the canvas", "frame": [150, 200, 40, 12]}], "ms": 5}
        return super().call(method, timeout, **p)


def test_vision_names_unlabeled_controls_and_offers_text_when_tree_is_sparse(tmp_path):
    obs = Observation(app=None, window=None, affordances=[])
    obs.notes.update({"window_actionable": 1, "window_frame": [0, 0, 300, 300],
                      "unlabeled": [{"ref": "r2", "role": "AXButton", "rdesc": "button", "frame": [10, 10, 20, 20], "action": "AXPress", "pid": 1}]})
    vision(Ctx(cfg(tmp_path), OCRHelper(), app={"pid": 1}), obs)
    named = next(a for a in obs.affordances if a.target.get("ref") == "r2")
    assert "「Export」" in named.label and named.channel == "window"
    click = next(a for a in obs.affordances if a.channel == "pointer")
    assert "words on the canvas" in click.label and click.target["x"] == 170 and "words on the canvas" in obs.screen_text


def test_app_model_learns_edges_and_dead_actions(tmp_path):
    models = AppModels(cfg(tmp_path))
    app = {"bundle_id": "dev.test", "path": ""}
    models.record(app, "s1", "menu View ▸ Sidebar", "s2", True, True)
    models.record(app, "s1", "button 「Invalid」", "s1", True, False)
    models.see(app, "s2", "With Sidebar", ["x"])
    h = models.hints(app, "s1", {"menu View ▸ Sidebar", "button 「Invalid」"})
    assert h["known_effects"][0].startswith("「menu View ▸ Sidebar」 -> With Sidebar") and h["no_effect_before"] == ["button 「Invalid」"]
    models.save(app)
    assert json.loads((tmp_path / "apps" / "dev.test@0.json").read_text())["edges"][0]["to"] == "s2"


def test_engine_records_learning_and_a_routine_then_replays_it(tmp_path):
    c = cfg(tmp_path, policy={"confirm": {"mode": "never"}})     # "Send" (send) would otherwise ask first
    eng = Engine(c, helper=FakeHelper(), decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "Send"}, {"pick": "done", "done": 0.95}]))
    res = eng.do("new and send")
    assert res["status"] == "done" and res["outputs"].get("learned_routine")
    skill = eng.skills.all()[0]
    assert [s["label"] for s in skill["steps"]] == ["menu File ▸ New Document (⌘N)", "button 「Send」"]
    model = eng.models.get(eng.helper.call("apps.frontmost")["app"])
    assert model["edges"] or model["dead"]                                            # where the steps led was recorded
    eng2 = Engine(c, helper=FakeHelper(), decider=ScriptedDecider([{"pick": "replay the learned routine"}, {"pick": "done", "done": 0.95}]))
    eng2.cfg.docs["config"]["observe"]["providers"] = ["menu", "window", "skills"]
    res2 = eng2.do("do new-and-send once more")
    routine = {k: v for k, v in res2["outputs"]["routines"][0].items() if k != "id"}
    assert res2["status"] == "done" and routine == {"goal": "new and send", "steps_done": 2, "of": 2, "ok": True}
    assert eng2.skills.all()[0]["uses"] == 1 and "learned_routine" not in res2["outputs"]   # a replay is not re-recorded


class FakeBackend:
    name = "fake-cloud"
    local = False

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, prompt, schema):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_planning_redacts_for_cloud_and_restores_pseudonyms(tmp_path):
    c = cfg(tmp_path)
    r = Redactor(c, entities=lambda ts: [[{"type": "PERSON", "text": "Zhang Wei"}] if "Zhang Wei" in t else [] for t in ts])
    back = FakeBackend([{"steps": ["open Messages", "write ⟦PERSON_1⟧ a message"], "inputs": {"text": "hello ⟦PERSON_1⟧"}}])
    plan = Planning(c, back, r, Audit(c)).plan("message Zhang Wei to say hello", {"screen_text": "Contacts Zhang Wei"})
    assert "Zhang Wei" not in back.prompts[0] and "⟦PERSON_1⟧" in back.prompts[0]
    assert plan == {"steps": ["open Messages", "write Zhang Wei a message"], "inputs": {"text": "hello Zhang Wei"}, "try": [], "blocked": ""}


def test_engine_consults_planner_on_rethink_and_lets_it_write_text(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([
        {"pick": "New Document", "move": "rethink"},                             # nothing on screen fits -> ask the planner
        {"pick": "Recipient"},                                                  # with a plan; the field needs text
        {"pick": "done"}]))
    eng._planner = FakeBackend([{"steps": ["fill in the recipient"], "inputs": {}}, {"text": "zw@example.com"}])
    res = eng.do("fill in the recipient")
    assert res["status"] == "done" and res["plan"]["steps"] == ["fill in the recipient"]
    assert eng.helper.did("input.type")[0]["text"] == "zw@example.com"          # typed like a person, not set behind its back
    assert res["outputs"]["typed_by_planner"][0]["text"] == "zw@example.com"    # and it is on the screen, not invented
    assert eng.decider.seen[1][0]["current_step"] == "fill in the recipient"


def test_a_value_the_task_never_saw_is_not_typed(tmp_path):
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "Recipient"}, {"pick": "done"}])
    d.stands = 0.0                        # an address from nowhere: no source holds it, in any wording
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["fill in the recipient"], "inputs": {}}, {"text": "someone@example.com"}])
    res = eng.do("fill in the recipient")
    assert res["status"] == "need_input"                                       # nobody has seen that address
    assert not eng.helper.did("input.type") and res["outputs"]["refused_text"][0]["text"] == "someone@example.com"
    assert any("stands" in q for _, q in d.side), "it was dropped without being judged"


def test_a_value_the_task_has_may_be_written_the_way_the_field_needs_it(tmp_path):
    """A path typed into an address bar becomes file:///… — the same value, one word longer. Word-for-word
    matching called that an invention and ended a real task at step zero, before anything had been typed."""
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "Recipient"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["open the page"], "inputs": {}},
                                {"text": "file:///Users/me/Caches/form.html"}])
    res = eng.do("Open /Users/me/Caches/form.html")
    assert eng.helper.did("input.type")[0]["text"] == "file:///Users/me/Caches/form.html"
    assert "refused_text" not in res["outputs"]


def test_deepseek_endpoint_request_shape(monkeypatch, tmp_path):
    c = cfg(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    p = OpenAICompatPlanner(c, "deepseek", c.get("planner.endpoints.deepseek"))
    assert p.available() and p.base == "https://api.deepseek.com" and p.model == "deepseek-flash"
    sent = []

    def post(path, body, timeout):
        sent.append((path, body))
        return {"choices": [{"message": {"content": "" if len(sent) == 1 else '{"steps": ["a"], "inputs": {}}'}}]}  # first reply empty
    monkeypatch.setattr(p, "_post", post)
    assert p.complete("sys", "plan it", {"properties": {"steps": {"type": "array"}, "inputs": {"type": "object"}}}) == {"steps": ["a"], "inputs": {}}
    path, body = sent[0]
    assert path == "/chat/completions" and body["response_format"] == {"type": "json_object"} and body["model"] == "deepseek-flash"
    assert "JSON" in body["messages"][1]["content"] and len(sent) == 2               # json mode: says JSON, retried the empty reply


def test_json_from_tolerates_thinking_and_prose():
    assert _json_from('<think>hmm {no}</think>Sure: {"text": "hi {x}"} done') == {"text": "hi {x}"}


def test_feedback_removes_a_routine_learned_from_a_wrong_done(tmp_path):
    c = cfg(tmp_path, policy={"confirm": {"mode": "never"}})
    eng = Engine(c, helper=FakeHelper(), decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "Send"}, {"pick": "done", "done": 0.95}]))
    res = eng.do("new and send")
    assert eng.skills.all()
    assert eng.feedback(res["task_id"], ok=False, note="check failed")["routine_removed"] and not eng.skills.all()


def test_text_change_counts_as_an_effect(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    app = {"bundle_id": "dev.calc", "path": ""}
    task = __import__("macwork.model", fromlist=["Task"]).Task(goal="x")
    task.prev = {"sig": "s1", "label": "button 「2」", "ok": True, "events": [], "app": app, "screen": "0"}
    eng._learn_from_prev(task, app, "s1", Observation(app=app, window="Calc", affordances=[], screen_text="2"))
    assert eng.models.get(app)["dead"] == []                   # same screen, no event, but the display changed


class GalleryHelper(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "screen.ocr":
            self.calls.append((method, p))
            return {"frame": [0, 0, 400, 400], "boxes": [{"text": "Blank", "frame": [20, 20, 30, 12]}, {"text": "Personal Budget", "frame": [120, 20, 50, 12]}], "ms": 5}
        return super().call(method, timeout, **p)


def test_unlabeled_container_offers_each_text_inside_as_a_target(tmp_path):
    obs = Observation(app=None, window=None, affordances=[])
    obs.notes.update({"window_actionable": 20, "window_frame": [0, 0, 400, 400],
                      "unlabeled": [{"ref": "g", "role": "AXScrollArea", "rdesc": "scroll area", "frame": [0, 0, 300, 100], "action": "AXScrollDownByPage", "pid": 1}]})
    ctx = Ctx(cfg(tmp_path), GalleryHelper(), app={"pid": 1})
    vision(ctx, obs)                                  # the tree is not sparse: reading the screen is offered, not done
    assert [(a.channel, a.verb) for a in obs.affordances] == [("vision", "reveal")] and not ctx.helper.did("screen.ocr")
    ctx.cache.setdefault("vision.wanted", {}).setdefault(ctx.task, set()).add(obs.affordances[0].target["key"])
    obs.affordances.clear()
    vision(ctx, obs)
    labels = [a.label for a in obs.affordances]
    assert labels == ["click 「Blank」 in scroll area", "click 「Personal Budget」 in scroll area"]
    obs.affordances.clear()
    vision(ctx, obs)                                  # same window content: read once
    assert len(ctx.helper.did("screen.ocr")) == 1 and obs.notes.get("vision_cached")
    assert obs.affordances[0].channel == "pointer" and obs.affordances[0].target["x"] == 35


def test_recorded_routines_drop_detours_but_keep_real_repeats():
    from macwork.skills import _without_detours
    st = lambda lab, before: {"channel": "menu", "verb": "press", "label": lab, "context": "", "inputs": [], "before": before}
    # Scientific from basic mode, Mode back to basic, Scientific again from basic: the middle was a detour
    assert [x["label"] for x in _without_detours([st("Scientific", "basic:0"), st("Mode", "sci:0"), st("Scientific", "basic:0")])] == ["Scientific"]
    # 1 + 1 =: the second "1" is pressed from a different display ("1+"), so it is real
    assert [x["label"] for x in _without_detours([st("1", "calc:0"), st("plus", "calc:1"), st("1", "calc:1+"), st("equals", "calc:1+1")])] == ["1", "plus", "1", "equals"]
    # no recorded state (e.g. replayed steps): never pruned
    assert len(_without_detours([st("x", None), st("y", None), st("x", None)])) == 3


def test_json_from_keeps_the_complete_part_of_a_cut_off_reply():
    cut = '{\n  "steps": [\n    "open 「Settings」",\n    "click 「Log In」",\n    "click 「Sign'
    assert _json_from(cut) == {"steps": ["open 「Settings」", "click 「Log In」"]}


def test_a_planner_with_rejected_credentials_hands_over_to_the_next():
    from macwork.planner import Chain, PlannerError

    class Refused:
        name = "cloud"
        def complete(self, *a):
            raise PlannerError("cloud: credentials rejected (401)", refused=True)

    class Works:
        name, local = "local", True
        def complete(self, *a):
            return {"steps": ["x"]}
    chain = Chain([Refused(), Works()])
    assert chain.name == "cloud" and chain.complete("s", "p", {}) == {"steps": ["x"]}
    assert chain.name == "local" and chain.local       # the rejected one is gone for the session (and redaction follows)


def test_learning_explores_only_what_the_decider_judges_harmless_and_caches_it(tmp_path):
    class Judge:
        calls, cost_usd, last_ms = 0, 0.0, 1.0
        def decide(self, state, questions):
            self.calls += 1
            return {k: {"type": "noul", "noul": 0.95 if "Show Sidebar" in worded(q) else 0.1} for k, q in questions.items()}
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Judge())
    ctx = Ctx(eng.cfg, eng.helper, app={"pid": 42, "name": "TextEdit", "bundle_id": "x"})
    obs = Observation(app=None, window="w", affordances=[])
    cands = [Affordance("a", "menu", "press", "menu Show ▸ Show Sidebar"), Affordance("b", "menu", "press", "menu Format ▸ Bold")]
    assert [a.id for a in eng._safe_to_explore(ctx, obs, cands)] == ["a"]
    assert [a.id for a in eng._safe_to_explore(ctx, obs, cands)] == ["a"] and eng.decider.calls == 1   # judged once


def test_learning_undoes_with_what_the_decider_picks_not_fixed_keys(tmp_path):
    class Back:
        calls, cost_usd, last_ms = 0, 0.0, 1.0
        def decide(self, state, questions):
            opts = questions["back"]["criteria"]
            return {"back": {"type": "choice", "choice": next(k for k, v in opts.items() if "escape" in v)}}
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=Back())
    ctx = Ctx(eng.cfg, eng.helper, app={"pid": 42, "name": "TextEdit", "bundle_id": "x"})
    obs = Observation(app=None, window="Inspector", affordances=[Affordance("w1", "window", "press", "button 「Delete」"),
                                                                  Affordance("w2", "window", "press", "button 「Done」")])
    back = eng._way_back(ctx, obs, "Untitled", "menu Show ▸ Inspector")
    assert back.label == "press the escape key"


class PromptOverHelper(FakeHelper):
    """A permission prompt from another process sits in front of the app's window; the Dock's clear layer too."""

    def call(self, method, timeout=30.0, **p):
        if method == "screen.windows":
            return [{"pid": 500, "owner": "UserNotificationCenter", "regular": False, "alpha": 1, "layer": 9, "frame": [100, 100, 200, 150]},
                    {"pid": 501, "owner": "Dock", "regular": False, "alpha": 1, "layer": 20, "frame": [0, 0, 2000, 1200]},
                    {"pid": 7, "owner": "Finder", "regular": True, "alpha": 1, "layer": 0, "frame": [50, 50, 400, 400]},
                    {"pid": 42, "owner": "TextEdit", "regular": True, "alpha": 1, "layer": 0, "frame": [0, 0, 800, 600]},
                    {"pid": 600, "owner": "Somewhere", "regular": False, "alpha": 1, "layer": 9, "frame": [0, 0, 100, 100]}]
        if method == "ax.snapshot" and p.get("pid") == 500:
            return {"nodes": [{"ref": "x", "role": "AXWindow", "title": "Warning"}, {"ref": "y", "role": "AXStaticText", "value": "“TextEdit”would like to access“Document”files in the folder。"},
                              {"ref": "z", "role": "AXButton", "title": "Allow", "actions": ["AXPress"]}]}
        if method == "ax.snapshot" and p.get("pid") in (501, 600):
            return {"nodes": []}
        return super().call(method, timeout, **p)


def test_a_prompt_from_another_process_can_be_answered_but_granting_is_the_users_call(tmp_path):
    from macwork.observe import overlays
    obs = Observation(app=None, window=None, affordances=[])
    overlays(Ctx(cfg(tmp_path), PromptOverHelper(), app={"pid": 42, "name": "TextEdit"}), obs)
    assert obs.notes["covered_by"][0]["from"] == "UserNotificationCenter" and "would like to access" in obs.screen_text
    allow = next(a for a in obs.affordances if "Allow" in a.label)
    assert allow.target["pid"] == 500 and allow.target["watch"] == 42 and "from UserNotificationCenter" in allow.context and "in front of the app" in allow.context
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert eng._needs_confirm(allow, 0.0, set())
    assert not eng._needs_confirm(Affordance("c", "window", "press", "button 「Don't Allow」"), 0.0, set())
    assert not eng._needs_confirm(Affordance("c", "window", "press", "button 「Don’t Allow」"), 0.0, set())


def test_rows_are_offered_for_selecting_named_by_their_text(tmp_path):
    nodes = [{"ref": "t", "role": "AXTable", "title": "Applications"},
             {"ref": "r1", "role": "AXRow", "parent": "t", "frame": [0, 0, 300, 20]},
             {"ref": "c1", "role": "AXCell", "parent": "r1"}, {"ref": "s1", "role": "AXStaticText", "value": "Safari", "parent": "c1"},
             {"ref": "r2", "role": "AXRow", "parent": "t", "selected": True},
             {"ref": "s2", "role": "AXStaticText", "value": "Mail", "parent": "r2"}]
    obs = Observation(app=None, window=None, affordances=[])
    element_affordances(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42}), obs, nodes, "w")
    sel = [a for a in obs.affordances if a.verb == "select"]
    assert [a.label for a in sel] == ["select row 「Safari」", "select row 「Mail」 (selected)"]
    h = FakeHelper()
    from macwork.act import window_channel
    window_channel(Ctx(cfg(tmp_path), h, app={"pid": 42}), sel[0], {})
    assert h.did("ax.set")[0] == {"ref": "r1", "attribute": "AXSelected", "value": True}


def test_text_inside_a_row_names_it_and_is_not_offered_on_its_own(tmp_path):
    nodes = [{"ref": "t", "role": "AXTable"},
             {"ref": "r1", "role": "AXRow", "parent": "t"},
             {"ref": "f1", "role": "AXTextField", "value": "Safari", "parent": "r1", "editable": False, "actions": ["AXShowMenu"]},
             {"ref": "b1", "role": "AXButton", "title": "Details", "parent": "r1", "actions": ["AXPress"]}]
    obs = Observation(app=None, window=None, affordances=[])
    element_affordances(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42}), obs, nodes, "w")
    assert [a.label for a in obs.affordances] == ["select row 「Safari」", "button 「Details」"]


def test_eval_harness_statistics_substitution_and_checks(tmp_path):
    from macwork.evals import _sub, check, wilson
    lo, hi = wilson(9, 10)
    assert 0.59 < lo < 0.6 and 0.98 < hi <= 1.0 and wilson(0, 0) == (0.0, 0.0)
    task = {"goal": "open {eval_dir}/a.txt", "check": {"file_exists": ["{eval_dir}/a.txt"], "answer_contains": ["3"],
                                                        "trace_excludes": ["(?i)chess"]}}
    t = _sub(task, str(tmp_path))
    assert t["goal"] == f"open {tmp_path}/a.txt"
    (tmp_path / "a.txt").write_text("x")
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert check(eng, t, {"outputs": {"answer": "There are 3 files."}, "steps": ["menu File ▸ Open"]}) == (True, "checked")
    ok, why = check(eng, t, {"outputs": {"answer": "3"}, "steps": ["open app Chess"]})
    assert not ok and "trace_excludes" in why
