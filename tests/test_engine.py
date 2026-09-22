"""Engine, providers, privacy and protocol — offline: a scripted helper and a scripted decider, no Mac UI touched."""

import json

import anyio

from macwork.config import Config
from macwork.engine import Engine
from macwork.observe import Ctx, arrange, menu, window
from macwork.model import Affordance, Observation
from macwork.privacy import Audit, Gate, Redactor


def cfg(tmp_path, **over):
    base = {"config": {"audit": {"path": str(tmp_path / "audit.jsonl")}, "observe": {"providers": ["apps", "menu", "window", "keys"]},
                       "engine": {"verify": {"timeout_ms": 10, "settle_ms": 0, "ambient_probe_s": 0}, "tidy_background": False}, "planner": {"kind": "none"},
                       "appmodel": {"dir": str(tmp_path / "apps")}, "skills": {"dir": str(tmp_path / "skills")}}}
    for doc, v in over.items():
        for k, x in v.items():
            if isinstance(x, dict) and isinstance(base.setdefault(doc, {}).get(k), dict):
                base[doc][k] = {**base[doc][k], **x}
            else:
                base.setdefault(doc, {})[k] = x
    return Config.load(user_dir=tmp_path / "none", overrides=base)


MENUBAR = {"nodes": [
    {"ref": "g1.0", "role": "AXMenuBar", "depth": 0},
    {"ref": "g1.1", "role": "AXMenuBarItem", "title": "Apple", "parent": "g1.0"},
    {"ref": "g1.2", "role": "AXMenu", "parent": "g1.1"},
    {"ref": "g1.3", "role": "AXMenuItem", "title": "Recent Items", "parent": "g1.2"},
    {"ref": "g1.4", "role": "AXMenuBarItem", "title": "File", "parent": "g1.0"},
    {"ref": "g1.5", "role": "AXMenu", "parent": "g1.4"},
    {"ref": "g1.6", "role": "AXMenuItem", "title": "New Document", "cmd": {"char": "N", "mods": 0}, "parent": "g1.5"},
    {"ref": "g1.7", "role": "AXMenuItem", "title": "Delete Document", "parent": "g1.5"},
    {"ref": "g1.8", "role": "AXMenuItem", "title": "Export", "parent": "g1.5"},
    {"ref": "g1.9", "role": "AXMenu", "parent": "g1.8"},
    {"ref": "g1.10", "role": "AXMenuItem", "title": "PDF", "cmd": {"char": "P", "mods": 1}, "parent": "g1.9"},
    {"ref": "g1.11", "role": "AXMenuItem", "title": "Greyed Item", "enabled": False, "parent": "g1.5"},
], "ms": 3}

WINDOW = {"nodes": [
    {"ref": "g2.0", "role": "AXWindow", "title": "Untitled", "depth": 0},
    {"ref": "g2.1", "role": "AXGroup", "title": "Toolbar", "parent": "g2.0"},
    {"ref": "g2.2", "role": "AXButton", "rdesc": "button", "title": "Send", "actions": ["AXPress"], "parent": "g2.1"},
    {"ref": "g2.3", "role": "AXTextField", "rdesc": "text field", "placeholder": "Recipient", "parent": "g2.0"},
    {"ref": "g2.4", "role": "AXStaticText", "value": "Zhang Wei's email is zw@example.com", "parent": "g2.0"},
    {"ref": "g2.5", "role": "AXButton", "actions": ["AXPress"], "parent": "g2.0"},   # no label: not offered
], "ms": 4}


class FakeHelper:
    mode = "fake"

    def __init__(self, locked=False, sparse_first=False):
        self.calls = []
        self.locked = locked
        self.sparse_first = sparse_first
        self.values = {}
        self.typed = ""
        self.focus_pid = 42            # the cursor is in the app being worked in, unless a test moves it
        self.focus_app = "TextEdit"
        self.secure_input = False

    def background(self):          # the real helper's second connection: here, the same fake
        return self

    def call(self, method, timeout=30.0, **p):
        self.calls.append((method, p))
        app = {"pid": 42, "name": "TextEdit", "bundle_id": "com.apple.TextEdit", "path": "/System/Applications/TextEdit.app"}
        if method == "apps.running":
            return [app, {"pid": 7, "name": "Finder", "bundle_id": "com.apple.finder", "path": "/System/Library/CoreServices/Finder.app"}]
        if method == "apps.frontmost":
            return {"app": app}
        if method == "apps.installed":
            return [{"name": "Calculator", "file": "Calculator", "path": "/System/Applications/Calculator.app", "bundle_id": "com.apple.calculator"}]
        if method == "session.state":
            return {"screen_locked": self.locked}
        if method == "system.locale":
            return {"locale": "en_US", "languages": ["en-US"], "ocr_languages": ["en-US"], "region": "US"}
        if method == "screen.windows":
            return list(getattr(self, "screen", []))
        if method == "ping":
            return {"version": "fake", "ax_trusted": getattr(self, "ax_trusted", True), "screen_capture": True, "secure_input": self.secure_input}
        if method == "clipboard.read":
            return {"change_count": getattr(self, "clipboard_count", 0), "types": [], "chars": 0}
        if method == "clipboard.write":
            self.clipboard_count = getattr(self, "clipboard_count", 0) + (0 if getattr(self, "clipboard_stuck", False) else 1)
            return {"ok": True, "change_count": self.clipboard_count}
        if method == "ax.snapshot":
            if p.get("scope") == "menubar":
                return MENUBAR
            if self.sparse_first and not p.get("manual_accessibility"):
                return {"nodes": [WINDOW["nodes"][0]], "ms": 1}
            return WINDOW
        if method == "ax.set":
            if p.get("attribute", "AXValue") == "AXValue":
                self.values[p["ref"]] = p["value"]
            return {"ok": True}
        if method == "ax.get":
            return {"value": self.values.get(p["ref"], self.typed)}   # a field shows what was last typed into it
        if method == "ax.wait":
            return {"events": [{"name": "AXValueChanged", "ms": 5}], "timed_out": False}
        if method == "nl.entities":
            return [[{"type": "PERSON", "text": "Zhang Wei"}] if "Zhang Wei" in t else [] for t in p["texts"]]
        if method == "input.type":
            self.typed = p.get("text", "")
            return {"ok": True}
        if method in ("ax.perform", "apps.activate", "input.key", "ax.set_range", "input.drag", "input.click", "input.release_all"):
            return {"ok": True}
        if method == "input.idle":
            return {"idle_s": 99}
        if method == "ax.fingerprint":
            return {"fingerprint": 1}
        if method == "apps.openers":       # what the system would open this file with
            return list(getattr(self, "openers", []))
        if method == "ax.focused":
            # the cursor sits in this app's document, holding whatever was last typed at it
            return {"focused": {"role": "AXTextArea", "rdesc": "text", "ref": "focus", "value": self.typed},
                    "pid": self.focus_pid, "app": self.focus_app, "secure_input": self.secure_input}
        raise AssertionError(f"unexpected helper call {method}")

    def did(self, method):
        return [p for m, p in self.calls if m == method]


def worded(q: dict) -> str:
    """The instructions as the decider really receives them.

    A question that names a live UI string hands it over in `fills` rather than interpolating it, so that
    `Gate.decide` can redact the value without redacting our own wording. A test double that reads
    `instructions` straight off the question sees `{action}` and nothing else — which is how one of these
    fakes came to classify 「Alles Löschen」 as navigation and let it run.
    """
    text = str(q.get("instructions", ""))
    for name, value in (q.get("fills") or {}).items():
        text = text.replace("{" + name + "}", str(value))
    return text


