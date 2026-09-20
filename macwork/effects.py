"""Carrying out an affordance and seeing what it did: waiting for the UI (speculatively), window fingerprints,
windows that change by themselves, and the menu key-equivalent fallback."""

from __future__ import annotations

import logging
import time
from typing import Any

from .act import NotInFront, Outcome, get_channel
from .helper import HelperError
from .model import Affordance, Observation
from .observe import Ctx

log = logging.getLogger(__name__)


class EffectsMixin:
    def _execute(self, ctx: Ctx, a: Affordance, params: dict[str, Any]) -> tuple[Outcome, list[str]]:
        ch = get_channel(a.channel)
        if ch is None:
            return Outcome(False, error=f"no channel '{a.channel}'"), []
        v = self.cfg.section("engine.verify")
        poll_ms, poll_nodes = int(v.get("poll_ms", 0)), int(v.get("poll_nodes", 400))
        baseline, base_pid = None, (ctx.app or {}).get("pid")
        if poll_ms and base_pid:   # the window's content before acting: UIs that post no notifications still change
            try:
                baseline = self.helper.call("ax.fingerprint", pid=base_pid, poll_nodes=poll_nodes).get("fingerprint")
            except HelperError:
                baseline = None
        windows_before = self._window_titles(ctx) if a.channel == "menu" and a.target.get("combo") else None
        t0 = time.monotonic()
        self.cache["actions_done"] = self.cache.get("actions_done", 0) + 1   # invalidates cached menus
        try:
            out = ch(ctx, a, params)
        except (HelperError, NotInFront) as exc:
            if isinstance(exc, HelperError) and exc.code == "stale_ref":
                self.cache.pop("menu.snap", None)   # a cached tree outlived its elements: rescan next time
            return Outcome(False, error=str(exc)), []
        self.last_timing = {"act": round((time.monotonic() - t0) * 1000)}
        t1 = time.monotonic()

        # speculating: stop waiting early (the decision that follows is checked against the screen when it returns)
        spec = bool(v.get("speculate", True)) and poll_ms > 0
        quiet_ms = int(v.get("speculate_quiet_ms", 80) if spec else v.get("quiet_ms", 300))
        front = (self.helper.call("apps.frontmost").get("app") or {}) if spec else {}
        if spec and f"{front.get('pid')}|{front.get('window')}" in self.cache.get("ambient", set()):
            quiet_ms = 0   # it never goes quiet by itself: the first reaction is all there is to wait for
        timeout_ms = int(v.get("speculate_timeout_ms", 800) if spec else v.get("timeout_ms", 2500))

        def wait() -> list[str]:
            if not (out.ok and out.wait and out.watch_pid):
                return []
            try:
                extra = {"poll_ms": poll_ms, "poll_nodes": poll_nodes, "quiet_ms": quiet_ms} if poll_ms else {}
                if poll_ms and baseline is not None and out.watch_pid == base_pid:
                    extra["baseline"] = baseline
                w = self.helper.call("ax.wait", pid=out.watch_pid, timeout_ms=timeout_ms,
                                     settle_ms=0 if spec else int(v.get("settle_ms", 250)), timeout=30, **extra)
                return [e["name"] for e in w.get("events", [])]
            except HelperError as exc:
                return [f"wait failed: {exc.code}"]
        events = wait()
        combo = a.target.get("combo") if a.channel == "menu" else None
        if combo and out.ok and self._menu_had_no_effect(ctx, windows_before, events):
            # some apps (Electron) ignore a pressed menu item but honour its own key equivalent: same command, other hand
            try:
                get_channel("keys")(ctx, Affordance(a.id, "keys", "key", a.label, {"combo": combo}), {})
                events = wait()
                if events:
                    events = ["(by its shortcut)"] + events
            except (HelperError, NotInFront):
                pass
        self.last_timing["wait"] = round((time.monotonic() - t1) * 1000)
        self.last_timing["events"] = len([x for x in events if not x.startswith("wait failed")])
        return out, events

    def _window_titles(self, ctx: Ctx) -> list[str] | None:
        if not ctx.app:
            return None
        try:
            nodes = self.helper.call("ax.snapshot", pid=ctx.app["pid"], scope="windows", max_depth=0, max_nodes=30, actions=False).get("nodes", [])
        except HelperError:
            return None
        return sorted(str(n.get("title") or "") for n in nodes)

    def _menu_had_no_effect(self, ctx: Ctx, windows_before: list[str] | None, events: list[str]) -> bool:
        """Did a pressed menu command do nothing? No reaction at all — or the windows are the same and the only
        change is the window's own live content (meters, clocks), measured on the spot."""
        real = [x for x in events if not x.startswith("wait failed")]
        if not real:
            return True
        if windows_before is None or self._window_titles(ctx) != windows_before:
            return False
        front = (self.helper.call("apps.frontmost").get("app") or {})
        return self._moves_by_itself(ctx, Observation(app=ctx.app, window=front.get("window"), affordances=[]))

    def _still_moving(self, ctx: Ctx) -> bool:
        """Before a final "done" after a speculative wait: let the window go quiet (as without speculating) and say
        whether it changed meanwhile — then the judgement is made again on what it settled into."""
        v = self.cfg.section("engine.verify")
        if not v.get("speculate", True) or not ctx.app or not int(v.get("poll_ms", 0)):
            return False
        before = self._fingerprint(ctx)
        try:
            self.helper.call("ax.wait", pid=ctx.app["pid"], timeout_ms=int(v.get("done_settle_ms", 1200)), settle_ms=0,
                             poll_ms=int(v.get("poll_ms", 80)), poll_nodes=int(v.get("poll_nodes", 400)),
                             quiet_ms=int(v.get("quiet_ms", 300)), baseline=before, timeout=30)
        except HelperError:
            return False
        after = self._fingerprint(ctx)
        return before is not None and after is not None and after != before

    def _moves_by_itself(self, ctx: Ctx, obs: Observation) -> bool:
        """Does this window change with nobody acting (a clock, live statistics)? Measured once, then remembered:
        such a window never looks settled, so checking decisions against it would only waste them."""
        a = self._fingerprint(ctx)
        time.sleep(float(self.cfg.get("engine.verify.ambient_probe_s", 0.15)))
        if a is None or self._fingerprint(ctx) == a:
            return False
        self.cache.setdefault("ambient", set()).add(f"{(ctx.app or {}).get('pid')}|{obs.window}")
        return True

    def _fingerprint(self, ctx: Ctx) -> int | None:
        if not ctx.app or not self.cfg.get("engine.verify.speculate", True):
            return None
        try:
            return self.helper.call("ax.fingerprint", pid=ctx.app["pid"], poll_nodes=int(self.cfg.get("engine.verify.poll_nodes", 400))).get("fingerprint")
        except HelperError:
            return None
