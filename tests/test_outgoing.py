"""What an MCP caller is handed is leaving this process.

`README.md` says everything a decider sees passes through `privacy.py` first, and at the goal level it did.
At the step level, `mac_observe` handed the caller the screen text, every field's contents and the document's
path in the clear, and `mac_act` handed it whatever a read returned — the whole file — unaudited. The caller
is a model somewhere else; the step level was the one door with no redaction on it.
"""

from macwork.engine import Engine
from macwork.observe import PROVIDERS, Ctx
from tests.english_mac import APP, EnglishMac
from tests.test_engine import ScriptedDecider, cfg


def engine(tmp_path, text="write to jo.bloggs@example.com about ~/Documents/plan.txt"):
    helper = EnglishMac()
    helper.text = text
    eng = Engine(cfg(tmp_path, config={"observe": {"providers": ["window"]}}), helper=helper, decider=ScriptedDecider([]))
    return eng


def test_what_goes_out_is_redacted_like_a_decider_request(tmp_path):
    eng = engine(tmp_path)
    seen = eng.observe(redact=True)
    assert "jo.bloggs@example.com" not in str(seen), "an address on screen went to the caller in the clear"
    assert "⟦EMAIL_1⟧" in seen["screen_text"]
    assert all("jo.bloggs" not in a["label"] for a in seen["affordances"])


def test_reading_its_own_screen_is_not_redacted(tmp_path):
    """The CLI prints to the person whose screen it is."""
    seen = engine(tmp_path).observe()
    assert "jo.bloggs@example.com" in seen["screen_text"]


def test_nothing_goes_out_when_tagging_broke(tmp_path):
    eng = engine(tmp_path)
    eng.redactor("observe").failed = True          # what a tagger failure leaves behind
    seen = eng.observe(redact=True)
    assert seen == {"error": "personal data could not be checked for, so nothing was returned", "withheld": True}


def test_every_answer_to_a_caller_is_on_the_audit_log(tmp_path):
    eng = engine(tmp_path)
    eng.observe(redact=True)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"kind": "observe"' in line for line in lines)
    assert not any("jo.bloggs" in line for line in lines), "the log holds what was sent, which was redacted"


def test_the_server_redacts_unless_told_not_to(tmp_path):
    from macwork import server
    calls = []

    class Eng:
        def observe(self, app, goal, inputs, limit, redact=False, **kw):
            calls.append(redact)
            return {}

        def act(self, *a, redact=False, **k):
            calls.append(redact)
            return {}

    import anyio
    mcp = server.build(cfg(tmp_path), engine=Eng())
    tool = next(t for t in mcp._tool_manager._tools.values() if t.name == "mac_observe")
    anyio.run(tool.fn)
    mcp = server.build(cfg(tmp_path, config={"server": {"raw_observe": True}}), engine=Eng())
    tool = next(t for t in mcp._tool_manager._tools.values() if t.name == "mac_observe")
    anyio.run(tool.fn)
    assert calls == [True, False]