class ScriptedDecider:
    """Answers each request from a script: ``pick`` (an option key or a substring of its text), ``move`` (act by
    default; done when the pick is done), plus optional noul answers by question name."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.side = []                     # requests without an action question (verification, back-out, diagnosis…)
        self.calls, self.cost_usd, self.last_ms = 0, 0.0, 1.0

    verify = 1.0     # answer to the stricter "is it really done?" check
    backs_out = 0.0
    calls_for = 1.0
    serves = 1.0
    stands = 1.0     # answer judged to follow from what was seen, when it is not traceable word for word
    about = 0.0      # is the goal about a prompt that turned up? By default it merely turned up
    what: dict = {}
    what_when: str = ""      # apply `what` only where this appears in the question (i.e. to one action)
    why = "failed"

    def _classify(self, q):
        """How the fake answers a floor classification.

        Every action is classified now, not only the ones whose words hit the floor, so the fake has to answer
        the two shapes of question the way a real classifier would: a narrow question (only the categories the
        words suggested) means the words flagged this action; the whole floor means they said nothing about it,
        and an ordinary action is navigation — or, where text is being entered, entering text."""
        crit = q["criteria"]
        if self.what and (not self.what_when or self.what_when in q.get("instructions", "")):
            return {"type": "choice", "choice": max(self.what, key=self.what.get), "probabilities": dict(self.what)}
        released = tuple(Config.load().policy["confirm"]["release"])   # policy says which verdicts let an action through
        if len([c for c in crit if c not in released]) > 4:      # the whole floor was offered
            pick = "enter" if "enter" in crit else "navigate"
        else:
            pick = "enter" if "enter" in crit else next(c for c in crit if c not in released)
        return {"type": "choice", "choice": pick, "probabilities": {pick: 1.0}}

    def decide(self, state, questions):
        self.calls += 1
        self.cost_usd += 0.0001
        if "action" not in questions:
            self.side.append((state, questions))
            if any(k.startswith("floor") for k in questions):
                return {k: self._classify(q) for k, q in questions.items()}
            out = {}
            for k, q in questions.items():
                if k == "what" or k.startswith("floor"):   # the floor's classification of one action
                    out[k] = self._classify(q)
                elif q["type"] == "choice":
                    out[k] = {"type": "choice", "choice": self.why}
                else:
                    out[k] = {"type": "noul", "noul": self.backs_out if k == "backs_out" else self.verify if k == "verified"
                              else self.calls_for if k == "calls_for" else self.serves if k == "serves"
                              else self.stands if k == "stands" else self.about if k == "about" else 1.0}
            return out
        self.seen.append((state, questions))
        step = self.script.pop(0)
        if callable(step):
            step = step(state, questions)
        opts = questions["action"]["criteria"]
        pick = step["pick"]
        key = pick if pick in opts or pick == "done" else next(k for k, v in opts.items() if pick in v)
        out = {"action": {"type": "choice", "choice": key, "confidence": step.get("conf", 0.9),
                          "probabilities": step.get("probs", {key: step.get("conf", 0.9)})},
               "move": {"type": "choice", "choice": step.get("move", "done" if pick == "done" else "act")}}
        for k in ("risky_screen", "progress", "step_done", "wants_answer"):
            if k in questions:
                out[k] = {"type": "noul", "noul": step.get(k, 1.0 if k == "progress" else 0.0)}
        if "verified" in questions:              # the stricter done check travels in the same request
            out["verified"] = {"type": "noul", "noul": step.get("verified", self.verify)}
        for k, q in questions.items():           # classifications that ride along with the step's request
            if k.startswith("floor"):
                out[k] = self._classify(q["criteria"])
        return out


def engine(tmp_path, script, **helper_kw):
    return Engine(cfg(tmp_path), helper=FakeHelper(**helper_kw), decider=ScriptedDecider(script))


# ----------------------------------------------------------------- config
def test_config_layers_and_env(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.yaml").write_text("engine:\n  max_steps: 5\n")
    monkeypatch.setenv("MACWORK__ENGINE__TOP_K", "7")
    c = Config.load(user_dir=user)
    assert c.get("engine.max_steps") == 5 and c.get("engine.top_k") == 7
    assert c.get("engine.thresholds.done") == 0.8           # untouched defaults survive a partial override
    assert "Mac" in c.question("action")


# ----------------------------------------------------------------- privacy
def names_in(texts):
    """Fake tagger: finds Zhang Wei, and (like the real one) mistakes the app name Safari for an organisation."""
    return [[e for e in ({"type": "PERSON", "text": "Zhang Wei"}, {"type": "ORG", "text": "Safari"}) if e["text"] in t] for t in texts]


def test_redactor_tags_clause_by_clause_protects_app_names_and_user_names(tmp_path):
    c = cfg(tmp_path, privacy={"redact": {"enabled": True, "entities": ["PERSON", "ORG"], "names": ["Lao Wang"],
                                          "patterns": {"EMAIL": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"}, "replace": {}}})
    seen = []
    r = Redactor(c, entities=lambda ts: (seen.extend(ts), names_in(ts))[1], protect=lambda: ["Safari Browser", "Safari"])
    out = r.text("email Zhang Wei at zw@example.com, then open Safari, and tell Lao Wang")
    assert out == "email ⟦PERSON_2⟧ at ⟦EMAIL_1⟧, then open Safari, and tell ⟦PERSON_1⟧"
    assert "email Zhang Wei at" in seen and not any("@" in s for s in seen)   # clauses, emails removed before tagging


def test_redactor_pseudonyms_are_stable_and_local(tmp_path):
    r = Redactor(cfg(tmp_path), entities=names_in)
    out = r.value({"a": "Zhang Wei zw@example.com 13812345678 /Users/alice/Docs", "b": ["email Zhang Wei"]})
    assert out["a"] == "⟦PERSON_1⟧ ⟦EMAIL_1⟧ ⟦PHONE_1⟧ ~/Docs"
    assert out["b"] == ["email ⟦PERSON_1⟧"]
    assert r.table["Zhang Wei"] == "⟦PERSON_1⟧"


def test_redactor_withholds_when_tagging_fails_and_on_never_send(tmp_path):
    c = cfg(tmp_path, privacy={"redact": {"enabled": True, "entities": ["PERSON"], "never_send": ["confidential"], "patterns": {}, "replace": {}}})
    assert Redactor(c, entities=lambda ts: 1 / 0).text("Hello Alice") == "[withheld]"
    assert Redactor(c, entities=lambda ts: [[] for _ in ts]).text("this is a confidential file") == "[withheld]"


def test_gate_redacts_criteria_keeps_keys_and_audits(tmp_path):
    c = cfg(tmp_path)
    d = ScriptedDecider([{"pick": "x1"}])
    gate = Gate(d, Audit(c))
    r = Redactor(c, entities=names_in)
    gate.decide(r, {"goal": "call Zhang Wei now"}, {"action": {"type": "choice", "instructions": "?", "criteria": {"x1": "call Zhang Wei", "done": "stop"}}})
    state, qs = d.seen[0]
    assert state["goal"] == "call ⟦PERSON_1⟧ now" and qs["action"]["criteria"] == {"x1": "call ⟦PERSON_1⟧", "done": "stop"}
    log = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert log[0]["kind"] == "decide" and "Zhang Wei" not in json.dumps(log, ensure_ascii=False)


# ----------------------------------------------------------------- providers
def test_menu_provider_paths_shortcuts_and_skips(tmp_path):
    obs = Observation(app=None, window=None, affordances=[])
    menu(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "TextEdit"}), obs)
    labels = [a.label for a in obs.affordances]
    assert "menu File ▸ New Document (⌘N)" in labels and "menu File ▸ Export ▸ PDF (⇧⌘P)" in labels
    # the Apple menu is offered: removing it by position was hand-written knowledge of where macOS puts
    # things, and it took "About This Mac", "System Settings…" and "Recent Items" with it
    assert any("Recent" in lab for lab in labels)
    skipped = Observation(app=None, window=None, affordances=[])
    menu(Ctx(cfg(tmp_path, config={"observe": {"menu": {"skip_first_menu": True}}}), FakeHelper(),
             app={"pid": 42, "name": "TextEdit"}), skipped)
    assert not any("Recent" in a.label for a in skipped.affordances)
    assert not any("Greyed Item" in lab for lab in labels)            # disabled items skipped


def test_window_provider_fields_buttons_text_and_manual_accessibility(tmp_path):
    h = FakeHelper(sparse_first=True)
    obs = Observation(app=None, window=None, affordances=[])
    window(Ctx(cfg(tmp_path), h, app={"pid": 42, "name": "TextEdit"}), obs)
    by_verb = {a.verb: a for a in obs.affordances}
    assert by_verb["press"].label == "button 「Send」" and by_verb["press"].context == "Toolbar"
    assert by_verb["type"].slots["text"].kind == "text" and "Recipient" in by_verb["type"].label
    assert "and press Return" in by_verb["type_submit"].label                      # single-line field: submit variant too
    assert len(obs.affordances) == 3 and "Zhang Wei" in obs.screen_text and obs.window == "Untitled"
    assert obs.notes.get("manual_accessibility") and any(p.get("manual_accessibility") for p in h.did("ax.snapshot"))


def test_options_that_do_not_fit_are_folded_by_where_they_live_not_by_guessed_relevance():
    affs = [Affordance(f"m{i}", "menu", "press", f"menu File ▸ item {i}", context="File") for i in range(30)] + \
           [Affordance(f"w{i}", "window", "press", f"button 「b{i}」", context="Toolbar") for i in range(5)] + \
           [Affordance(f"i{i}", "app", "open", f"open app A{i}") for i in range(40)]
    flat, folded = arrange(affs, 40, set())
    assert {a.id for a in flat} == {a.id for a in affs[30:35]} | {a.id for a in affs[:30]}   # smallest groups first, whole
    assert list(folded) == ["app:open"] and folded["app:open"][0].startswith("look into open another app (40 options")
    flat, folded = arrange(affs, 45, {"app:open"})                  # a group the decider opened is shown in full
    assert "app:open" not in folded and sum(a.channel == "app" for a in flat) == 40
    assert arrange(affs[:10], 40, set()) == (affs[:10], {})
    flat, folded = arrange(affs, 500, set(), fold_over=35)               # a very large group is one level down anyway
    assert list(folded) == ["app:open"] and len(flat) == 35


def test_menu_items_keep_their_own_shortcut_as_a_key_combo(tmp_path):
    obs = Observation(app=None, window=None, affordances=[])
    menu(Ctx(cfg(tmp_path), FakeHelper(), app={"pid": 42, "name": "TextEdit"}), obs)
    combos = {a.label: a.target.get("combo") for a in obs.affordances}
    assert combos["menu File ▸ New Document (⌘N)"] == "cmd+n" and combos["menu File ▸ Export ▸ PDF (⇧⌘P)"] == "cmd+shift+p"
    assert combos["menu File ▸ Delete Document"] is None


# ----------------------------------------------------------------- goal-level protocol
def test_do_happy_path_runs_menu_then_done(tmp_path):
    eng = engine(tmp_path, [{"pick": "New Document"}, {"pick": "done", "done": 0.95}])
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    assert eng.helper.did("ax.perform")[0]["ref"] == "g1.6"
    # Every action on offer is classified in one batch that rides along with the step's own request. The step
    # does not wait that batch out, so whether its answer is back before the action is chosen depends on
    # scheduling — which makes an exact request count a test of the scheduler. What has to hold is that the
    # chosen action was in the batch, and that it is classified at all: a word list that misses must never
    # read as "safe", which is what makes the floor hold in a language the words never saw.
    assert res["decider"]["calls"] <= 5
    floor = [q for _, q in eng.decider.side if any(k.startswith("floor") for k in q)]
    assert floor and any("New Document" in worded(q) for q in floor[0].values())
    second_state = eng.decider.seen[1][0]
    assert "AXValueChanged" in second_state["last_action"]           # the decider sees what the UI did


def test_do_needs_input_then_types_it(tmp_path):
    eng = engine(tmp_path, [{"pick": "Recipient"}, {"pick": "done"}])
    res = eng.do("fill in the recipient")
    assert res["status"] == "need_input" and "text" in res["pending"]["inputs"]
    res = eng.resume(res["task_id"], inputs={"text": "someone@example.com"})
    assert res["status"] == "done"
    assert eng.helper.did("input.type")[0]["text"] == "someone@example.com"
    assert eng.helper.did("input.key")[0]["combo"] == "cmd+a"                     # what was in the field is replaced


def test_do_risky_needs_confirm_decline_and_accept(tmp_path):
    eng = engine(tmp_path, [{"pick": "Delete Document"}])
    res = eng.do("delete this document")
    assert res["status"] == "need_confirm" and "Delete" in res["pending"]["confirm"]["label"]
    assert eng.resume(res["task_id"], confirm=False)["status"] == "cancelled"
    assert not eng.helper.did("ax.perform")
    eng = engine(tmp_path, [{"pick": "Delete Document"}, {"pick": "done"}])
    res = eng.do("delete this document")
    assert eng.resume(res["task_id"], confirm=True)["status"] == "done"
    assert eng.helper.did("ax.perform")[0]["ref"] == "g1.7"


def test_do_ambiguous_returns_choices_and_resumes_with_one(tmp_path):
    probs = lambda q: {k: (0.3 if "PDF" in v else 0.25 if "New" in v else 0.0) for k, v in q["action"]["criteria"].items()}  # noqa: E731
    eng = engine(tmp_path, [lambda s, q: {"pick": "PDF", "move": "ask_user", "probs": probs(q)}, {"pick": "done"}])
    res = eng.do("Export")
    assert res["status"] == "ambiguous"
    ids = [o["id"] for o in res["pending"]["choose_one_of"]]
    assert len(ids) >= 2
    # exporting is a floor category, and this action has never been classified — the engine returned
    # `ambiguous` before it got that far. Picking an option says which one, not that it is safe.
    asked = eng.resume(res["task_id"], choice_id=ids[0])
    assert asked["status"] == "need_confirm"
    res = eng.resume(res["task_id"], confirm=True)
    assert res["status"] == "done" and "PDF" in res["steps"][0]


def test_a_caller_can_answer_which_one_and_whether_in_one_call(tmp_path):
    probs = lambda q: {k: (0.3 if "PDF" in v else 0.25 if "New" in v else 0.0) for k, v in q["action"]["criteria"].items()}  # noqa: E731
    eng = engine(tmp_path, [lambda s, q: {"pick": "PDF", "move": "ask_user", "probs": probs(q)}, {"pick": "done"}])
    res = eng.do("Export")
    ids = [o["id"] for o in res["pending"]["choose_one_of"]]
    res = eng.resume(res["task_id"], choice_id=ids[0], confirm=True)
    assert res["status"] == "done" and "PDF" in res["steps"][0]


def test_do_locked_screen_fails_fast(tmp_path):
    eng = engine(tmp_path, [], locked=True)
    res = eng.do("make a new document")
    assert res["status"] == "failed" and "locked" in res["reason"] and not eng.decider.seen


def test_policy_deny_pattern_hides_affordances(tmp_path):
    c = cfg(tmp_path, policy={"deny": {"patterns": ["Delete"], "bundle_ids": []}})
    eng = Engine(c, helper=FakeHelper(), decider=ScriptedDecider([]))
    labels = [a["label"] for a in eng.observe(limit=500)["affordances"]]
    assert not any("Delete" in lab for lab in labels) and any("New Document" in lab for lab in labels)


def test_step_level_observe_act_needs_confirm_and_input(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    obs = eng.observe(goal="Delete", limit=500)
    delete = next(a for a in obs["affordances"] if "Delete Document" in a["label"])
    assert eng.act(delete["id"])["needs_confirm"]
    field = next(a for a in obs["affordances"] if a["verb"] == "type")
    assert eng.act(field["id"])["needs_input"] == ["text"]
    assert eng.act(delete["id"], confirm=True)["ok"]


# ----------------------------------------------------------------- MCP surface
def test_mcp_server_exposes_both_levels_of_control(tmp_path):
    from macwork.server import build

    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    mcp = build(cfg(tmp_path), eng)
    tools = {t.name for t in anyio.run(mcp.list_tools)}
    assert {"mac_do", "mac_resume", "mac_cancel", "mac_feedback", "mac_observe", "mac_act", "mac_status"} <= tools
    # looking something up is a goal like any other, driven by the same loop through the Mac's own browser
    assert "web_research" not in tools


def test_backing_out_of_a_risky_screen_is_judged_by_the_decider_and_paste_always_asks(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    later = Affordance("x", "window", "press", "button 「later」")
    paste = Affordance("y", "menu", "press", "menu Edit ▸ Paste (⌘V)")
    assert not eng._needs_confirm(later, risky_screen=0.95, approved=set(), harmless=lambda a: True)
    assert eng._needs_confirm(later, risky_screen=0.95, approved=set(), harmless=lambda a: False)
    assert eng._needs_confirm(paste, 0.0, set(), harmless=lambda a: True)       # the safety floor is not negotiable
    assert not eng._needs_confirm(later, 0.1, set())                            # a calm screen: nothing to ask


def test_on_a_risky_screen_the_decider_is_asked_about_the_chosen_action(tmp_path):
    d = ScriptedDecider([{"pick": "New Document", "risky_screen": 0.9}, {"pick": "done"}])
    d.backs_out = 0.95
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    assert eng.do("make a new document")["status"] == "done"
    asked = [q["backs_out"]["instructions"] for _s, q in d.side if "backs_out" in q]
    assert asked and "New Document" in asked[0]
    d2 = ScriptedDecider([{"pick": "New Document", "risky_screen": 0.9}])
    assert Engine(cfg(tmp_path), helper=FakeHelper(), decider=d2).do("make a new document")["status"] == "need_confirm"


class StuckInBackHelper(FakeHelper):
    """An app that refuses to come to the front: typing must be refused, not sent to whatever is in front."""

    def call(self, method, timeout=30.0, **p):
        if method == "apps.frontmost":
            self.calls.append((method, p))
            return {"app": {"pid": 999, "name": "Terminal", "bundle_id": "com.apple.Terminal"}}
        return super().call(method, timeout, **p)


def test_never_types_into_an_app_that_is_not_in_front(tmp_path, monkeypatch):
    import macwork.act as act
    monkeypatch.setattr(act.subprocess, "run", lambda *a, **k: None)
    c = cfg(tmp_path, config={"engine": {"activate_wait_s": 0.05}})
    eng = Engine(c, helper=StuckInBackHelper(), decider=ScriptedDecider([{"pick": "pagedown"}, {"pick": "done"}]))
    eng.cfg.docs["config"]["observe"]["providers"] = ["keys"]
    res = eng.do("Page Down", app="TextEdit")
    assert not eng.helper.did("input.key") and not eng.helper.did("input.type")
    assert "nothing was typed" in (eng.tasks[res["task_id"]].steps[0].error or "")


def test_a_doubted_done_is_decided_again_without_done_on_the_table(tmp_path):
    d = ScriptedDecider([{"pick": "done", "verified": 0.1}, {"pick": "New Document"}, {"pick": "done", "verified": 0.9}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and eng.helper.did("ax.perform")[0]["ref"] == "g1.6"
    second = d.seen[1]
    assert "done" not in second[1]["action"]["criteria"] and second[0]["not_done_yet"]    # re-asked, not a runner-up


def test_waiting_is_a_move_the_decider_can_make(tmp_path):
    d = ScriptedDecider([{"pick": "New Document", "move": "wait"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"engine": {"wait_s": 0.01}}), helper=FakeHelper(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and len(res["steps"]) == 1 and len(d.seen) == 3


def test_eval_setup_never_sends_keys_when_the_app_is_not_in_front(tmp_path, monkeypatch):
    import macwork.act as act
    from macwork.evals import cleanup
    monkeypatch.setattr(act.subprocess, "run", lambda *a, **k: None)
    eng = Engine(cfg(tmp_path, config={"engine": {"activate_wait_s": 0.05}}), helper=StuckInBackHelper(), decider=ScriptedDecider([]))
    cleanup(eng, {"app": "TextEdit", "setup": [{"activate": True, "key": "cmd+q"}]}, "setup")
    assert not eng.helper.did("input.key")          # the terminal in front must never receive cmd+q


def test_saving_replacing_and_pointer_clicks_on_risky_screens_ask_first(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    for label in ("menu File ▸ Save… (⌘S)", "button 「Replace」", "button 「Replace」", "menu File ▸ Export…"):
        assert eng._needs_confirm(Affordance("x", "window", "press", label), 0.0, set()), label
    click = Affordance("o1", "pointer", "click", "click 「OK」 in scroll area")
    assert eng._needs_confirm(click, risky_screen=0.94, approved=set())     # vision clicks obey a risky screen too


def test_no_channel_is_exempt_from_a_risky_screen_unless_one_is_named(tmp_path):
    """The default used to exempt `web`, and that channel no longer exists. Nothing is exempt now, and the
    setting is what grants it — an exemption written into the code is one nobody can see."""
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    a = Affordance("s0", "shortcut", "run", "run shortcut 「x」")
    assert eng._needs_confirm(a, risky_screen=0.94, approved=set())
    lenient = Engine(cfg(tmp_path, policy={"confirm": {"screen_gate_exempt": ["shortcut"]}}),
                     helper=FakeHelper(), decider=ScriptedDecider([]))
    assert not lenient._needs_confirm(a, risky_screen=0.94, approved=set())


def test_planner_suggestions_are_offered_to_the_decider_not_forced(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New Document", "move": "rethink"}, {"pick": "cmd+shift+x"}, {"pick": "settings"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["open settings with the command palette"], "inputs": {},
                                 "try": [{"keys": "cmd+shift+x"}, {"type": "settings"}, {"action": "menu File ▸ New Document (⌘N)"}, {"keys": "rm -rf /"}]}])
    res = eng.do("open settings")
    opts = d.seen[1][1]["action"]["criteria"]
    assert any(v.startswith("press cmd+shift+x (suggested") for v in opts.values())
    assert not any("rm -rf" in v for v in opts.values())                                # an invalid key string is dropped
    assert any("New Document" in v and v.endswith("suggested by the planner") for v in opts.values())
    assert d.seen[1][0]["planner_suggests"][0] == "press cmd+shift+x"
    assert [p["combo"] for p in eng.helper.did("input.key")] == ["cmd+shift+x"]
    assert eng.helper.did("input.type")[0]["text"] == "settings" and res["status"] == "done"
    third = d.seen[2][1]["action"]["criteria"]
    assert not any(v.startswith("press cmd+shift+x") for v in third.values())             # used: no longer suggested


def test_blocked_gets_a_second_opinion_from_the_planner(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New Document", "move": "blocked"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["open the File menu"], "inputs": {}, "try": [{"action": "menu File ▸ New Document (⌘N)"}]}])
    assert eng.do("make a new document")["status"] == "done"          # the planner saw another way in: keep going
    d = ScriptedDecider([{"pick": "New Document", "move": "blocked"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "try": [], "blocked": "Sign in to the app first"}])
    res = eng.do("open settings")
    assert res["status"] == "blocked" and res["reason"] == "Sign in to the app first"
    d = ScriptedDecider([{"pick": "New Document", "move": "blocked"}])            # no planner: the decider's judgement stands
    res = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d).do("open settings")
    assert res["status"] == "blocked" and "user" in res["reason"]


def test_actions_without_effect_are_not_offered_again_from_the_same_screen(tmp_path):
    class StillHelper(FakeHelper):     # nothing ever changes on screen
        def call(self, method, timeout=30.0, **p):
            if method == "ax.wait":
                return {"events": [], "timed_out": True}
            return super().call(method, timeout, **p)
    d = ScriptedDecider([{"pick": "New Document"}, {"pick": "Send"}, {"pick": "Export", "move": "impossible"}])
    eng = Engine(cfg(tmp_path, policy={"confirm": {"mode": "never"}}), helper=StillHelper(), decider=d)
    res = eng.do("open settings")
    second, third = d.seen[1], d.seen[2]
    assert not any("New Document" in v for v in second[1]["action"]["criteria"].values())      # did nothing: not offered again
    assert "menu File ▸ New Document (⌘N)" in third[0]["tried_here_without_effect"]
    assert res["status"] == "failed" and "unreachable" in res["reason"]


def test_a_menu_item_the_app_ignores_is_retried_with_its_own_shortcut(tmp_path):
    class IgnoresPress(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.wait":
                self.calls.append((method, p))
                return {"events": [{"name": "AXWindowCreated"}] if self.did("input.key") else [], "timed_out": False}
            return super().call(method, timeout, **p)
    eng = Engine(cfg(tmp_path), helper=IgnoresPress(), decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "done"}]))
    res = eng.do("make a new document")
    assert [p["combo"] for p in eng.helper.did("input.key")] == ["cmd+n"] and res["status"] == "done"
    assert eng.tasks[res["task_id"]].steps[0].events[0] == "(by its shortcut)"


def test_signing_in_always_needs_the_user(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    for label in ("button 「Log In」", "click 「Sign Up」 in group", "button 「Log In」", "link 「Sign in with Google」"):
        kind = "pointer" if label.startswith("click") else "window"
        assert eng._needs_confirm(Affordance("x", kind, "press", label), 0.0, set()), label


def test_out_of_budget_the_decider_says_why_and_the_planner_words_it(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New Document", "progress": 0.1}] * 6)
    d.why = "blocked"
    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 2}}), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": [], "inputs": {}, "try": [], "blocked": "Sign in to the app first"}])
    res = eng.do("open settings")
    assert res["status"] == "blocked" and len(res["steps"]) == 2 and res["reason"].endswith("Sign in to the app first")
    # still getting somewhere when this run's steps ran out: handed back to be continued, not failed
    d = ScriptedDecider([{"pick": "New Document"}] * 12)
    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 2}}), helper=FakeHelper(), decider=d)
    res = eng.do("open settings")
    assert res["status"] == "need_continue" and res["pending"]["steps_taken"] == 2

    # …and it stops for good at the whole-task ceiling, however many turns it is given
    d2 = ScriptedDecider([{"pick": "New Document"}] * 12)
    eng2 = Engine(cfg(tmp_path, config={"engine": {"max_steps": 2, "total_steps": 4}}), helper=FakeHelper(), decider=d2)
    res2 = eng2.do("open settings")
    while res2["status"] == "need_continue":
        res2 = eng2.resume(res2["task_id"])
    assert res2["status"] in ("failed", "blocked") and len(res2["steps"]) == 4


def test_the_decider_can_open_a_folded_group_before_acting(tmp_path):
    c = cfg(tmp_path, config={"engine": {"max_options": 8}})
    d = ScriptedDecider([lambda s, q: {"pick": next(k for k, v in q["action"]["criteria"].items() if "「File」" in v)},
                         {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(c, helper=FakeHelper(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    assert not any("New Document" in v for k, v in d.seen[0][1]["action"]["criteria"].items() if not k.startswith("g"))   # folded at first


def test_apps_are_found_however_the_user_spells_them(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    running = [{"pid": 5, "name": "AudioMIDISettings", "bundle_id": "com.apple.audio.AudioMIDISetup"},
               {"pid": 6, "name": "Safari", "bundle_id": "com.apple.Safari"}]
    assert eng._resolve_app("Audio MIDI Settings", running)["pid"] == 5                    # spacing differs
    eng.cache["installed"] = [{"name": "Safari Browser", "file": "Safari", "path": "/Applications/Safari.app", "bundle_id": "com.apple.Safari"}]
    assert eng._resolve_app("Safari Browser", running)["pid"] == 6                        # the name it is displayed under vs the name it runs under


def test_the_app_the_caller_named_is_opened_first(tmp_path, monkeypatch):
    import macwork.act as act
    ran = []

    class R:
        returncode, stderr = 0, ""
    monkeypatch.setattr(act.subprocess, "run", lambda args, **k: (ran.append(args), R())[1])

    class Launches(FakeHelper):   # the app shows up once it was opened
        def call(self, method, timeout=30.0, **p):
            if method == "apps.running" and ran:
                return super().call(method, timeout, **p) + [{"pid": 9, "name": "Calculator", "bundle_id": "com.apple.calculator",
                                                               "path": "/System/Applications/Calculator.app"}]
            return super().call(method, timeout, **p)
    eng = Engine(cfg(tmp_path), helper=Launches(), decider=ScriptedDecider([{"pick": "done"}]))
    res = eng.do("use the calculator to work out 1+1", app="Calculator")
    assert ran and ran[0][:2] == ["open", "-a"] and ran[0][2].endswith("Calculator.app")
    assert res["steps"][0].startswith("open app Calculator")


def test_a_planner_suggestion_inside_a_folded_group_is_shown(tmp_path):
    from tests.test_flex import FakeBackend
    c = cfg(tmp_path, config={"engine": {"max_options": 8}})
    d = ScriptedDecider([{"pick": "Send", "move": "rethink"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(c, helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"steps": ["x"], "inputs": {}, "try": [{"action": "menu File ▸ New Document (⌘N)"}]}])
    assert eng.do("make a new document")["status"] == "done"
    first = d.seen[0][1]["action"]["criteria"].values()
    # only named inside the fold's own summary ("look into the 「File」 menu (3 options: New Document…)"),
    # never as an option of its own
    assert not any(v.startswith("menu File ▸ New Document") for v in first)
    assert any(v.startswith("look into") and "New Document" in v for v in first)
    offered = d.seen[1][1]["action"]["criteria"].values()
    assert "menu File ▸ New Document (⌘N) — suggested by the planner" in offered


def test_what_was_looked_up_becomes_a_fact_the_answer_can_be_written_from(tmp_path):
    """What the web subsystem was *for*, done the ordinary way: read a window in full, and what it said is
    a fact — so the answer comes from what was actually read, not from the planner's memory."""
    from tests.test_flex import FakeBackend

    class Article(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("visible_only") is False:
                return {"nodes": [{"ref": "w.0", "role": "AXWindow", "depth": 0},
                                  {"ref": "w.1", "role": "AXStaticText", "parent": "w.0",
                                   "value": "Python 3.14 was released on 7 October 2025."}]}
            return super().call(method, timeout, **p)

    # the goal asks for something to be reported back, so the answer is written at the end
    d = ScriptedDecider([{"pick": "read all the text", "wants_answer": 1.0}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["readall"], "readall": {"offer_over": 0}}}),
                 helper=Article(), decider=d)
    eng._planner = FakeBackend([{"answer": "7 October 2025"}])

    res = eng.do("Python 3.14 which day was it released")
    assert res["status"] == "done"
    assert res["outputs"]["answer"] == "7 October 2025"
    assert "answer_refused" not in res["outputs"], "it was read, so it may be written"


