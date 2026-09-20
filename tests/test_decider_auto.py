"""The decider is the one part with no fallback, and it needed one.

Every step needs a decision, so no key, no network or that vendor being down meant nothing ran at all. The
`macwork.deciders` entry point existed for a second decider and an implementation was written for it — and
reaching it still meant editing a config file, which is a manual recovery, not a fallback.

Not a tie, though: Jev returns calibrated probabilities and every threshold in config.yaml is a cut-off on
those. So the order is a preference, a switch is loud, and `status()` says who actually answered.
"""

import logging

import pytest

from macwork import decider as D
from macwork.config import Config


class Fake:
    def __init__(self, name, fails=0):
        self.name, self.fails, self.calls, self.cost_usd, self.last_ms = name, fails, 0, 0.0, 1.0

    def decide(self, state, questions):
        self.calls += 1
        if self.fails:
            self.fails -= 1
            raise D.DeciderError(f"{self.name} is not answering")
        return {"q": {"type": "noul", "noul": 1.0, "confidence": 1.0, "_by": self.name}}


def cfg(**over):
    return Config.load(overrides={"config": {"decider": {"kind": "auto", **over}}})


def build(monkeypatch, made):
    """`made`: name -> a Fake, or an exception to raise when that one is built."""
    def _build(kind, c, h):
        got = made.get(kind)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise D.DeciderError(f"unknown decider '{kind}'")
        return got
    monkeypatch.setattr(D, "_build", _build)


# --------------------------------------------------------------------------- choosing one

def test_the_preferred_one_is_used_when_it_is_there(monkeypatch):
    jev = Fake("jev")
    build(monkeypatch, {"jev": jev, "local": Fake("local")})
    assert D.make_decider(cfg()).decide({}, {})["q"]["_by"] == "jev"


def test_no_key_is_no_longer_the_end_of_it(monkeypatch):
    build(monkeypatch, {"jev": D.DeciderError("no TypeSafe API key"), "local": Fake("local")})
    assert D.make_decider(cfg()).decide({}, {})["q"]["_by"] == "local"


def test_when_none_can_be_reached_the_error_says_what_was_tried(monkeypatch):
    build(monkeypatch, {"jev": D.DeciderError("no key"), "local": D.DeciderError("nothing is running")})
    with pytest.raises(D.DeciderError, match="no key.*nothing is running"):
        D.make_decider(cfg())


def test_a_backend_that_will_not_even_import_is_survived(monkeypatch):
    build(monkeypatch, {"jev": ImportError("no module named typesafe_sdk"), "local": Fake("local")})
    assert D.make_decider(cfg()).decide({}, {})["q"]["_by"] == "local"


def test_naming_one_explicitly_still_means_only_that_one(monkeypatch):
    build(monkeypatch, {"jev": D.DeciderError("no key"), "local": Fake("local")})
    with pytest.raises(D.DeciderError):
        D.make_decider(Config.load(overrides={"config": {"decider": {"kind": "jev"}}}))


def test_dry_run_is_never_fallen_back_from(monkeypatch):
    """Audit mode's whole promise is that nothing leaves the Mac; a fallback would break it silently."""
    build(monkeypatch, {"jev": Fake("jev"), "local": Fake("local")})
    d = D.make_decider(Config.load(overrides={"config": {"decider": {"kind": "auto"}, "audit": {"dry_run": True}}}))
    with pytest.raises(D.DeciderError):
        d.decide({}, {})


# --------------------------------------------------------------------------- losing one mid-session

def test_a_key_that_works_and_a_network_that_does_not_is_the_case_that_matters(monkeypatch):
    build(monkeypatch, {"jev": Fake("jev", fails=1), "local": Fake("local")})
    assert D.make_decider(cfg()).decide({}, {})["q"]["_by"] == "local"


def test_it_stays_switched_rather_than_alternating(monkeypatch):
    """Half a task decided by a calibrated model and half by an uncalibrated one makes the thresholds in
    config.yaml mean nothing in particular."""
    jev = Fake("jev", fails=1)
    d = build(monkeypatch, {"jev": jev, "local": Fake("local")}) or D.make_decider(cfg())
    assert [d.decide({}, {})["q"]["_by"] for _ in range(3)] == ["local"] * 3
    assert jev.calls == 1


def test_the_switch_is_loud(monkeypatch, caplog):
    build(monkeypatch, {"jev": Fake("jev", fails=1), "local": Fake("local")})
    with caplog.at_level(logging.WARNING, logger="macwork.decider"):
        D.make_decider(cfg()).decide({}, {})
    assert "switching to local" in caplog.text


def test_the_last_one_left_raises_rather_than_going_quiet(monkeypatch):
    build(monkeypatch, {"jev": Fake("jev", fails=9), "local": Fake("local", fails=9)})
    with pytest.raises(D.DeciderError):
        D.make_decider(cfg()).decide({}, {})


def test_an_answer_is_never_second_guessed_by_the_next_one(monkeypatch):
    """Asking a second model until one agrees is not a fallback."""
    local = Fake("local")
    build(monkeypatch, {"jev": Fake("jev"), "local": local})
    D.make_decider(cfg()).decide({}, {})
    assert local.calls == 0


def test_who_answered_is_reportable(monkeypatch):
    build(monkeypatch, {"jev": Fake("jev", fails=1), "local": Fake("local")})
    d = D.make_decider(cfg())
    assert d.name == "jev"
    d.decide({}, {})
    assert d.name == "local"     # status() reads this: with `auto`, the configured kind is not the answer


# --------------------------------------------------------------------------- saying which one answered

def test_doctor_does_not_report_a_fallback_as_if_it_were_fine(tmp_path, capsys, monkeypatch):
    """`using: local:x` on its own reads as green while that backend may be handing back 401 — the same
    kind of misreport as showing the configured `kind` once `auto` exists."""
    import argparse

    from macwork import cli          # noqa: F401  (the command under test)

    class Eng:
        cfg = Config.load()
        helper = None

        def status(self):
            return {"helper": {"ax_trusted": True}, "decider": {"kind": "auto", "using": "local:deepseek"}}

    import macwork.engine as engine
    import macwork.planner as planner

    monkeypatch.setattr(engine, "Engine", lambda cfg: Eng())   # cmd_doctor imports it inside the function
    monkeypatch.setattr(planner, "probe_planners",
                        lambda cfg, helper: [{"planner": "deepseek", "status": "credentials rejected",
                                              "detail": "deepseek: credentials rejected (401)"}])
    code = cli.cmd_doctor(Config.load(), argparse.Namespace(planners=True, ask=False))
    err = capsys.readouterr().err
    assert code == 1 and "Nothing can decide" in err
