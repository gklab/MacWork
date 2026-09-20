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

    result = engine.do("看看屏幕上写了什么")
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
    texts = [f"张伟 的第 {n} 条记录，还有 Safari" for n in range(200)]
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
    assert redactor.text("张伟") == redactor.text("张伟"), "the same person keeps the same token"