def _counting_engine(tmp_path, answer):
    """A task that reads a window listing three files and then answers a question about them."""
    from tests.test_flex import FakeBackend

    class Listing(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("visible_only") is False:
                return {"nodes": [{"ref": "w.0", "role": "AXWindow", "depth": 0},
                                  {"ref": "w.1", "role": "AXStaticText", "parent": "w.0",
                                   "value": "one.txt\ntwo.txt\nthree.txt"}]}
            return super().call(method, timeout, **p)

    d = ScriptedDecider([{"pick": "read all the text", "wants_answer": 1.0}, {"pick": "done", "done": 0.95}])
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["readall"], "readall": {"offer_over": 0}}}),
                 helper=Listing(), decider=d)
    eng._planner = FakeBackend([{"answer": answer}])
    return eng, d


def test_an_answer_that_counts_what_it_saw_is_reported(tmp_path):
    """The word-for-word check cannot pass a sentence: "there are three files" is prose around a count, and
    prose is on no screen. A real run answered this correctly and had the answer thrown away — so an answer
    it cannot trace is judged against what was seen instead of being dropped."""
    eng, d = _counting_engine(tmp_path, "The folder holds three files: one.txt, two.txt and three.txt.")
    res = eng.do("how many files are in this folder？")
    assert res["outputs"]["answer"].startswith("The folder holds three files")
    assert "answer_refused" not in res["outputs"]
    assert any("stands" in q for _, q in d.side), "the untraceable answer went out unjudged"


