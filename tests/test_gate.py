"""The Gate is the only door to the decider, so everything that goes through it must be redacted.

`README.md` opens its privacy section with "Everything the decider sees passes through `privacy.py` on this
Mac first". It did not. `Gate.decide` redacted the state and each question's `criteria`, and carried
`instructions` through untouched — and `instructions` is exactly where twelve call sites interpolate a live
UI string:

    policy.py   `choice(self.cfg.question("floor_what").replace("{action}", a.label), ...)`
    tidy.py     `{app}` / `{window}` / `{action}`
    learn.py    `{action}`

An affordance label carries what a field holds by construction (`observe.py` appends `(now: …)`), and a row
is labelled by the text it shows. So one request went out with the same email pseudonymised in `state` and
in the clear in `instructions`. The safety floor classifies *every* action before it runs, so this was
several times per step, on the hot path, for the life of the project.

`macwork privacy-check` could not see it: it measures `Redactor` against a corpus, never `Gate.decide`.
"""

from typing import Any

import pytest

from macwork.privacy import Audit, Gate, Redactor


class Decider:
    def __init__(self) -> None:
        self.state: Any = None
        self.questions: Any = None

    def decide(self, state, questions):
        self.state, self.questions = state, questions
        return {k: {"type": "choice", "choice": "a", "confidence": 1.0} for k in questions}


def gate(tmp_path, **conf):
    from macwork.config import Config
    cfg = Config.load(overrides={"config": {"audit": {"path": str(tmp_path / "a.jsonl")}}} | (conf or {}))
    d = Decider()
    return Gate(d, Audit(cfg)), Redactor(cfg), d


SECRETS = ["wang.zhaokai@example.com", "/Users/wangzhaokai", "13800138000"]


def ask(g, r, instructions: str, state: Any = None):
    g.decide(r, state if state is not None else {"app": "Mail"},
             {"what": {"type": "choice", "instructions": instructions,
                       "criteria": {"navigate": "it only navigates", "delete": "it deletes something"}}})


# --------------------------------------------------------------------------- the leak

@pytest.mark.parametrize("secret", SECRETS)
def test_nothing_personal_survives_in_the_instructions(tmp_path, secret):
    g, r, d = gate(tmp_path)
    ask(g, r, f'what does choosing "press 「{secret}」" actually do?')
    assert secret not in str(d.questions), f"{secret!r} left this Mac in a question's instructions"


def test_the_state_and_the_instructions_agree_on_the_pseudonym(tmp_path):
    """One pass for the whole request, or the decider is shown two different names for one person and the
    answer it gives is about neither."""
    g, r, d = gate(tmp_path)
    ask(g, r, 'what does "press 「mail to wang.zhaokai@example.com」" do?',
        state={"app": "Mail", "action": "press 「mail to wang.zhaokai@example.com」"})
    token = d.state["action"].split("「")[1].split("」")[0].replace("mail to ", "")
    assert token.startswith("⟦") and token in d.questions["what"]["instructions"]


def test_the_audit_log_records_what_was_actually_sent(tmp_path):
    """The log is the evidence; if it shows the redacted form while the clear form went out, it is worse
    than no log."""
    g, r, d = gate(tmp_path)
    ask(g, r, 'press 「wang.zhaokai@example.com」')
    logged = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert "wang.zhaokai@example.com" not in logged
    assert str(d.questions["what"]["instructions"]) in logged


def test_the_question_itself_is_not_mangled(tmp_path):
    """Redacting the wording must not eat the question: the decider has to still know what is being asked."""
    g, r, d = gate(tmp_path)
    wording = "In the app named in the state, what does choosing this action actually do? Judge from the action alone."
    ask(g, r, wording)
    assert d.questions["what"]["instructions"] == wording


def test_criteria_and_state_are_still_redacted(tmp_path):
    g, r, d = gate(tmp_path)
    g.decide(r, {"app": "Mail", "screen_text": "call 13800138000"},
             {"what": {"type": "choice", "instructions": "which one?",
                       "criteria": {"a": "write to wang.zhaokai@example.com", "b": "no"}}})
    assert "13800138000" not in str(d.state) and "wang.zhaokai@example.com" not in str(d.questions)


def test_a_question_with_no_criteria_is_redacted_too(tmp_path):
    """A yes/no question has instructions and nothing else — it used to be passed through whole."""
    g, r, d = gate(tmp_path)
    g.decide(r, {"app": "Mail"},
             {"ok": {"type": "noul", "instructions": "did 「wang.zhaokai@example.com」 receive it?"}})
    assert "wang.zhaokai@example.com" not in str(d.questions)


def test_withholding_a_field_is_not_undone_by_the_instructions(tmp_path):
    """`never_send` replaced a state field with [withheld] while the same text rode along in the question."""
    from macwork.config import Config
    cfg = Config.load(overrides={"config": {"audit": {"path": str(tmp_path / "a.jsonl")}},
                                 "privacy": {"redact": {"never_send": ["screen_text"]}}})
    d = Decider()
    Gate(d, Audit(cfg)).decide(Redactor(cfg), {"screen_text": "13800138000"},
                               {"what": {"type": "noul", "instructions": "is 13800138000 on screen?"}})
    assert "13800138000" not in str(d.state) + str(d.questions)
