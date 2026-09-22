"""The wire between the engine and the helper: one question, one answer, matched by id.

No real helper is started — a socket pair stands in for it, so these drive the protocol itself rather than a
fake `call`. What they protect: a call that timed out leaves its reply in the stream, and if the next call
takes that line as its own answer, every later call is one question behind and nothing says so — the engine
would decide on the window it saw a step ago.
"""

import json
import socket

import pytest

from macwork.helper import Helper, HelperError
from tests.test_engine import cfg


@pytest.fixture
def wire(tmp_path):
    """A Helper whose socket is the other end of one the test holds."""
    theirs, ours = socket.socketpair()
    helper = Helper(cfg(tmp_path))
    helper._sock, helper.mode = theirs, "socket"
    yield helper, ours
    ours.close()
    theirs.close()


def reply(sock, id: int, result) -> None:
    sock.sendall(json.dumps({"id": id, "result": result}).encode() + b"\n")


def test_a_reply_left_over_from_an_earlier_call_is_dropped(wire):
    helper, helper_side = wire
    reply(helper_side, 0, {"window": "the screen a step ago"})   # never collected by whoever asked for it
    reply(helper_side, 1, {"window": "the screen now"})
    assert helper.call("apps.frontmost") == {"window": "the screen now"}


def test_a_timeout_does_not_shift_every_later_call(wire):
    helper, helper_side = wire
    with pytest.raises(HelperError) as raised:
        helper.call("ax.snapshot", timeout=0.05)      # nothing answers in time
    assert raised.value.code == "timeout"

    reply(helper_side, 1, {"nodes": ["too late"]})    # the answer to the call that gave up
    reply(helper_side, 2, {"nodes": ["the real answer"]})
    assert helper.call("ax.snapshot") == {"nodes": ["the real answer"]}


def test_an_error_reply_is_matched_by_id_too(wire):
    helper, helper_side = wire
    reply(helper_side, 0, {"nodes": []})
    helper_side.sendall(json.dumps({"id": 1, "error": {"code": "stale_ref", "message": "gone"}}).encode() + b"\n")
    with pytest.raises(HelperError) as raised:
        helper.call("ax.perform")
    assert raised.value.code == "stale_ref"


def test_the_background_connection_reports_a_dead_helper_too(tmp_path):
    """Two connections, one helper: when it dies, the element references handed to the foreground are
    gone as well. The second connection had no `on_reset`, so a crash met there left every cache believed
    valid. It is forwarded, not copied — the engine sets its handler after the helper object exists."""
    helper = Helper(cfg(tmp_path))
    bg = helper.background()
    dropped = []
    helper.on_reset = lambda: dropped.append("caches dropped")     # set afterwards, as the engine does
    bg._reset()
    assert dropped == ["caches dropped"]
