"""Client for macwork-helper, the signed native process that holds the macOS permissions.

Modes (``helper.mode``):
* ``socket``  talk to the installed MacWork Helper.app over its 0600 Unix socket (launched on demand)
* ``stdio``   spawn the binary as a child (development: it inherits the terminal's permissions)
* ``auto``    socket if the app is installed or already running, else stdio
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config, expand

log = logging.getLogger(__name__)

DEV_BINARY = Path(__file__).resolve().parents[1] / "helper" / ".build" / "release" / "macwork-helper"


class HelperError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Helper:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0
        self.mode: str | None = None
        self.on_reset: Callable[[], None] | None = None   # set by the engine: a new helper invalidates its refs
        self._bg: Helper | None = None
        self._bg_lock = threading.Lock()

    # ------------------------------------------------------------------ connect
    def _binary(self) -> Path | None:
        for p in (expand(self.cfg.get("helper.binary")), DEV_BINARY,
                  (expand(self.cfg.get("helper.app")) or Path("/nonexistent")) / "Contents" / "MacOS" / "macwork-helper"):
            if p and p.exists():
                return p
        return None

    def _connect_socket(self, path: Path) -> bool:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(path))
        except OSError:
            return False
        self._sock, self.mode = s, "socket"
        return True

    def start(self) -> None:
        if self.mode:
            return
        mode = self.cfg.get("helper.mode", "auto")
        sock = expand(self.cfg.get("helper.socket"))
        app = expand(self.cfg.get("helper.app"))
        if mode in ("auto", "socket") and sock and sock.exists() and self._connect_socket(sock):
            return
        if mode in ("auto", "socket") and app and app.exists() and sock:
            sock.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", "-g", "-j", "-a", str(app), "--args", "--socket", str(sock)], check=False)
            deadline = time.monotonic() + float(self.cfg.get("helper.start_timeout_s", 6))
            while time.monotonic() < deadline:
                if sock.exists() and self._connect_socket(sock):
                    return
                time.sleep(0.1)
            if mode == "socket":
                raise HelperError("start", f"{app} did not open {sock}")
        if mode == "socket":
            raise HelperError("start", "helper app not installed: run `macwork helper install`")
        binary = self._binary()
        if binary is None:
            raise HelperError("start", "macwork-helper not built: run `macwork helper build`")
        self._proc = subprocess.Popen([str(binary), "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.mode = "stdio"

    def background(self) -> "Helper":
        """A second connection, for work that must not hold up the look.

        `call` holds one lock for the whole round trip, so a "background" refresh sharing this object stops
        the foreground dead. Measured: enumerating the menu bar's owners took 1516 ms in its own thread and
        the next provider to ask for anything waited 1717 ms for it — the refresh was moved off the critical
        path and then blocked it anyway, which is worse than not having moved it.

        In socket mode this is another connection to the same helper; in stdio mode it is a second child.
        Either way the helper serves each connection on its own thread, so only the methods that need the
        main thread still queue against each other.
        """
        with self._bg_lock:
            if self._bg is None:
                self._bg = Helper(self.cfg)
                # In socket mode both connections are one helper: when it dies, every element reference the
                # foreground holds is gone too, and the second connection had no `on_reset`, so a crash met
                # there left every cache believed valid. (In stdio mode it is a second child, and dropping
                # the caches is only over-cautious.) Forwarded, not copied: the engine sets its handler
                # after this object exists.
                self._bg.on_reset = self._forward_reset
            return self._bg

    def _forward_reset(self) -> None:
        if self.on_reset is not None:
            self.on_reset()

    def close(self) -> None:
        with self._bg_lock:
            if self._bg is not None:
                self._bg.close()
                self._bg = None
        with self._lock:
            if self._sock:
                self._sock.close()
            if self._proc:
                self._proc.terminate()
            self._sock = self._proc = None
            self.mode = None

    # --------------------------------------------------------------------- call
    def _send(self, data: bytes) -> None:
        if self._sock:
            self._sock.sendall(data)
        else:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(data)

    def _readline(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        fd = self._sock.fileno() if self._sock else self._proc.stdout.fileno()  # type: ignore[union-attr]
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise HelperError("timeout", "helper did not answer")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise HelperError("closed", "helper exited")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _reply_to(self, want_id: int, method: str, timeout: float) -> dict[str, Any]:
        """The reply to *this* request, matched by id.

        A call that timed out leaves its reply in the stream. Without matching ids the next call reads that
        stale line and returns it as its own answer — and every call after it is one question behind, silently:
        the engine would act on the window it saw a step ago. Late replies are dropped here, not returned.
        """
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HelperError("timeout", f"helper did not answer {method}")
            reply = json.loads(self._readline(left))
            if reply.get("id") == want_id:
                return reply
            log.warning("helper: dropped a late reply to #%s while waiting for #%s (%s)",
                        reply.get("id"), want_id, method)

    def _reset(self) -> None:
        """Forget a helper that died (its pipe broke or it exited); the next call starts a fresh one.

        Loudly. Restarting is right — a crash should not take the task with it — but doing it in silence
        hides the crash itself: a helper that died on every file event looked like "file events never
        arrive", and anything it was holding (subscriptions, element references) was gone with no sign.
        """
        if self.mode:
            log.warning("helper died and is being restarted: anything it was holding is gone")
        try:
            if self._sock:
                self._sock.close()
            if self._proc:
                self._proc.kill()
        except OSError:
            pass
        self._sock = self._proc = None
        self._buf = b""
        self.mode = None
        if self.on_reset is not None:   # every element reference the old process handed out is dead with it
            try:
                self.on_reset()
            except Exception:  # noqa: BLE001  (a failed cache drop must not mask the helper's own failure)
                log.warning("on_reset failed", exc_info=True)

    def call(self, method: str, timeout: float = 30.0, **params: Any) -> Any:
        with self._lock:
            for attempt in (1, 2):   # a helper that crashed is restarted once; the call is not lost with it
                if not self.mode:
                    self.start()
                self._id += 1
                try:
                    self._send(json.dumps({"id": self._id, "method": method, "params": params}, ensure_ascii=False).encode() + b"\n")
                    reply = self._reply_to(self._id, method, timeout)
                    break
                except (BrokenPipeError, ConnectionError, OSError) as exc:
                    self._reset()
                    if attempt == 2:
                        raise HelperError("closed", f"helper died: {exc}") from exc
                except HelperError as exc:
                    if exc.code != "closed" or attempt == 2:
                        raise
                    self._reset()
        if "error" in reply:
            e = reply["error"] or {}
            raise HelperError(str(e.get("code", "error")), str(e.get("message", "")))
        return reply.get("result")
