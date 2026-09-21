"""Exploring an app ahead of time: only what the decider judges to just show or navigate, undone each time."""

from __future__ import annotations

import logging
import time
from typing import Any

from .act import get_channel
from .decider import DeciderError, choice, noul
from .model import Affordance, Observation
from .loop import Progress
from .observe import Ctx, arrange, get_provider, observe

log = logging.getLogger(__name__)


class LearnMixin:
    # --------------------------------------------------------------- learning an app ahead of time
    def learn(self, app: str, progress: Progress | None = None) -> dict[str, Any]:
        """Explore an app safely — only what the decider judges to just show or navigate (never the safety floor's
        risky actions) — undoing each step with Escape or by closing the window it opened, and record where
        everything leads."""
        progress = progress or (lambda _m: None)
        lc = self.cfg.section("learn")
        t0 = time.monotonic()
        with self._lock:
            running = self.helper.call("apps.running")
            target = self._resolve_app(app, running)
            if target is None:
                key = app.casefold()
                inst = next((a for a in installed_apps(self.cfg, self.helper)
                             if key in (a.get("name", "").casefold(), a.get("file", "").casefold(), str(a.get("bundle_id", "")).casefold())), None)
                if inst is None:
                    return {"error": f"no app called {app}"}
                opened = get_channel("app")(Ctx(self.cfg, self.helper, cache=self.cache), Affordance("x", "app", "open", "", inst), {})
                if not opened.ok or not opened.target:
                    return {"error": opened.error or "could not open the app"}
                target = opened.target
                time.sleep(1.0)
            self.helper.call("apps.activate", pid=target["pid"])
            time.sleep(0.4)
            channels = set(lc.get("channels") or ["menu", "window"])
            tried: set[str] = set()
            explored, restored_fail = 0, 0
            providers = ["menu", "window", "popups"]
            cfg_obs = dict(self.cfg.docs["config"].get("observe", {}))

            def look() -> tuple[Ctx, Observation, str]:
                self.cfg.docs["config"]["observe"] = {**cfg_obs, "providers": providers}
                try:
                    c = self._ctx("", {}, target, "learn")
                    o = observe(c)
                finally:
                    self.cfg.docs["config"]["observe"] = cfg_obs
                return c, o, self._signature(c.app, o)

            ctx, obs, base = look()
            base_window = obs.window
            self.models.see(ctx.app, base, obs.window, [a.label for a in obs.affordances if a.channel == "window"][:12])
            while explored < int(lc.get("max_actions", 30)) and time.monotonic() - t0 < float(lc.get("budget_s", 180)):
                cands = self._safe_to_explore(ctx, obs, [a for a in obs.affordances if a.channel in channels and a.label not in tried
                                                         and not self._denied(a, ctx.app) and not self._risky(a, ctx) and not a.slots])
                if not cands:
                    break
                a = cands[0]
                tried.add(a.label)
                progress(f"try: {a.label}")
                out, events = self._execute(ctx, a, {})
                ctx2, obs2, sig2 = look()
                self.models.see(ctx2.app, sig2, obs2.window, [x.label for x in obs2.affordances if x.channel == "window"][:12])
                self.models.record(ctx.app, base, a.label, sig2, out.ok, sig2 != base or bool(events))
                explored += 1
                for _ in range(int(lc.get("max_back_steps", 2))):   # undo: the decider picks what gets back
                    if sig2 == base:
                        break
                    back = self.vetted("learn", ctx2, self._way_back(ctx2, obs2, base_window, a.label), obs2.window)
                    if back is None:
                        break
                    progress(f"back: {back.label}")
                    self._execute(ctx2, back, {})
                    ctx2, obs2, sig2 = look()
                if sig2 != base:
                    restored_fail += 1
                    if restored_fail >= int(lc.get("max_unrestored", 2)):
                        break
                ctx, obs, base = look() if sig2 != base else (ctx2, obs2, sig2)
            self.models.save(ctx.app)
            m = self.models.get(ctx.app) or {}
        return {"app": (ctx.app or {}).get("name"), "tried": explored, "screens": len(m.get("screens", {})),
                "edges": len(m.get("edges", [])), "scriptable_commands": len(m.get("sdef", {}).get("commands", [])),
                "seconds": round(time.monotonic() - t0, 1)}

    def _way_back(self, ctx: Ctx, obs: Observation, window: str | None, done: str) -> Affordance | None:
        """After exploring, which action on this screen returns to where we were, changing nothing?

        The decider chooses among what is there (keys included). `_risky` narrows the pool cheaply, but it
        does not ask the decider and falls back to a word list two languages wide — so the *pick* is gated
        by the floor proper at the call site (`vetted`), not only the pool."""
        extra = Observation(app=obs.app, window=obs.window, affordances=[])
        keys_provider = get_provider("keys")
        if keys_provider:
            keys_provider(ctx, extra)
        pool = [a for a in obs.affordances + extra.affordances if not a.slots and not self._risky(a, ctx) and not self._denied(a, ctx.app)]
        flat, _folded = arrange(pool, int(self.cfg.get("engine.max_options", 200)) - 1, set())
        if not flat:
            return None
        options = {"none": "none of these gets back without changing anything"} | {a.id: a.describe() for a in flat}
        q = self.cfg.question("way_back").replace("{action}", done).replace("{window}", window or "the previous window")
        try:
            ans = self.gate.decide(self.redactor("learn"), {"app": (ctx.app or {}).get("name"), "window": obs.window,
                                                           "screen_text": obs.screen_text[:600]}, {"back": choice(q, options)}, task="learn")
        except DeciderError:
            return None
        key = (ans.get("back") or {}).get("choice")
        return next((a for a in flat if a.id == key), None)

    def _safe_to_explore(self, ctx: Ctx, obs: Observation, cands: list[Affordance]) -> list[Affordance]:
        """Which of these only show or navigate? The decider judges each (in batches, one request per batch);
        verdicts are cached per app+label, so re-looking at a screen costs nothing."""
        lc = self.cfg.section("learn")
        cache: dict[str, bool] = self.cache.setdefault("learn.safe", {})
        app = (ctx.app or {}).get("bundle_id") or ""
        todo = [a for a in cands if f"{app}|{a.label}" not in cache]
        q = self.cfg.question("explore_safe")
        batch = max(1, int(lc.get("judge_batch", 16)))
        state = {"app": (ctx.app or {}).get("name"), "window": obs.window, "screen_text": obs.screen_text[:600]}
        for i in range(0, min(len(todo), int(lc.get("max_judged", 160))), batch):
            chunk = todo[i:i + batch]
            try:
                ans = self.gate.decide(self.redactor("learn"), state, {f"s{j}": noul(q.replace("{action}", a.label)) for j, a in enumerate(chunk)}, task="learn")
            except DeciderError as exc:
                log.info("learn: %s", exc)
                break
            for j, a in enumerate(chunk):
                cache[f"{app}|{a.label}"] = float(ans.get(f"s{j}", {}).get("noul", 0.0)) >= float(lc.get("safe_threshold", 0.8))
        return [a for a in cands if cache.get(f"{app}|{a.label}")]