def test_an_answer_holding_a_value_no_screen_showed_is_still_refused(tmp_path):
    """The guard that matters: judging the answer must not turn into accepting whatever the planner writes."""
    eng, d = _counting_engine(tmp_path, "The folder holds three files，in total 4096 bytes。")
    d.stands = 0.0
    res = eng.do("how many files are in this folder？")
    assert "answer" not in res["outputs"] and res["outputs"]["answer_refused"].startswith("The folder holds three files")


def test_the_same_action_over_and_over_is_noticed_and_then_stopped(tmp_path):
    """A real run scrolled one Finder list eleven times and called it progress every time: each scroll showed a
    new screen (so the circle check saw nothing) and the decider kept scoring it well (so the no-effect check
    saw nothing either). How many times running an action has been taken is a plain fact, so it is stated —
    and past a ceiling the action stops being offered, whatever it happens to be."""
    def again(state, questions):   # keep choosing the same thing for as long as it is on offer
        offered = any("press the pagedown key" in v for v in questions["action"]["criteria"].values())
        return {"pick": "press the pagedown key" if offered else "done", "conf": 0.6, "move": "act" if offered else "done"}

    picks = [again for _ in range(12)]
    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 12}}), helper=FakeHelper(),
                 decider=ScriptedDecider(picks))
    eng.do("send this email")
    states = [st for st, q in eng.decider.seen if "action" in q]
    noticed = next((i for i, st in enumerate(states) if "done_over_and_over" in st), None)
    assert noticed is not None and noticed <= 4, "repeating one action went unmentioned"
    assert len(states) <= 7, "the ceiling did not arrive: the same screen kept offering the same action"
    offered = [any("press the pagedown key" in v for v in q["action"]["criteria"].values()) for _, q in eng.decider.seen if "action" in q]
    assert offered[0] and not offered[-1], "the action was still on offer after its ceiling"


