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
from macwork.helper import HelperError
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


# --- numbers that are not phone, card or ID numbers ----------------------------------------------------

def test_the_digits_after_a_decimal_point_are_not_a_card_or_phone_number(tmp_path):
    """A ruler mark read 「20.⟦CARD_1⟧」 and a stepper 「0.⟦CARD_1⟧」: 6,401 times in 79 tasks the decider was
    shown a pseudonym where a number was."""
    r = Redactor(cfg(tmp_path), entities=lambda ts: [[] for _ in ts])
    for number in ("20.31746031746032", "0.27000121772289", "12.13812345678"):
        assert r.text(f"value {number}") == f"value {number}", number
    assert "4111 1111 1111 1111" not in r.text("Card ending in 4111 1111 1111 1111")
    assert "13812345678" not in r.text("call 13812345678")


def test_a_csv_row_after_a_number_still_hides_its_phone_and_card(tmp_path):
    """After a comma the digits are the next field of a row, not the rest of a number — and the system's
    detector finds none of these, so the patterns are all there is."""
    r = Redactor(cfg(tmp_path), entities=lambda ts: [[] for _ in ts])
    for row, kind in (("1,13812345678", "PHONE"), ("3,4111111111111111", "CARD"), ("1,11010519491231002X", "IDNUM"),
                      ("12,5500-0000-0000-0004", "CARD")):
        field = row.split(",", 1)[1]
        sent = r.text(row)
        assert field not in sent and f"⟦{kind}_" in sent, sent


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


# --- this Mac's own identifiers -----------------------------------------------------------------------

SERIAL, UUID = "C02TESTSERIAL", "4A1B2C3D-1111-2222-3333-4444ABCDEF12"   # its middle reads as a card number


class Identified(FakeHelper):
    """A Mac that answers what its serial number and hardware UUID are — after `failures` errors."""

    def __init__(self, failures=0, **kw):
        super().__init__(**kw)
        self.failures = failures

    def call(self, method, timeout=30.0, **p):
        if method == "system.identity":
            self.calls.append((method, p))
            if self.failures:
                self.failures -= 1
                raise HelperError("no_method", "unknown method system.identity")
            return {"serial": SERIAL, "uuid": UUID}
        return super().call(method, timeout, **p)


def test_this_macs_own_serial_number_is_never_sent(tmp_path):
    """An About This Mac window left open put the serial number into 1,911 decider requests: no tagger calls it
    a name and no pattern knows its shape. The Mac is asked for it instead."""
    eng = Engine(cfg(tmp_path), helper=Identified(), decider=ScriptedDecider([]))
    sent = eng.redactor("t").value({"screen_text": f"Serial number {SERIAL}\nHardware UUID: {UUID}"})
    assert sent == {"screen_text": "Serial number ⟦DEVICE_1⟧\nHardware UUID: ⟦DEVICE_2⟧"}   # whole, never in pieces


def test_the_serial_number_is_asked_of_the_mac_once(tmp_path):
    h = Identified()
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    assert SERIAL not in eng.redactor("a").text(f"Serial number {SERIAL}")
    eng.redactor("b")
    assert len(h.did("system.identity")) == 1


def test_the_macs_identity_is_asked_again_after_the_helper_failed(tmp_path):
    """An older helper does not know the question. Kept as "nothing to hide", its error outlived the helper: an
    engine running on across a helper rebuild would have sent the serial number for the rest of its life."""
    h = Identified(failures=1)
    eng = Engine(cfg(tmp_path), helper=h, decider=ScriptedDecider([]))
    assert SERIAL in eng.redactor("a").text(f"Serial number {SERIAL}")      # an older helper: nothing to hide it by
    assert SERIAL not in eng.redactor("b").text(f"Serial number {SERIAL}")  # asked again, and answered
    eng.redactor("c")
    assert len(h.did("system.identity")) == 2                               # an answer is kept
    eng._helper_restarted()                                                 # …until the helper is a new one
    eng.redactor("d")
    assert len(h.did("system.identity")) == 3


# --- privacy-check ------------------------------------------------------------------------------------

def test_privacy_check_writes_nothing_to_the_real_audit(tmp_path):
    """Its through-the-gate pass wrote every corpus request into the engine's audit log, as a task called ''."""
    from macwork import privacycheck
    c = cfg(tmp_path)
    res = privacycheck.run(c, lambda ts: [[] for _ in ts], ["Safari"])
    assert res["through_the_gate"]["requests"] == len(privacycheck.corpus())
    assert not (tmp_path / "audit.jsonl").exists()
