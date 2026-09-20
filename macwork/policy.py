"""What the engine may do at all — the part that does not depend on the goal being reachable:

* apps and actions that are denied outright (including the app the engine itself runs under),
* the safety floor: words find candidates, the decider classifies them (without seeing the screen), and only
  "it just navigates" releases one,
* what needs the caller's confirmation,
* the two questions answered from the user's words alone (does the goal call for this action, does leaving the
  app serve the goal) — deliberately blind to screen text, so instructions hidden in a page cannot answer them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .decider import DeciderError, choice, noul
from .model import Affordance, Task
from .observe import Ctx
from .privacy import Redactor

log = logging.getLogger(__name__)


class PolicyMixin:
    def _host_bundles(self) -> set[str]:
        """The apps this engine runs under (the terminal or client that started it), found by walking up the
        process tree once: typing there would type into the caller itself."""
        if "host.bundles" not in self.cache:
            import os
            import subprocess
            parents: dict[int, int] = {}
            try:
                out = str(getattr(subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False), "stdout", "") or "")
            except OSError:
                out = ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    parents[int(parts[0])] = int(parts[1])
            chain, pid = set(), os.getpid()
            while pid and pid not in chain and pid > 1:
                chain.add(pid)
                pid = parents.get(pid, 0)
            try:
                running = self.helper.call("apps.running")
            except Exception:  # noqa: BLE001
                running = []
            self.cache["host.bundles"] = {a["bundle_id"] for a in running if a.get("pid") in chain and a.get("bundle_id")}
        return self.cache["host.bundles"]

    def _denied(self, a: Affordance, app: dict[str, Any] | None) -> bool:
        deny = self.cfg.policy.get("deny", {}) or {}
        allow = self.cfg.policy.get("allow", {}) or {}
        bundle = a.target.get("bundle_id") or (app or {}).get("bundle_id")
        if bundle and bundle in (deny.get("bundle_ids") or []):
            return True
        if bundle and deny.get("host_app", True) and bundle in self._host_bundles():
            return True
        if bundle and allow.get("bundle_ids") and bundle not in allow["bundle_ids"]:
            return True
        text = f"{a.verb} {a.label} {a.context}"
        return any(re.search(rx, text) for rx in deny.get("patterns") or [])

    # ------------------------------------------------------------------ the safety floor
    def _floor_hits(self, a: Affordance) -> list[str]:
        """Floor categories whose words appear in the action: candidates, not verdicts."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        text = f"{a.verb} {a.label} {a.context}"
        hits = [name for name, c in (conf.get("categories") or {}).items()
                if any(re.search(rx, text) for rx in (c or {}).get("patterns") or [])]
        if any(re.search(rx, text) for rx in conf.get("patterns") or []):
            hits.append("other")
        return hits

    def _risky(self, a: Affordance) -> bool:
        """Words alone (conservative): used where no decider is involved — step-level acts, replays, exploring."""
        return bool(self._floor_hits(a))

    @staticmethod
    def _nondescript(a: Affordance) -> bool:
        """An action whose label does not say what it does: a click on read text, an unlabeled control, a button
        of a prompt from another process."""
        return a.channel == "pointer" or "(no label)" in a.label or "in front of the app" in a.context

    def _floor_key(self, ctx: Ctx, a: Affordance) -> str:
        return f"{(ctx.app or {}).get('bundle_id')}|{a.label}|{a.context}"

    def _floor_options(self, a: Affordance, hits: list[str]) -> dict[str, str]:
        conf = self.cfg.policy.get("confirm", {}) or {}
        cats = {k: str((v or {}).get("what") or k) for k, v in (conf.get("categories") or {}).items()}
        cats["other"] = "it does something irreversible or outward-facing"
        options = {"navigate": str(conf.get("navigate") or "it only opens, shows or navigates to something").strip()}
        if a.verb in ("type", "type_submit"):
            options["enter"] = str(conf.get("enter") or "it only enters text into a field or a document").strip()
        return options | ({k: cats[k] for k in hits if k in cats} if hits else {k: v for k, v in cats.items() if k != "other"})

    def _floor_questions(self, ctx: Ctx, affs: list[Affordance], window: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        """Every floor candidate on screen, classified in one request of its own — sent at the same time as the
        step's own request, so it costs no waiting, and with a state that holds no screen text, so nothing written
        on the page can argue an action out of the floor. Verdicts are remembered per app and label."""
        questions: dict[str, Any] = {}
        mapping: dict[str, str] = {}
        cache = self.cache.setdefault("floor.verdicts", {})
        limit = int(self.cfg.get("engine.floor_batch", 8))
        actions: list[dict[str, str]] = []
        for a in affs:
            if len(questions) >= limit:
                break
            hits = self._floor_hits(a)
            key = self._floor_key(ctx, a)
            if not hits or key in cache or key in mapping.values():
                continue
            qid = f"floor{len(questions)}"
            questions[qid] = choice(self.cfg.question("floor_what").replace("{action}", a.label), self._floor_options(a, hits))
            mapping[qid] = key
            actions.append({"action": a.label, "where": a.context, "kind": f"{a.channel} {a.verb}"})
        state = {"app": (ctx.app or {}).get("name"), "window": window, "actions": actions}
        return questions, state, mapping

    def _floor_answers(self, ans: dict[str, Any], mapping: dict[str, str]) -> None:
        cache = self.cache.setdefault("floor.verdicts", {})
        for qid, key in mapping.items():
            a = ans.get(qid) or {}
            probs = a.get("probabilities") or {}
            if a:
                cache[key] = (a.get("choice", ""), float(probs.get("navigate", 0.0)) + float(probs.get("enter", 0.0)))

    def _floor(self, task: Task, ctx: Ctx, a: Affordance, risky_screen: float, window: str | None = None,
               force: bool = False) -> list[str]:
        """The floor categories that gate this action ([] = none). Word hits are classified by the decider from the
        action and where it sits — no screen text, so page content cannot argue it out of the floor; only "it
        just navigates" at >= release_threshold releases it. Nondescript actions on a screen with some risk are
        classified the same way (they are gated when judged to do anything more than navigate). The classification
        usually rode along with the step's own request; only an action seen for the first time here asks again."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        hits = self._floor_hits(a)
        always = force or a.channel == "script_cmd"      # scripting commands act below the UI: always classified
        if not hits and not always and not (self._nondescript(a) and risky_screen >= float(conf.get("classify_nondescript_over", 0.3))):
            return []
        key = self._floor_key(ctx, a)
        cache: dict[str, tuple[str, float]] = self.cache.setdefault("floor.verdicts", {})
        if key not in cache:
            state = {"app": (ctx.app or {}).get("name"), "window": window, "action": a.label,
                     "where": a.context, "kind": f"{a.channel} {a.verb}"}
            q = self.cfg.question("floor_what").replace("{action}", a.label)
            try:
                ans = ctx.gate.decide(self.redactor(task.id), state, {"what": choice(q, self._floor_options(a, hits))}, task=task.id)
                probs = (ans.get("what") or {}).get("probabilities") or {}
                cache[key] = ((ans.get("what") or {}).get("choice", ""), float(probs.get("navigate", 0.0)) + float(probs.get("enter", 0.0)))
            except DeciderError:
                cache[key] = ("", 0.0)             # cannot tell: the words stand
        top, nav = cache[key]
        if hits:
            if nav >= float(conf.get("release_threshold", 0.9)):
                self.audit.record("floor", task=task.id, action=a.label, released=True, navigate=round(nav, 3), words=hits)
                return []
            return hits
        return [] if top in ("navigate", "enter", "") else [top]

    def _needs_confirm(self, a: Affordance, risky_screen: float, approved: set[str],
                       harmless: Callable[[Affordance], bool] | None = None, floor: list[str] | None = None) -> bool:
        """The safety floor always asks (``floor``: its categories for this action, as classified; by default the
        words alone). On a screen the decider judged risky, anything else asks too — unless the decider, asked
        about this very action, judges that it only backs out (closes, cancels, postpones) and commits nothing."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        if conf.get("mode", "caller") == "never" or a.label in approved:
            return False
        if self._floor_hits(a) if floor is None else floor:
            return True
        th = float(self.cfg.get("engine.thresholds.risky_screen", 0.6))
        exempt = set(conf.get("screen_gate_exempt") or ["web"])
        if risky_screen < th or a.channel in exempt:
            return False
        return not (harmless and harmless(a))   # pointer clicks included

    def _goal_state(self, task: Task, ctx: Ctx, a: Affordance | None = None) -> dict[str, Any]:
        """Only what the user said, where we are and the action in question: questions about what the goal wants
        must not be answerable by text on the screen — that is how instructions hidden in a page would get in."""
        out = {"goal": task.goal, "inputs": dict(task.inputs), "working_in": (ctx.app or {}).get("name")}
        if a is not None:
            out["action"] = a.label
        return out

    def _goal_calls_for(self, task: Task, ctx: Ctx, redactor: Redactor, state: dict[str, Any], a: Affordance) -> bool:
        """Asked only for an action the safety floor gates: does the goal itself call for it?"""
        if a.label in task.approved:
            return True
        q = self.cfg.question("goal_calls_for").replace("{action}", a.label)
        try:
            ans = ctx.gate.decide(redactor, self._goal_state(task, ctx, a), {"calls_for": noul(q)}, task=task.id)
        except DeciderError:
            return True    # cannot tell: let the caller decide
        return float(ans.get("calls_for", {}).get("noul", 1.0)) >= float(self.cfg.get("engine.thresholds.goal_calls_for", 0.3))

    def _serves_goal(self, task: Task, ctx: Ctx, a: Affordance) -> bool:
        """Leaving the app being worked in — opening or switching to another app, running a Shortcut, researching
        on the web — must make sense for the goal as the user stated it (judged without the screen's text)."""
        if a.label in task.approved or a.id == "launch":
            return True
        cache = task.memory.serves
        if a.label not in cache:
            q = self.cfg.question("serves_goal").replace("{action}", a.label)
            try:
                ans = ctx.gate.decide(self.redactor(task.id), self._goal_state(task, ctx, a), {"serves": noul(q)}, task=task.id)
                cache[a.label] = float(ans.get("serves", {}).get("noul", 1.0)) >= float(self.cfg.get("engine.thresholds.serves_goal", 0.35))
            except DeciderError:
                cache[a.label] = True
        return cache[a.label]

    def _backs_out(self, task: Task, ctx: Ctx, redactor: Redactor, state: dict[str, Any], a: Affordance) -> bool:
        """Asked only on a screen judged risky: does this particular action just back out of it?"""
        q = self.cfg.question("backs_out").replace("{action}", a.label)
        try:
            ans = ctx.gate.decide(redactor, state, {"backs_out": noul(q)}, task=task.id)
        except DeciderError:
            return False
        return float(ans.get("backs_out", {}).get("noul", 0.0)) >= float(self.cfg.get("engine.thresholds.backs_out", 0.8))
