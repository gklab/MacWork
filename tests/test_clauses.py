"""A cap on how much gets checked must not be a cap on how much gets sent.

`redact.max_clauses` (600) bounded what went to the on-device tagger — and everything past it was sent
anyway, untagged. No error, no flag, no note: a long screen simply stopped being redacted partway through,
and the part that stopped is the part nobody looks at.

Redaction already has the right behaviour for "we could not check this": `failed` is set, `Gate.decide`
raises, and nothing is sent at all. Truncation is the same situation and was the only one that went quiet.
"""

from typing import Any

import pytest

from macwork.config import Config
from macwork.privacy import Redactor


def tagger(names: set[str]):
    """Stands in for the on-device tagger: finds any of `names` in a clause."""
    seen: list[list[str]] = []

    def entities(clauses: list[str]) -> list[list[dict[str, str]]]:
        seen.append(list(clauses))
        return [[{"type": "PERSON", "text": n} for n in names if n in c] for c in clauses]
    entities.batches = seen      # type: ignore[attr-defined]
    return entities


def redactor(tmp_path, ents, **over):
    cfg = Config.load(overrides={"privacy": {"redact": over}} if over else None)
    return Redactor(cfg, entities=ents)


def many(n: int) -> list[str]:
    """Clauses that survive `_worth_tagging` — capitalised words, so they look name-like and are really
    sent to the tagger. Filler the filter drops would not exercise the cap at all."""
    return [f"Meeting Notes Alpha{i} Bravo{i} Charlie{i}" for i in range(n)]


# --------------------------------------------------------------------------- the leak

def test_a_name_past_the_cap_does_not_go_out_in_the_clear(tmp_path):
    ents = tagger({"Emily Johnson"})
    r = redactor(tmp_path, ents, max_clauses=10)
    texts = many(30) + ["call Emily Johnson tomorrow"]
    out = r.value(texts)
    assert "Emily Johnson" not in str(out)


def test_everything_is_actually_checked_not_just_the_first_batch(tmp_path):
    ents = tagger({"Emily Johnson"})
    r = redactor(tmp_path, ents, max_clauses=10)
    r.value(many(30) + ["call Emily Johnson tomorrow"])
    checked = [c for batch in ents.batches for c in batch]          # type: ignore[attr-defined]
    assert any("Emily Johnson" in c for c in checked), "the clause with the name was never tagged"


def test_the_cap_is_a_batch_size_not_a_cliff(tmp_path):
    ents = tagger(set())
    r = redactor(tmp_path, ents, max_clauses=10)
    r.value(many(35))
    assert len(ents.batches) >= 4, "everything went in one oversized call"      # type: ignore[attr-defined]
    assert all(len(b) <= 10 for b in ents.batches), "a batch was bigger than the cap"  # type: ignore[attr-defined]


def test_a_short_request_still_makes_one_call(tmp_path):
    ents = tagger(set())
    redactor(tmp_path, ents, max_clauses=600).value(many(5))
    assert len(ents.batches) == 1      # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the ceiling that is left

def test_past_the_hard_ceiling_nothing_is_sent_at_all(tmp_path):
    """There has to be some end to it. When there is, it behaves like tagging failing — loudly — rather
    than like tagging succeeding on a part nobody named."""
    ents = tagger({"Emily Johnson"})
    r = redactor(tmp_path, ents, max_clauses=10, max_clauses_total=20)
    r.value(many(50) + ["call Emily Johnson tomorrow"])
    assert r.failed


def test_under_the_ceiling_it_does_not_refuse(tmp_path):
    ents = tagger(set())
    r = redactor(tmp_path, ents, max_clauses=10, max_clauses_total=1000)
    r.value(many(50))
    assert not r.failed


def test_the_gate_refuses_rather_than_sending_a_partly_checked_request(tmp_path):
    from macwork.privacy import Audit, Gate, RedactionError

    class Decider:
        sent = False

        def decide(self, state, questions):
            type(self).sent = True
            return {}

    cfg = Config.load(overrides={"config": {"audit": {"path": str(tmp_path / "a.jsonl")}},
                                 "privacy": {"redact": {"max_clauses": 10, "max_clauses_total": 20}}})
    d = Decider()
    # the shape a real request has: many separate strings — one per action on the screen — not one blob
    with pytest.raises(RedactionError):
        Gate(d, Audit(cfg)).decide(Redactor(cfg, entities=tagger({"Emily Johnson"})),
                                   {"app": "Mail", "actions": many(60)},
                                   {"q": {"type": "noul", "instructions": "ok?"}})
    assert not Decider.sent


def test_what_was_refused_says_why(tmp_path, caplog):
    import logging
    ents = tagger(set())
    r = redactor(tmp_path, ents, max_clauses=10, max_clauses_total=20)
    with caplog.at_level(logging.WARNING, logger="macwork.privacy"):
        r.value(many(50))
    assert "600" not in caplog.text and ("more than" in caplog.text or "ceiling" in caplog.text)