def test_a_goal_that_asks_for_information_does_not_finish_without_any(tmp_path):
    """A real run opened a page and called itself done one step later — before the page was on screen — and
    handed back "the title could not be found". Asking for something to be reported is not accomplished until
    there is something to report, so it looks again."""
    from tests.test_flex import FakeBackend

    class Page(FakeHelper):
        def __init__(self):
            super().__init__()
            self.opened = False

        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("visible_only") is False:
                text = "Morning News" if self.opened else ""
                self.opened = True                       # the page is only there from the second look on
                return {"nodes": [{"ref": "w.0", "role": "AXWindow", "depth": 0},
                                  {"ref": "w.1", "role": "AXStaticText", "parent": "w.0", "value": text}]}
            return super().call(method, timeout, **p)

    def step(state, questions):    # read the window when that is on offer, otherwise say it is done
        reading = next((k for k, v in questions["action"]["criteria"].items() if "read all the text" in v), None)
        return {"pick": reading or "done", "wants_answer": 1.0, "done": 0.95,
                "move": "act" if reading else "done"}

    d = ScriptedDecider([step] * 6)
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["readall"], "readall": {"offer_over": 0}}}),
                 helper=Page(), decider=d)
    eng._planner = FakeBackend([{"answer": ""}, {"answer": "Morning News"}])

    res = eng.do("what is the title of this page")
    assert res["outputs"]["answer"] == "Morning News"
    asked = [q for _, q in d.seen if "action" in q]
    assert len(asked) > 2, "it stopped at the first 'done', with nothing to report"


