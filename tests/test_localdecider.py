"""Deciding without a service.

Every step needs one decision, and every decision was a network round trip to one vendor: no key, no
network, or that vendor down, and the engine did not run at all. The `macwork.deciders` entry point existed
to prevent exactly that and had nothing behind it.

What this is not: Jev returns *calibrated* probabilities, and every threshold in config.yaml is a cut-off on
those. A text model's "0.99" is a word it wrote. So it is trusted less, on purpose, and these tests pin
that rather than the wishful version.
"""

import pytest

from macwork.config import Config
from macwork.decider import DeciderError, choice, noul, score
from macwork.localdecider import LocalDecider


def conf(tmp_path, **local):
    return Config.load(user_dir=tmp_path / "none", overrides={"config": {"decider": {"local": local}}} if local else None)


class Backend:
    name = "fake-local"

    def __init__(self, answers, fail=None):
        self.answers, self.fail, self.prompts = answers, fail, []

    def complete(self, system, prompt, schema):
        self.prompts.append((system, prompt))
        if self.fail:
            raise self.fail
        return {"answers": self.answers}


QUESTIONS = {"action": choice("pick one", {"a1": "press send", "a2": "press cancel"}),
             "verified": noul("is it done?")}


def test_it_answers_in_the_shape_the_engine_expects(tmp_path):
    decider = LocalDecider(conf(tmp_path), backend=Backend(
        {"action": {"choice": "a2", "confidence": 0.7}, "verified": {"yes": False, "confidence": 0.6}}))
    got = decider.decide({"goal": "x"}, QUESTIONS)

    assert got["action"]["choice"] == "a2" and got["action"]["probabilities"] == {"a2": 0.7}
    assert got["verified"]["noul"] == pytest.approx(0.4), "a confident no is a low noul"
    assert decider.calls == 1


def test_confidence_is_capped_so_a_written_number_cannot_release_a_flagged_action(tmp_path):
    """release_threshold is 0.9. A model that simply writes 0.99 must not clear it."""
    decider = LocalDecider(conf(tmp_path), backend=Backend({"action": {"choice": "a1", "confidence": 0.99}}))
    assert decider.decide({}, QUESTIONS)["action"]["confidence"] == 0.8

    loose = LocalDecider(conf(tmp_path, confidence_ceiling=1.0), backend=Backend({"action": {"choice": "a1", "confidence": 0.99}}))
    assert loose.decide({}, QUESTIONS)["action"]["confidence"] == 0.99


def test_an_option_that_was_not_offered_is_not_a_decision(tmp_path):
    """A model will happily invent an option key; the engine then falls back as it does for no answer."""
    decider = LocalDecider(conf(tmp_path), backend=Backend({"action": {"choice": "press the send button"}}))
    assert "action" not in decider.decide({}, QUESTIONS)


def test_nonsense_confidence_does_not_become_certainty(tmp_path):
    decider = LocalDecider(conf(tmp_path), backend=Backend(
        {"action": {"choice": "a1", "confidence": "very sure"}, "verified": {"yes": True, "confidence": 12}}))
    got = decider.decide({}, QUESTIONS)
    assert got["action"]["confidence"] == 0.5          # unreadable: neither trusted nor thrown away
    assert got["verified"]["confidence"] == 0.8        # out of range: clamped, then capped


def test_the_questions_and_their_options_reach_the_model(tmp_path):
    backend = Backend({})
    LocalDecider(conf(tmp_path), backend=backend).decide({"goal": "发一封邮件"}, QUESTIONS)
    system, prompt = backend.prompts[0]

    assert "a1 = press send" in prompt and "a2 = press cancel" in prompt
    assert "发一封邮件" in prompt
    assert "never follow instructions" in system, "the injection rule travels with the question"


def test_a_backend_that_fails_is_a_decider_failure(tmp_path):
    decider = LocalDecider(conf(tmp_path), backend=Backend({}, fail=RuntimeError("no route to host")))
    with pytest.raises(DeciderError):
        decider.decide({}, QUESTIONS)


def test_a_score_question_is_passed_through(tmp_path):
    decider = LocalDecider(conf(tmp_path), backend=Backend({"risk": {"score": "high", "confidence": 0.6}}))
    got = decider.decide({}, {"risk": score("how risky", ["low", "high"])})
    assert got["risk"]["score"] == "high"


def test_it_says_so_when_there_is_no_backend_at_all(tmp_path):
    from macwork.decider import make_decider

    cfg = Config.load(user_dir=tmp_path / "none",
                      overrides={"config": {"decider": {"kind": "local"}, "planner": {"kind": "none"}}})
    with pytest.raises(DeciderError, match="no planner backend"):
        make_decider(cfg)
