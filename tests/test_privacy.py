"""The door to the decider, and the record of what went through it.

What these protect:
* a redactor that could not check for personal data used to keep going with every string replaced by
  "[withheld]" — the task stayed alive and decided blind. It must stop instead.
* the audit log holds redacted screen text, and redaction is best effort, so it must not be world-readable
  and must not grow until the disk is full.
* the floor classification runs in its own thread beside the step's own request, on the *same* pseudonym
  table. Without a lock that table is written while it is being iterated.
"""

import json
import threading

import pytest

from macwork.engine import Engine
from macwork.privacy import Audit, Gate, RedactionError, Redactor
from tests.test_engine import FakeHelper, ScriptedDecider, cfg, names_in


class CountingDecider(ScriptedDecider):
    pass


def test_the_gate_sends_nothing_when_tagging_failed(tmp_path):
    c = cfg(tmp_path)
    decider = CountingDecider([])
    gate = Gate(decider, Audit(c))
    broken = Redactor(c, entities=lambda texts: 1 / 0)

    with pytest.raises(RedactionError):
        gate.decide(broken, {"screen_text": "Hello Alice"}, {"q": {"type": "noul", "instructions": "?"}})
    assert decider.calls == 0, "the request must not reach the decider"


def test_a_task_stops_instead_of_deciding_on_withheld_text(tmp_path):
    """The engine turns it into a plain failed task, not a traceback at the caller."""
    engine = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([{"pick": "done", "done": 0.95}]))
    engine._entities = lambda texts: 1 / 0            # on-device tagging is unavailable

    result = engine.do("see what the screen says")
    assert result["status"] == "failed"
    assert "nothing could be sent" in result["reason"]


def test_the_audit_log_is_private_and_rotates(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = Audit(cfg(tmp_path, config={"audit": {"path": str(path), "max_bytes": 400, "keep": 2}}))

    audit.record("decide", state={"screen_text": "x"})
    assert path.stat().st_mode & 0o777 == 0o600

    for _ in range(40):
        audit.record("decide", state={"screen_text": "y" * 50})
    assert path.with_suffix(".1.jsonl").exists()
    assert not path.with_suffix(".3.jsonl").exists(), "keep=2 means two old files, no more"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["kind"] == "decide"


def test_one_pseudonym_table_survives_two_threads(tmp_path):
    """The floor thread and the main loop share the task's redactor."""
    redactor = Redactor(cfg(tmp_path), entities=names_in)
    texts = [f"record {n} of Zhang Wei, and Safari as well" for n in range(200)]
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for t in texts:
                redactor.value({"screen_text": t, "nested": [t, {"again": t}]})
                redactor.restore(redactor.text(t))
        except BaseException as exc:      # noqa: BLE001  (any raise at all is the failure)
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[0]
    assert redactor.text("Zhang Wei") == redactor.text("Zhang Wei"), "the same person keeps the same token"


# --- what is redacted, in languages the patterns were never written for --------------------------------

def test_phone_numbers_are_found_wherever_they_are_from(tmp_path):
    """The hand-written pattern knew mainland-Chinese mobiles and North American numbers, and nothing else."""
    from macwork.privacy import Redactor

    numbers = {"+49 30 901820": "Germany", "03-1234-5678": "Japan", "+55 11 91234-5678": "Brazil"}

    def detector(texts):   # what NSDataDetector returns for these, checked against the real one
        return [[{"type": "PHONE", "text": n} for n in numbers if n in t] for t in texts]

    redactor = Redactor(cfg(tmp_path), entities=names_in, detect=detector)
    for number, where in numbers.items():
        sent = redactor.text(f"Call {number} about it")
        assert number not in sent, f"the number from {where} leaked: {sent}"


def test_the_patterns_still_catch_what_the_detector_misses(tmp_path):
    """Both sources, not one: NSDataDetector does not find a German street, the pattern finds a Chinese one."""
    from macwork.privacy import Redactor

    redactor = Redactor(cfg(tmp_path), entities=names_in, detect=lambda texts: [[] for _ in texts])
    assert "北京市朝阳区建国路88号" not in redactor.text("寄送地址：北京市朝阳区建国路88号")


def test_text_in_any_script_reaches_the_tagger(tmp_path):
    """Not that the tagger knows every language — it does not — but that nothing is skipped before it."""
    from macwork.privacy import _nameish, _runs

    for text in ("Иван Петров", "김민준", "Σοφία", "محمد العلي", "山田太郎", "Emily"):
        assert _nameish(text), text
    assert not _nameish("3 + 4 = 7")

    assert _runs("Meeting with 王芳 at 3pm") == ["Meeting with", "王芳", "at", "pm"]
    assert _runs("发送给 Grace Lee") == ["发送给", "Grace Lee"]


def test_a_version_number_glued_to_a_letter_is_not_an_address(tmp_path):
    """RayLink's About window says V8.1.3.8; the engine answered ⟦IP_1⟧."""
    from macwork.privacy import Redactor
    from tests.test_engine import cfg
    r = Redactor(cfg(tmp_path), entities=lambda ts: [[] for _ in ts])
    assert r.text("About RayLink / V8.1.3.8 / About") == "About RayLink / V8.1.3.8 / About"
    assert "⟦IP_1⟧" in r.text("connected to 10.0.0.7 on port 22")


# --- what was left in place ---------------------------------------------------------------------------

def test_what_was_left_in_place_is_counted_by_kind(tmp_path):
    """A value is not replaced inside a longer word, nor where it would cut a name of this Mac's in two. What
    was left in place is counted on every request, by kind, and so is what was replaced although glued: a
    person left in place must show."""
    found = {"a Service of Saf": [("Saf", "PERSON")], "来自北京的消息": [("北京", "PLACE")],
             "I met menu Keynote yesterday.": [("menu Keynote", "PERSON")]}   # as the on-device tagger reads them
    r = Redactor(cfg(tmp_path), protect=lambda: ["Keynote讲演"],
                 entities=lambda ts: [[{"type": k, "text": v} for v, k in found.get(t, [])] for t in ts])
    r.text("a Service of Saf")
    tally: dict = {}
    sent = r.value(["open app Safari", "来自北京的消息", "menu Keynote讲演 ▸ 设置…"], tally=tally)
    assert sent == ["open app Safari", "来自⟦PLACE_1⟧的消息", "menu Keynote讲演 ▸ 设置…"]
    assert tally == {"glued": {"PERSON": 1}, "replaced_glued": {"PLACE": 1}, "in_a_name": {"PERSON": 1}}


# --- privacy-check ------------------------------------------------------------------------------------

def test_privacy_check_writes_nothing_to_the_real_audit(tmp_path):
    """Its through-the-gate pass wrote every corpus request into the engine's audit log, as a task called ''."""
    from macwork import privacycheck
    c = cfg(tmp_path)
    res = privacycheck.run(c, lambda ts: [[] for _ in ts], ["Safari"])
    assert res["through_the_gate"]["requests"] == len(privacycheck.corpus())
    assert not (tmp_path / "audit.jsonl").exists()