def test_an_action_that_keeps_leading_back_where_it_was_is_dropped(tmp_path):
    """The shape a stuck task really has is not "again" but "again, and back where I was". A real run opened
    the same 「File ▸ Open…」 six times, escaping each time, and the per-screen count started over every time
    because each escape left a screen just different enough. The circle is what counts, wherever it happens."""
    class TwoScreens(FakeHelper):
        """A dialog that opens and closes: the window alternates, so no screen repeats twice running."""
        def __init__(self):
            super().__init__()
            self.open = False

        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") not in ("menubar", "windows"):
                self.open = not self.open
                title = "Open" if self.open else "Untitled"
                return {"nodes": [{"ref": "d.0", "role": "AXWindow", "title": title, "depth": 0},
                                  {"ref": "d.1", "role": "AXStaticText", "value": title, "parent": "d.0"}]}
            return super().call(method, timeout, **p)

    def open_it(state, questions):
        opts = questions["action"]["criteria"]
        pick = next((k for k, v in opts.items() if "Open" in v), None)
        return {"pick": pick or "done", "move": "act" if pick else "done"}

    d = ScriptedDecider([open_it] * 12)
    eng = Engine(cfg(tmp_path, config={"engine": {"max_steps": 12}}), helper=TwoScreens(), decider=d)
    res = eng.do("open this file")
    opened = [a for a in (res.get("steps") or []) if "Open" in a]
    assert len(opened) <= int(Config.load().get("engine.max_circles")), \
        f"it went round the same loop {len(opened)} times: {opened}"


def test_a_locked_screen_is_reported_as_such(tmp_path):
    c = cfg(tmp_path, config={"engine": {"locked_channels": ["keys"]}})
    eng = Engine(c, helper=FakeHelper(locked=True), decider=ScriptedDecider([{"pick": "pagedown", "move": "blocked"}]))
    res = eng.do("make a new document")
    assert res["status"] == "blocked" and "locked" in res["reason"]


class OpensAnApp(FakeHelper):
    """After the first look, an app appears that the task opened; apps.quit may or may not succeed."""

    def __init__(self, quits=True, **kw):
        super().__init__(**kw)
        self.quits, self.looks = quits, 0

    def call(self, method, timeout=30.0, **p):
        import time as _t
        if method == "apps.running":
            self.calls.append((method, p))
            self.looks += 1
            base = super().call(method, timeout, **p)
            if self.looks > 1 and not self.did("apps.quit") or (self.did("apps.quit") and not self.quits):
                base = base + [{"pid": 77, "name": "Calculator", "bundle_id": "com.apple.calculator",
                                "path": "/System/Applications/Calculator.app", "launched": _t.time()}]
            return base
        if method == "apps.quit":
            self.calls.append((method, p))
            return {"terminated": self.quits}
        return super().call(method, timeout, **p)


class Tidier(ScriptedDecider):
    def __init__(self, script, keep=0.1, bg=0.1):
        super().__init__(script)
        self.keep, self.bg = keep, bg

    def decide(self, state, questions):
        if any(k.startswith(("keep", "win")) for k in questions):
            self.side.append((state, questions))
            return {k: {"type": "noul", "noul": self.keep if k.startswith(("keep", "win")) else self.bg} for k in questions}
        return super().decide(state, questions)


def test_apps_the_task_opened_are_closed_when_no_longer_needed(tmp_path):
    h = OpensAnApp()
    d = Tidier([{"pick": "New Document"}, {"pick": "done"}])
    res = Engine(cfg(tmp_path), helper=h, decider=d).do("use the calculator to work out 1+1")
    assert [p["pid"] for p in h.did("apps.quit")] == [77] and res["outputs"]["closed"] == ["Calculator"]
    state = d.side[-1][0]
    assert state["apps_opened_by_the_task"][0]["app"] == "Calculator" and state["result_screen"]["text"]   # the answer is kept
    assert all(p["pid"] != 42 for p in h.did("apps.quit"))                          # what was already running: never


def test_needed_or_background_apps_stay_open(tmp_path):
    for keep, bg, why in ((0.9, 0.1, "still needed"), (0.1, 0.9, "background")):
        h = OpensAnApp()
        res = Engine(cfg(tmp_path), helper=h, decider=Tidier([{"pick": "New Document"}, {"pick": "done"}], keep, bg)).do("open Calculator")
        assert not h.did("apps.quit") and why in res["outputs"]["left_open"][0]["why"]


