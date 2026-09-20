"""What the engine may do at all — the part that does not depend on the goal being reachable:

* apps and actions that are denied outright (including the app the engine itself runs under),
* the safety floor: every action about to run is classified by the decider (without seeing the screen), and
  only "it just navigates" releases one. The word list in policy.yaml does not judge — it is two languages
  wide, so a miss would switch the floor off everywhere else; it only says what to classify first,
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
        """Floor categories whose *words* appear in the action — a hint, never a verdict.

        The patterns in policy.yaml are written in English and Chinese, so "Löschen", "削除", "Supprimer" and
        "Удалить" match nothing at all. Letting a miss mean "safe" would switch the floor off for every other
        language, which is why nothing here decides on its own: hits only say what to classify first, and which
        categories to offer the classifier.
        """
        conf = self.cfg.policy.get("confirm", {}) or {}
        text = f"{a.verb} {a.label} {a.context}"
        hits = [name for name, c in (conf.get("categories") or {}).items()
                if any(re.search(rx, text) for rx in (c or {}).get("patterns") or [])]
        if any(re.search(rx, text) for rx in conf.get("patterns") or []):
            hits.append("other")
        return hits

    def _floor_key(self, ctx: Ctx, a: Affordance) -> str:
        """Per app *version*: an update can move a command or change what a label means."""
        app = ctx.app or {}
        return f"{self.models.key(app) or app.get('bundle_id')}|{a.label}|{a.context}"

    def _releases(self, a: Affordance | None = None) -> dict[str, str]:
        """The verdicts that let an action through, named by policy rather than written into the code: what a
        Mac holds harmless is a policy question, and it grew — deleting a word inside the document being edited
        is not the delete the floor is there for, and asking the user about it stopped a real task dead."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        only_for = conf.get("release_only_for") or {}
        out = {}
        for name in conf.get("release") or ["navigate", "enter"]:
            verbs = only_for.get(name)
            if verbs and a is not None and a.verb not in verbs:   # a is None: asking what counts, not what to offer
                continue
            if conf.get(name):
                out[name] = str(conf[name]).strip()
        return out or {"navigate": "it only opens, shows or navigates to something"}

    def _floor_options(self, a: Affordance, hits: list[str]) -> dict[str, str]:
        conf = self.cfg.policy.get("confirm", {}) or {}
        cats = {k: str((v or {}).get("what") or k) for k, v in (conf.get("categories") or {}).items()}
        cats["other"] = str(conf.get("other") or "it does something else irreversible or outward-facing").strip()
        options = self._releases(a)
        # a word hit narrows the question to what the words suggested; with no hit the whole floor is on the
        # table, because the words being silent says nothing about the action
        return options | ({k: cats[k] for k in hits if k in cats} if hits else cats)

    def _floor_questions(self, ctx: Ctx, affs: list[Affordance], window: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        """Classify the actions the words flagged, in one request of its own sent at the same time as the step's
        own request, so it costs no waiting. Its state holds no screen text, so nothing written on the page can
        argue an action out of the floor.

        Only word hits are pre-warmed. Every other action is classified too, but when it is *chosen* (in
        ``_pick``): speculatively classifying eight arbitrary actions out of the two hundred on a screen would
        cost a request every step to cover the one that gets picked about four times in a hundred.
        """
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
                cache[key] = (a.get("choice", ""), sum(float(probs.get(r, 0.0)) for r in self._releases()))

    def _verdict(self, task_id: str, ctx: Ctx, a: Affordance, hits: list[str], window: str | None,
                 ask: bool) -> tuple[str, float] | None:
        """What this action does, as the decider judges it: (category, how sure it is that it only navigates).

        ``None`` means nobody has judged it — either because we may not ask here, or because the decider could
        not be reached. A failed attempt is never cached: it would make the words stand for the rest of the run.
        """
        key = self._floor_key(ctx, a)
        cache: dict[str, tuple[str, float]] = self.cache.setdefault("floor.verdicts", {})
        if key in cache:
            return cache[key]
        if not ask or getattr(ctx, "gate", None) is None:
            return None
        state = {"app": (ctx.app or {}).get("name"), "window": window, "action": a.label,
                 "where": a.context, "kind": f"{a.channel} {a.verb}"}
        q = self.cfg.question("floor_what").replace("{action}", a.label)
        try:
            ans = ctx.gate.decide(self.redactor(task_id), state, {"what": choice(q, self._floor_options(a, hits))}, task=task_id)
        except DeciderError as exc:
            log.info("floor classification unavailable for %r: %s", a.label[:40], exc)
            return None
        probs = (ans.get("what") or {}).get("probabilities") or {}
        cache[key] = ((ans.get("what") or {}).get("choice", ""),
                      sum(float(probs.get(r, 0.0)) for r in self._releases()))
        return cache[key]

    def _floor(self, task_id: str, ctx: Ctx, a: Affordance, window: str | None = None, ask: bool = True) -> list[str]:
        """The floor categories that gate this action ([] = none).

        *Every* action is judged, not only the ones whose words happen to be in policy.yaml — that word list is
        two languages wide and the floor has to hold in all of them. The judgement is made from the action and
        where it sits, never from screen text, so content on a page cannot argue an action out of the floor.
        Only "it just navigates" (or, for typing, "it only enters text") at >= release_threshold releases an
        action the words flagged.
        """
        conf = self.cfg.policy.get("confirm", {}) or {}
        if conf.get("mode", "caller") == "never":
            return []
        hits = self._floor_hits(a)
        verdict = self._verdict(task_id, ctx, a, hits, window, ask)
        if verdict is None:
            # nobody judged it: the words are all there is. This is the honest degradation when there is no
            # decider at hand (step-level acts, a transient failure) — not a licence, which is why it is logged.
            if not hits:
                log.debug("unclassified, falling back to words: %r", a.label[:60])
            return hits
        top, nav = verdict
        self.cache["floor.last_category"] = top     # what it was judged to be, for the step to record
        if hits:
            if nav >= float(conf.get("release_threshold", 0.9)):
                self.audit.record("floor", task=task_id, action=a.label, released=True, navigate=round(nav, 3), words=hits)
                return []
            return hits
        return [] if top in self._releases() or top == "" else [top]

    def _risky(self, a: Affordance, ctx: Ctx | None = None) -> bool:
        """A cheap read for the places that filter a whole pool of actions before a decider question picks one
        (exploring an app, backing out of a window): what has already been classified, else the words. It does
        not ask — one round trip per candidate would cost more than the question that follows it."""
        if ctx is not None:
            cached = (self.cache.get("floor.verdicts") or {}).get(self._floor_key(ctx, a))
            if cached is not None:
                return cached[0] not in self._releases() and cached[0] != ""
        return bool(self._floor_hits(a))

    def _needs_confirm(self, a: Affordance, risky_screen: float, approved: set[str],
                       harmless: Callable[[Affordance], bool] | None = None, floor: list[str] | None = None) -> bool:
        """The safety floor always asks (``floor``: its categories for this action, as classified; by default
        the words alone, for callers that have no context to classify in). On a screen the decider judged risky,
        anything else asks too — unless the decider, asked about this very action, judges that it only backs out
        (closes, cancels, postpones) and commits nothing."""
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
