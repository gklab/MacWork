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


# --------------------------------------------------------------------------- app names, and people called them

def protector(apps):
    from macwork.config import Config
    from macwork.privacy import Redactor
    return Redactor(Config.load(), protect=lambda: apps)


APPS = ["Mail", "Preview", "Numbers", "Pages", "Music", "Notes", "Maps", "Books", "Photos", "Reminders"]


@pytest.mark.parametrize("name", ["Mai", "Nu", "Ma", "Bo", "Note", "Photo", "Rem", "Pag"])
def test_a_person_is_not_protected_for_being_part_of_an_app_name(name):
    """`_is_protected` asked whether the name is a *substring* of any installed app's name, so anyone
    called Mai, Nu or Bo was never redacted — and short names in scripts without capitals are exactly the
    ones the tagger is least sure about."""
    assert not protector(APPS)._is_protected(name), f"{name!r} was taken for an app name"


@pytest.mark.parametrize("name", ["Mail", "mail", "Activity Monitor", "Monitor"])
def test_an_app_name_is_still_protected(name):
    """The point of the list: the decider has to see "switch to Mail" as an app, not ⟦PERSON_3⟧."""
    assert protector(APPS + ["Activity Monitor"])._is_protected(name)


def test_a_bundle_id_still_protects_its_own_app():
    assert protector(["com.apple.mail"])._is_protected("com.apple.mail")


# --------------------------------------------------------------------------- the same clause, every step

def test_a_clause_judged_not_worth_tagging_is_not_judged_again(tmp_path):
    """Measured with `macwork profile` and a decider that answers instantly: 163 ms of every step was the
    engine's own, and 155 ms of that was one line — a regex made of every installed app's name (405 here),
    run over 1574 clauses per step. A clause found worth tagging was remembered and skipped next time; one
    found *not* worth it was not, so menus and button labels were re-examined on every single step."""
    from macwork.config import Config
    from macwork.privacy import Redactor
    calls = []
    r = Redactor(Config.load(), entities=lambda cs: [[] for _ in cs], protect=lambda: ["Mail", "Numbers"])
    real = r._worth_tagging
    r._worth_tagging = lambda c: (calls.append(c), real(c))[1]          # type: ignore[method-assign]
    screen = {"actions": [f"menu item number {i}" for i in range(50)]}   # nothing name-like in any of them
    r.value(screen)
    first = len(calls)
    r.value(screen)                                                      # the next step: the same screen
    assert first > 0 and len(calls) == first, f"{len(calls) - first} clauses were examined a second time"


def test_remembering_that_does_not_stop_a_new_clause_being_looked_at(tmp_path):
    from macwork.config import Config
    from macwork.privacy import Redactor
    found = []
    def ents(cs):
        found.extend(cs)
        return [[{"type": "PERSON", "text": "Emily Johnson"}] if "Emily Johnson" in c else [] for c in cs]
    r = Redactor(Config.load(), entities=ents)
    r.value({"a": "menu item one"})
    out = r.value({"a": "menu item one", "b": "call Emily Johnson"})
    assert "Emily Johnson" not in str(out)


@pytest.mark.parametrize("name,kept", [("Look Up", True), ("look up", True), ("Up in", True), ("Dictionary", True),
                                       ("Look Down", False), ("Up", False), ("Look Up Dictionary", False)])
def test_a_run_of_whole_words_of_a_longer_name_is_protected(name, kept):
    """The tagger took "Look Up" of the Service 「Look Up in Dictionary」 for a person, and the floor, shown
    「⟦PERSON_1⟧ in Dictionary」, could not tell what the action did and stopped a real task to ask."""
    assert protector(APPS + ["Look Up in Dictionary"])._is_protected(name) is kept


def test_the_vocabulary_read_behind_the_first_look_still_counts():
    """Services are read in the background, after a task's first request: a redactor that fixed its list then
    would never have known them."""
    from macwork.config import Config
    from macwork.privacy import Redactor
    vocab = ["Mail"]
    r = Redactor(Config.load(), entities=lambda cs: [[{"type": "PERSON", "text": "Look Up"}] if "Look Up" in c else [] for c in cs],
                 protect=lambda: vocab)
    r.value({"app": "Mail"})                                  # the list is read here, before the Services are
    vocab.append("Look Up in Dictionary")
    assert r.value("「Look Up in Dictionary」 — a Service of Dictionary on this Mac").startswith("「Look Up in Dictionary」")


def test_the_macs_vocabulary_includes_the_services_its_apps_declare(tmp_path):
    from macwork.engine import Engine
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([]))
    assert "Look Up in Dictionary" not in eng._mac_vocabulary()
    eng.cache["services.all"] = (0.0, [{"name": "Look Up in Dictionary", "app": "Dictionary", "sends": ["NSStringPboardType"]}])
    assert "Look Up in Dictionary" in eng._mac_vocabulary() and "Calculator" in eng._mac_vocabulary()