def test_a_quit_that_asks_something_is_cancelled_and_reported(tmp_path):
    h = OpensAnApp(quits=False)
    d = Tidier([{"pick": "New Document"}, {"pick": "done"}])
    d.why = "k1"                        # the cancel-quit choice: press Escape
    res = Engine(cfg(tmp_path, config={"engine": {"activate_wait_s": 0.05}}), helper=h, decider=d).do("use the calculator to work out 1+1")
    assert "asked something" in res["outputs"]["left_open"][0]["why"]
    assert any("cancels quitting" in q["back"]["instructions"] for _s, q in d.side if "back" in q)


def test_pending_tasks_and_tidy_off_touch_nothing(tmp_path):
    h = OpensAnApp()
    res = Engine(cfg(tmp_path), helper=h, decider=Tidier([{"pick": "New Document"}, {"pick": "Recipient"}])).do("fill in the recipient")
    assert res["status"] == "need_input" and not h.did("apps.quit")
    h = OpensAnApp()
    Engine(cfg(tmp_path, config={"engine": {"tidy": "off"}}), helper=h, decider=Tidier([{"pick": "New Document"}, {"pick": "done"}])).do("x")
    assert not h.did("apps.quit")


def test_a_gated_action_the_goal_never_asked_for_is_dropped_not_put_to_the_user(tmp_path):
    d = ScriptedDecider([{"pick": "Delete Document"}, {"pick": "New Document"}, {"pick": "done"}])
    d.calls_for = 0.1
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    assert not any("Delete Document" in v for v in d.seen[1][1]["action"]["criteria"].values())


def test_rethinking_with_no_new_route_ends_in_a_diagnosis_not_wandering(tmp_path):
    """It still says why instead of wandering — but only once the screen itself has had a fair try. A real task
    gave up after two steps with the action it needed sitting in the options, because the planner (which it did
    not even have here) had nothing to add."""
    def rethink(state, questions):   # the same thing while it is offered; the repeat ceiling takes it away
        opts = questions["action"]["criteria"]
        pick = next((k for k, v in opts.items() if "New Document" in v), None) or next(k for k in opts if k != "done")
        return {"pick": pick, "move": "rethink"}

    d = ScriptedDecider([rethink] * 8)
    d.why = "blocked"
    res = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d).do("open settings")     # no planner configured
    assert res["status"] == "blocked" and res["reason"].startswith("no route to the goal was found")
    assert len(res["steps"]) >= int(Config.load().get("engine.min_steps_before_giving_up"))


def test_an_app_without_windows_can_have_its_main_window_brought_back(tmp_path):
    from macwork.observe import windows
    class NoWindows(FakeHelper):
        def call(self, method, timeout=30.0, **p):
            if method == "ax.snapshot" and p.get("scope") == "windows":
                return {"nodes": []}
            return super().call(method, timeout, **p)
    obs = Observation(app=None, window=None, affordances=[])
    windows(Ctx(cfg(tmp_path), NoWindows(), app={"pid": 42, "name": "ColorSync Utility", "path": "/System/Applications/Utilities/ColorSync Utility.app"}), obs)
    assert [(a.channel, a.verb) for a in obs.affordances] == [("app", "reopen")] and "main window" in obs.affordances[0].label


def test_a_decision_made_while_the_screen_still_moved_is_made_again(tmp_path):
    class Moving(FakeHelper):      # after the step, the window changes once more while the decider thinks
        after = iter([5, 5, 5, 6])   # probe (not moving by itself), probe, seen before deciding, changed after

        def call(self, method, timeout=30.0, **p):
            if method == "ax.fingerprint":
                self.calls.append((method, p))
                return {"fingerprint": next(self.after, 6) if self.did("ax.perform") else 1}
            return super().call(method, timeout, **p)
    d = ScriptedDecider([{"pick": "New Document"}, {"pick": "New Document"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=Moving(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and len(res["steps"]) == 1 and len(d.seen) == 3    # one decision thrown away


def test_waiting_stops_early_when_speculating(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([{"pick": "New Document"}, {"pick": "done"}]))
    eng.cfg.docs["config"]["engine"]["verify"]["poll_ms"] = 80
    eng.do("make a new document")
    w = eng.helper.did("ax.wait")[0]
    assert w["quiet_ms"] == 80 and w["timeout_ms"] == 800 and w["settle_ms"] == 0


MENU_WITH_WORDS = {"nodes": MENUBAR["nodes"] + [
    {"ref": "g1.20", "role": "AXMenuBarItem", "title": "Edit", "parent": "g1.0"},
    {"ref": "g1.21", "role": "AXMenu", "parent": "g1.20"},
    {"ref": "g1.22", "role": "AXMenuItem", "title": "Replace", "parent": "g1.21"},
    {"ref": "g1.23", "role": "AXMenu", "parent": "g1.22"},
    {"ref": "g1.24", "role": "AXMenuItem", "title": "Show Replace", "parent": "g1.23"}], "ms": 3}


class WordsHelper(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "menubar":
            return MENU_WITH_WORDS
        return super().call(method, timeout, **p)


def test_floor_words_are_candidates_the_decider_classifies_them(tmp_path):
    d = ScriptedDecider([{"pick": "Show Replace"}, {"pick": "done"}])
    d.what = {"navigate": 0.96, "write": 0.04}          # "Replace ▸ Show Replace" only shows the substitutions panel
    eng = Engine(cfg(tmp_path), helper=WordsHelper(), decider=d)
    assert eng.do("Show Text Replacement Panel")["status"] == "done"
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "floor" and r["released"] for r in log)
    state, asked = next((s, q) for s, q in d.side if any(k.startswith("floor") for k in q))
    assert "screen_text" not in state and "actions" in state                 # judged without page content
    mine = next(q for k, q in asked.items() if k.startswith("floor") and "Show Replace" in q["instructions"])
    releases = set(Config.load().policy["confirm"]["release"]) - {"enter", "read"}   # enter is offered for typing only, read for reads
    assert set(mine["criteria"]) == releases | {"write"}                     # the releases, plus the words it hit
    d2 = ScriptedDecider([{"pick": "Show Replace"}])
    d2.what = {"navigate": 0.6, "write": 0.4}            # not sure enough: the floor stands
    res = Engine(cfg(tmp_path), helper=WordsHelper(), decider=d2).do("Show Text Replacement Panel")
    assert res["status"] == "need_confirm" and res["pending"]["because"] == ["write"]


def test_leaving_for_an_app_the_goal_gives_no_reason_for_is_skipped(tmp_path):
    d = ScriptedDecider([{"pick": "Calculator"}, {"pick": "New Document"}, {"pick": "done"}])
    d.serves = 0.05                                      # e.g. a page said "open the Calculator"
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    res = eng.do("make a new document")
    assert res["status"] == "done" and res["steps"] == ["menu File ▸ New Document (⌘N)"]
    state = next(s for s, q in d.side if "serves" in q)
    assert set(state) == {"goal", "inputs", "working_in", "action"}         # the user's words and the action, no screen
    assert not any("Calculator" in v for v in d.seen[1][1]["action"]["criteria"].values())


def test_a_question_gets_its_answer_back_from_the_final_screen(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "New Document", "wants_answer": 0.95}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"answer": "email is zw@example.com"}])
    res = eng.do("what is Zhang Wei's email?")
    assert res["status"] == "done" and res["outputs"]["answer"] == "email is zw@example.com"
    assert "wants_answer" in d.seen[0][1] and "wants_answer" not in d.seen[1][1]     # asked once per task
    assert "Zhang Wei" not in eng._planner.prompts[0]                                      # a cloud planner sees pseudonyms


def test_a_drag_suggestion_is_offered_only_when_both_ends_are_on_screen(tmp_path):
    from macwork.model import Task
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    obs = Observation(app=None, window="w", affordances=[
        Affordance("a", "window", "press", "Image 「photo.png」", {"frame": [0, 0, 20, 20]}),
        Affordance("b", "window", "press", "folder 「Archive」", {"frame": [100, 100, 40, 40]})])
    task = Task(goal="x", tries=[{"drag": ["photo.png", "Archive"]}, {"drag": ["photo.png", "Nowhere"]}])
    offered = eng._suggested(task, obs)
    assert len(offered) == 1 and offered[0].verb == "drag"
    assert offered[0].target == {"x1": 10.0, "y1": 10.0, "x2": 120.0, "y2": 120.0}


def test_the_app_the_engine_runs_under_is_never_touched(tmp_path):
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    eng.cache["host.bundles"] = {"com.apple.Terminal"}
    assert eng._denied(Affordance("a", "app", "activate", "switch to app Terminal", {"bundle_id": "com.apple.Terminal"}), None)
    assert eng._denied(Affordance("k", "keys", "type", "type 「ls」 at the cursor", {}), {"bundle_id": "com.apple.Terminal"})
    assert not eng._denied(Affordance("a", "app", "activate", "switch to app Finder", {"bundle_id": "com.apple.finder"}), None)


def test_a_scripting_command_that_runs_code_is_not_run_unasked(tmp_path):
    d = ScriptedDecider([{"pick": "do script"}, {"pick": "New Document"}, {"pick": "done"}])
    d.what, d.what_when = {"execute": 0.97, "navigate": 0.03}, "do script"
    d.calls_for = 0.1
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)

    class Sdef(FakeHelper):
        pass
    eng.cfg.docs["config"]["observe"]["providers"] = ["menu", "window", "keys"]
    import macwork.loop as loopmod
    real = loopmod.observe

    def with_script(ctx):
        o = real(ctx)
        o.affordances.append(Affordance("d0", "script_cmd", "run", "scripting command 「do script」: Runs a UNIX shell script or command.",
                                        {"bundle_id": "com.apple.Terminal", "command": "do script"}))
        return o
    loopmod.observe = with_script
    try:
        res = eng.do("make a new document")
    finally:
        loopmod.observe = real
    assert res["status"] == "done" and "do script" not in " ".join(res["steps"])


