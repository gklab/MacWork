"""Carrying out an affordance and seeing what it did: waiting for the UI (speculatively), window fingerprints,
windows that change by themselves, and the menu key-equivalent fallback."""

from __future__ import annotations

import logging
import time
from typing import Any

from .act import NotInFront, Outcome, get_channel
from .helper import HelperError
from .model import Affordance, Observation, Task
from .observe import Ctx
from .onscreen import app_readiness

log = logging.getLogger(__name__)


class EffectsMixin:
    def _await_app(self, task: Task, ctx: Ctx) -> dict[str, Any] | None:
        """Before a look: wait for an app that is starting or busy to answer, rather than read it blind.

        'open app' returns once the process is listed and the wait after an action ends at the first event,
        so the look after an open came while the app could not be read: all 10 in-loop opens since 22ce357
        were followed by a blind look. A blind look was asked about all the same: of 158, the decider voted
        rethink on 144 and wait on none, and 53 plans were made right after one — on 09-23 three of them
        began by opening Calculator's or Dictionary's window.

        The first question is asked as any look asks it (the helper does not ask an app it has marked silent
        again within its wait); only when the app is not ready does the look wait, asking now (`ask_now`)
        every `engine.ready_poll_ms`, until it is ready, the task is cancelled, or `engine.open_front_s` has
        passed. Ready is answering, and launched or showing a window. An app that stays silent that long is
        not waited for again until it answers once (`TaskScope.silent`), and each wait is one of the ledger's
        `ready_waits`, apart from the decider's own waits. A helper older than 0.2.0 cannot be asked at once
        and keeps its 20 s cooldown whatever it is asked: polling it would only add open_front_s to every
        launch, so there is no wait at all. {why: starting | busy, ms, answered}, or None: no wait."""
        pid = (ctx.app or {}).get("pid")
        if not pid:
            return None
        bound = float(self.cfg.get("engine.open_front_s", 6))
        size = self.cfg.get("observe.windows.min_size") or [100, 60]

        def readiness(now: bool) -> dict[str, Any]:
            return app_readiness(self.helper, pid, ctx.running, ask_now=now, launch_bound_s=bound,
                                 min_size=(int(size[0]), int(size[1])))
        state, silent = readiness(False), ctx.scope.silent
        if state["ready"]:
            silent.discard(pid)
            return None
        if pid in silent:
            return None
        if not state["can_ask_now"]:
            silent.add(pid)          # the older helper: nothing to wait for until its cooldown lets the app answer
            return None
        if not self.spend_allowance(task, "ready_waits"):
            return None
        why = "starting" if state["launching"] else "busy"
        poll = max(0.0, float(self.cfg.get("engine.ready_poll_ms", 200)) / 1000)
        t0, answered = time.monotonic(), False
        while task.id not in self._cancelled:
            if readiness(True)["ready"]:
                answered = True
                break
            if time.monotonic() - t0 >= bound:
                silent.add(pid)
                break
            time.sleep(poll)
        ms = round((time.monotonic() - t0) * 1000)
        name = ctx.app.get("name") or ctx.app.get("bundle_id") or pid
        log.info("waited %d ms for %s (%s) to answer: %s", ms, name, why, "it did" if answered else "it did not")
        self.audit.record("ready_wait", task=task.id, app=ctx.app.get("bundle_id"), why=why, ms=ms, answered=answered)
        return {"why": why, "ms": ms, "answered": answered}

    def _execute(self, ctx: Ctx, a: Affordance, params: dict[str, Any]) -> tuple[Outcome, list[str]]:
        ch = get_channel(a.channel)
        if ch is None:
            return Outcome(False, error=f"no channel '{a.channel}'"), []
        v = self.cfg.section("engine.verify")
        poll_ms, poll_nodes = int(v.get("poll_ms", 0)), int(v.get("poll_nodes", 400))
        baseline, base_pid = None, (ctx.app or {}).get("pid")
        if poll_ms and base_pid:   # the window's content before acting: UIs that post no notifications still change
            baseline = self._recent_fingerprint(ctx)     # the judge took one a moment ago, of this same screen
            if baseline is None:
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
                                     settle_ms=0 if spec else int(v.get("settle_ms", 150)), timeout=30, **extra)
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
        a = self._recent_fingerprint(ctx)
        if a is None:
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
            fp = self.helper.call("ax.fingerprint", pid=ctx.app["pid"],
                                  poll_nodes=int(self.cfg.get("engine.verify.poll_nodes", 400))).get("fingerprint")
        except HelperError:
            return None
        ctx.cache["screen.fp"] = (ctx.app["pid"], fp)   # the menus are cached against this; taking it again would cost a round trip
        ctx.cache["screen.fp_at"] = (ctx.app["pid"], fp, time.monotonic())
        return fp

    def _recent_fingerprint(self, ctx: Ctx) -> int | None:
        """The fingerprint taken a moment ago of this same app, or None. A step took it three times — after the
        look, after the decision, before the action — and the last two are of one screen, milliseconds apart.
        Where the caller wants to know whether the screen *moved*, it asks for a fresh one; where it wants a
        baseline to move from, the recent one is the same baseline."""
        if not ctx.app:
            return None
        pid, fp, at = ctx.cache.get("screen.fp_at") or (None, None, 0.0)
        within = float(self.cfg.get("engine.verify.fingerprint_reuse_ms", 300)) / 1000
        return fp if pid == ctx.app["pid"] and time.monotonic() - at < within else None