def test_typing_a_command_is_judged_with_its_text(tmp_path):
    from tests.test_flex import FakeBackend
    d = ScriptedDecider([{"pick": "Recipient"}])
    d.what, d.what_when = {"execute": 0.9, "enter": 0.1}, "rm -rf"
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    eng._planner = FakeBackend([{"text": "rm -rf ~/Documents"}])
    res = eng.do("fill in the recipient with rm -rf ~/Documents")      # asked for by the user, so it is not refused as invented
    assert res["status"] == "need_confirm" and res["pending"]["because"] == ["execute"]
    assert not eng.helper.did("input.type") and "rm -rf" in res["pending"]["confirm"]["text"]


class LeavesAWindow(FakeHelper):
    """Finder (already running) gets a new window during the task; its close button closes it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.screen = [{"pid": 7, "id": 1, "layer": 0, "alpha": 1, "frame": [0, 0, 500, 400]}]

    def call(self, method, timeout=30.0, **p):
        if method == "ax.perform" and p.get("ref") == "fw2.close":
            self.calls.append((method, p))
            self.screen = [w for w in self.screen if w["id"] != 2]
            return {"ok": True}
        if method == "ax.snapshot" and p.get("pid") == 7 and p.get("scope") == "windows":
            return {"nodes": [{"ref": "fw1", "role": "AXWindow", "title": "Document", "frame": [0, 0, 500, 400]},
                              {"ref": "fw2", "role": "AXWindow", "title": "Caches", "frame": [50, 50, 600, 400]},
                              {"ref": "fw2.close", "role": "AXButton", "subrole": "AXCloseButton", "parent": "fw2"}]}
        out = super().call(method, timeout, **p)
        if method == "ax.perform" and not any(w["id"] == 2 for w in self.screen) and self.did("ax.perform") and len(self.did("ax.perform")) == 1:
            self.screen = self.screen + [{"pid": 7, "id": 2, "layer": 0, "alpha": 1, "frame": [50, 50, 600, 400]}]
        return out


def test_windows_the_task_left_in_running_apps_are_closed_unless_needed(tmp_path):
    h = LeavesAWindow()
    d = Tidier([{"pick": "New Document"}, {"pick": "done"}])
    res = Engine(cfg(tmp_path, config={"engine": {"close_wait_s": 0}}), helper=h, decider=d).do("find that file")
    assert res["outputs"]["closed"] == ["Finder: Caches"]
    asked = next(s for s, q in d.side if any(k.startswith("win") for k in q))
    assert asked["windows_opened_by_the_task"] == [{"app": "Finder", "window": "Caches", "kind": "window"}]
    assert not any(p.get("ref") == "fw1.close" for p in h.did("ax.perform"))      # what was there before: untouched
    h2 = LeavesAWindow()
    res2 = Engine(cfg(tmp_path, config={"engine": {"close_wait_s": 0}}), helper=h2, decider=Tidier([{"pick": "New Document"}, {"pick": "done"}], keep=0.9)).do("open that folder")
    assert res2["outputs"]["left_open"] == [{"window": "Finder: Caches", "why": "still needed for the goal"}]


def test_a_field_falls_back_to_setting_its_value_when_the_app_cannot_come_forward(tmp_path, monkeypatch):
    import macwork.act as act
    monkeypatch.setattr(act.subprocess, "run", lambda *a, **k: None)
    d = ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path, config={"engine": {"activate_wait_s": 0.05}}), helper=StuckInBackHelper(), decider=d)
    res = eng.do("fill in the recipient", {"text": "someone@example.com"}, app="TextEdit")
    assert eng.helper.did("ax.set")[0] == {"ref": "g2.3", "attribute": "AXValue", "value": "someone@example.com"}
    assert not eng.helper.did("input.type") and res["status"] == "done"


class UserIsTyping(FakeHelper):
    def call(self, method, timeout=30.0, **p):
        if method == "input.idle":
            self.calls.append((method, p))
            return {"idle_s": 0.2}          # the user touched the keyboard a moment ago
        return super().call(method, timeout, **p)


def test_the_keyboard_is_not_taken_while_the_user_is_using_the_mac(tmp_path):
    c = cfg(tmp_path, config={"engine": {"mode": "yield", "yield_max_s": 0.3, "yield_idle_s": 1.5}})
    eng = Engine(c, helper=UserIsTyping(), decider=ScriptedDecider([{"pick": "pagedown"}, {"pick": "done"}]))
    eng.cfg.docs["config"]["observe"]["providers"] = ["keys"]
    res = eng.do("Page Down", app="TextEdit")
    assert not eng.helper.did("input.key")                                  # nothing was typed into what they are doing
    assert "the Mac is in use" in (eng.tasks[res["task_id"]].steps[0].error or "")
    eng2 = Engine(cfg(tmp_path, config={"engine": {"mode": "exclusive"}}), helper=UserIsTyping(),
                  decider=ScriptedDecider([{"pick": "pagedown"}, {"pick": "done"}]))
    eng2.cfg.docs["config"]["observe"]["providers"] = ["keys"]
    eng2.do("Page Down", app="TextEdit")
    assert eng2.helper.did("input.key")                                     # an exclusive run owns the Mac


def test_a_step_that_did_not_keep_its_promise_counts_as_failed(tmp_path):
    class IgnoresTyping(FakeHelper):
        """The field shows the old text however it is written to (Finder's "Go to Folder", a browser address bar)."""

        def call(self, method, timeout=30.0, **p):
            if method == "ax.get":
                self.calls.append((method, p))
                return {"value": "/old/path"}
            return super().call(method, timeout, **p)
    d = ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=IgnoresTyping(), decider=d)
    res = eng.do("fill in the recipient", {"text": "/new/path"}, app="TextEdit")
    step = eng.tasks[res["task_id"]].steps[0]
    assert step.ok is False and "not what was typed" in (step.error or "")
    assert step.decision.get("verified_effect") is False


def test_a_step_that_kept_its_promise_is_marked_verified(tmp_path):
    d = ScriptedDecider([{"pick": "Recipient"}, {"pick": "done"}])
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=d)
    res = eng.do("fill in the recipient", {"text": "someone@example.com"}, app="TextEdit")
    step = eng.tasks[res["task_id"]].steps[0]
    assert step.ok and step.decision.get("verified_effect") is True
