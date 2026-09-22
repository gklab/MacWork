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
from .helper import HelperError
from .model import Affordance, Task
from .observe import Ctx
from .planner import Planning
from .privacy import Redactor
from .words import languages_wanted

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
        # Which app this action touches: the one it names, else — for a channel that drives the UI — the one
        # in front. A channel that works through the system touches neither unless it names one.
        from .act import THROUGH_THE_SYSTEM
        bundle = a.target.get("bundle_id") or (None if a.channel in THROUGH_THE_SYSTEM else (app or {}).get("bundle_id"))
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

        The patterns in policy.yaml are written in English and Chinese; the words for every other language
        this Mac's interface uses are derived once from what each category means (`words.py`), so "Löschen"
        is a hit on a German Mac the way "Delete" is on an English one. Letting a miss mean "safe" would still
        switch the floor off wherever nothing could be derived, which is why nothing here decides on its own:
        hits only say what to classify first, which categories to offer the classifier, and how sure it has
        to be.
        """
        conf = self.cfg.policy.get("confirm", {}) or {}
        text = f"{a.verb} {a.label} {a.context}"
        derived = self._floor_words()
        hits = [name for name, c in (conf.get("categories") or {}).items()
                if any(re.search(rx, text) for rx in list((c or {}).get("patterns") or []) + derived.get(name, []))]
        if any(re.search(rx, text) for rx in conf.get("patterns") or []):
            hits.append("other")
        return hits

    def _floor_words(self) -> dict[str, list[str]]:
        """The derived patterns per category for the languages of this Mac the seed lists do not cover."""
        if "floor.words" not in self.cache:
            conf = self.cfg.policy.get("confirm", {}) or {}
            covered = [str(x).lower() for x in conf.get("languages") or []]
            wanted = languages_wanted(self.system(), covered)
            self.cache["floor.words"] = self.floor_words.patterns(conf.get("categories") or {}, wanted, self._derive_floor_words) if wanted else {}
        return self.cache["floor.words"]

    def _derive_floor_words(self, lang: str, meanings: dict[str, str]) -> dict[str, list[str]] | None:
        backend = self.planning_backend
        if backend is None:
            return None
        return Planning(self.cfg, backend, None, self.audit).floor_words(lang, meanings)

    def _floor_key(self, ctx: Ctx, a: Affordance) -> str:
        """Per app *version*: an update can move a command or change what a label means.

        What the field happens to hold is left out of the key. A label carries it for the decider's benefit —
        「type into 文本栏 (now: /Users/…)」 — and it changes with every keystroke, so the same action came back
        as a new one to classify each time: 81 single-action classifications for 37 distinct actions in one
        run. What an action *does* is the same whatever is in the field, which is what is being classified.
        """
        app = ctx.app or {}
        return f"{self.models.key(app) or app.get('bundle_id')}|{a.name()}|{a.context}"

    def _releases(self, a: Affordance | None = None) -> dict[str, str]:
        """The verdicts that let an action through, named by policy rather than written into the code: what a
        Mac holds harmless is a policy question, and it grew — deleting a word inside the document being edited
        is not the delete the floor is there for, and asking the user about it stopped a real task dead.

        ``release_only_for`` narrows a verdict to the actions it can honestly apply to. Each entry is a verb
        or ``channel:<name>``; an action that is neither cannot be released by that verdict however sure the
        classifier is. "It only changes what this window holds" is a judgement about prose; "this action is
        on the channel that moves files" is a fact about the action, and where a fact is available it is not
        the classifier's to overrule.

        ``a`` is None when the question is "what counts as a release at all" rather than "may *this* be
        released" — every caller that is deciding about an action passes it.
        """
        conf = self.cfg.policy.get("confirm", {}) or {}
        only_for = conf.get("release_only_for") or {}
        out = {}
        for name in conf.get("release") or ["navigate", "enter"]:
            allowed = only_for.get(name)
            if allowed and a is not None and a.verb not in allowed and f"channel:{a.channel}" not in allowed:
                continue
            if conf.get(name):
                out[name] = str(conf[name]).strip()
        return out or {"navigate": "it only opens, shows or navigates to something"}

    def vetted(self, task_id: str, ctx: Ctx, pick: Affordance | None, window: str | None = None) -> Affordance | None:
        """The pick, or None if the safety floor gates it.

        For the paths that choose an action outside the main loop — backing out of a dialog, undoing an
        exploration step, resuming a caller's choice. Each of those used `_risky` at most, which does not
        ask the decider and falls back to a word list two languages wide, while their own docstrings said
        the floor covered them. They also choose with `screen_text` in front of the decider, unlike the
        floor, which is deliberately blind to it — so they had the most influence from page text and the
        least gating.
        """
        if pick is None:
            return None
        if getattr(ctx, "gate", None) is None:
            # These callers build their own Ctx and ask through `self.gate` directly, so `ctx.gate` was
            # None — and `_floor` with nobody to ask degrades to the word list, which is the degradation
            # this guard exists to prevent. A guard must not be switched off by a caller forgetting.
            ctx.gate = self.gate
        gated = self._floor(task_id, ctx, pick, window)
        if gated:
            log.info("not doing %r unasked: the floor calls it %s", pick.label[:60], gated)
            self.audit.record("floor", task=task_id, action=pick.label, released=False, words=gated, where="off-loop")
            return None
        return pick

    def still_the_app(self, app: dict[str, Any]) -> bool:
        """Is the process at that pid still the app this task opened?

        A pid is a handle to a process that was running then. It survives a restart in the store, macOS
        reuses the numbers, and `tidy` quits by pid — so a resumed task could quit whatever now holds the
        number its app had. An entry with no bundle id cannot be checked, and is therefore not acted on.
        """
        pid, bundle = app.get("pid"), app.get("bundle_id")
        if not pid or not bundle:
            return False
        try:
            running = self.helper.call("apps.running")
        except HelperError:
            return False
        return any(a.get("pid") == pid and a.get("bundle_id") == bundle for a in running)

    def _calibrated(self) -> bool:
        """Does the decider's confidence mean a frequency? Every threshold in `config.yaml` assumes so."""
        return bool(getattr(self.decider, "calibrated", True))

    def _uncalibrated_release(self, task_id: str, ctx: Ctx, a: Affordance, window: str | None, ask: bool) -> bool:
        """Ask a decider that cannot be thresholded the question it *can* answer: yes or no.

        Cached with the verdict, so this costs one question the first time an action is seen and nothing
        afterwards. It is asked with no screen text, like the classification it backs up, so content on a
        page cannot answer it.
        """
        key = f"noul|{self._floor_key(ctx, a)}"
        cache: dict[str, bool] = self.cache.setdefault("floor.harmless", {})
        if key in cache:
            return cache[key]
        if not ask or getattr(ctx, "gate", None) is None:
            return False
        state = {"app": (ctx.app or {}).get("name"), "window": window, "action": a.label,
                 "where": a.context, "kind": f"{a.channel} {a.verb}"}
        q, fills = self.cfg.question("floor_harmless"), {"action": a.label}
        try:
            ans = ctx.gate.decide(self.redactor(task_id), state, {"harmless": noul(q, fills=fills)}, task=task_id)
        except DeciderError as exc:
            log.info("floor: no second opinion for %r (%s)", a.label[:40], exc)
            return False
        got = (ans.get("harmless") or {}).get("noul")
        cache[key] = got is not None and float(got) < 0.5      # "does it do something irreversible?" -> no
        return cache[key]

    def _scored(self, a: Affordance, verdict: tuple[str, Any]) -> tuple[str, float]:
        """(what it was judged to be, how much of that judgement counts as a release *for this action*).

        The score used to be worked out once and cached. But `release_only_for` makes the answer depend on
        the action — and the cache key is the app, the label and where it sits, which does not tell a menu
        command apart from the file channel moving something to the Trash. A score computed for one was
        being handed to the other.
        """
        choice_, probs = verdict
        if not isinstance(probs, dict):     # a score cached by an older run: keep it rather than lose the verdict
            return choice_, float(probs)
        return choice_, sum(float(probs.get(r, 0.0)) for r in self._releases(a))

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

        Word hits go first, then the budget is filled with whatever else is on offer. The reasoning used to be
        that pre-warming arbitrary actions buys little — the one that gets chosen is rarely among them. But the
        cost was counted wrong: this request is sent anyway, and the decider answers its questions in parallel
        against one state, so another question costs a question, not a round trip. What did cost a round trip
        was the other path — classifying the chosen action afterwards, which the engine must do before acting.
        Measured over one suite: 59 of those, one at a time, against 101 steps.
        """
        questions: dict[str, Any] = {}
        mapping: dict[str, str] = {}
        cache = self.cache.setdefault("floor.verdicts", {})
        limit = int(self.cfg.get("engine.floor_batch", 24))
        actions: list[dict[str, str]] = []
        fresh = [(a, self._floor_hits(a)) for a in affs if self._floor_key(ctx, a) not in cache]
        for a, hits in sorted(fresh, key=lambda x: not x[1]):   # the words' candidates first, then the rest
            if len(questions) >= limit:
                break
            key = self._floor_key(ctx, a)
            if key in mapping.values():
                continue
            qid = f"floor{len(questions)}"
            questions[qid] = choice(self.cfg.question("floor_what"), self._floor_options(a, hits), fills={"action": a.label})
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
                # the probabilities, not a score: what fraction of them counts as "released" depends on the
                # action being asked about, and the key does not distinguish a menu command from a file move
                cache[key] = (a.get("choice", ""), probs)

    def _verdict(self, task_id: str, ctx: Ctx, a: Affordance, hits: list[str], window: str | None,
                 ask: bool) -> tuple[str, float] | None:
        """What this action does, as the decider judges it: (category, how sure it is that it only navigates).

        ``None`` means nobody has judged it — either because we may not ask here, or because the decider could
        not be reached. A failed attempt is never cached: it would make the words stand for the rest of the run.
        """
        key = self._floor_key(ctx, a)
        cache: dict[str, tuple[str, dict[str, float]]] = self.cache.setdefault("floor.verdicts", {})
        if key in cache:
            return self._scored(a, cache[key])
        if not ask or getattr(ctx, "gate", None) is None:
            return None
        state = {"app": (ctx.app or {}).get("name"), "window": window, "action": a.label,
                 "where": a.context, "kind": f"{a.channel} {a.verb}"}
        q, fills = self.cfg.question("floor_what"), {"action": a.label}
        try:
            ans = ctx.gate.decide(self.redactor(task_id), state, {"what": choice(q, self._floor_options(a, hits), fills=fills)}, task=task_id)
        except DeciderError as exc:
            log.info("floor classification unavailable for %r: %s", a.label[:40], exc)
            return None
        probs = (ans.get("what") or {}).get("probabilities") or {}
        cache[key] = ((ans.get("what") or {}).get("choice", ""), probs)
        return self._scored(a, cache[key])

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
        if top == "":
            # The decider answered and the answer was unusable — not the same as not being able to ask it.
            # This used to `return hits`, which on a Mac whose language the words do not cover is an empty
            # list, which is a release.
            log.info("the classification of %r came back with no verdict", a.label[:60])
            return hits or ["unclassified"]
        # One threshold, both paths. Batch 2 removed a word list that read a miss as "safe"; what replaced
        # it required near-certainty to release an action the words flagged and took the classifier's bare
        # argmax for one they did not — so the same unsure verdict released on a German or Japanese Mac and
        # stopped on an English one. The words may still decide *what* is asked about; how sure the
        # classifier has to be is not theirs to set.
        #
        # The threshold is a cut-off on a *calibrated* probability. A decider that says it is not
        # calibrated (`localdecider`, capped at `confidence_ceiling` precisely because its numbers are
        # words it wrote) can never reach it, and gating every action including scrolling is not a safer
        # engine, it is an unusable one. Such a decider is asked the question it can answer instead — a
        # plain yes or no about this action — and the ceiling still keeps it from releasing what the words
        # flagged. See `_uncalibrated_release`.
        if top not in self._releases(a):
            return hits or [top]
        if self._calibrated():
            # Two bars, and which applies is decided by the words — not because a word list may set how
            # sure a classifier has to be, but because a hit *is* evidence of risk and evidence is
            # allowed to change what is required. What is not allowed is the reverse, their silence
            # standing in for evidence of safety: that was a bare argmax, and it released a German
            # "Löschen" at 0.34 while stopping an English "Delete".
            #
            # One bar for both was tried and measured: at 0.9 everywhere, a calculator's `Equals`,
            # `File ▸ New` and `File ▸ Open…` all came back needing confirmation, which is the failure
            # `edit` exists to prevent, recreated. 0.9 was calibrated for releasing something the words
            # flagged, and that prior evidence is what makes so high a bar reasonable.
            bar = "release_threshold" if hits else "release_threshold_unflagged"
            sure = nav >= float(conf.get(bar, 0.9 if hits else 0.7))
        else:
            sure = not hits and self._uncalibrated_release(task_id, ctx, a, window, ask)
        if sure:
            self.audit.record("floor", task=task_id, action=a.label, released=True,
                              navigate=round(nav, 3), words=hits, verdict=top,
                              calibrated=self._calibrated())
            return []
        # Gated, but not under the name of a release: "because: ['navigate']" is not a reason to show
        # anyone. The words' own categories if they had any, else that nobody could vouch for it.
        return hits or ["unclassified"]

    def _risky(self, a: Affordance, ctx: Ctx | None = None) -> bool:
        """A cheap read for the places that filter a whole pool of actions before a decider question picks one
        (exploring an app, backing out of a window): what has already been classified, else the words. It does
        not ask — one round trip per candidate would cost more than the question that follows it."""
        if ctx is not None:
            cached = (self.cache.get("floor.verdicts") or {}).get(self._floor_key(ctx, a))
            if cached is not None:
                return cached[0] not in self._releases() and cached[0] != ""
        return bool(self._floor_hits(a))

    # ------------------------------------------------------------------ standing grants
    def standing(self, task_id: str, ctx: Ctx, a: Affordance, because: list[str]) -> tuple[bool, dict[str, Any] | None]:
        """(a grant the person gave covers this, what a grant for it would be if it may have one).

        Asked only at the point where the caller would otherwise be asked — after the floor has judged the
        action and after the goal was found to call for it. A grant answers "may this go ahead without
        asking me", never "should this be done".
        """
        offer = self.grants.offer(ctx.app, a, because)
        row = self.grants.covers(offer)
        if row is None:
            return False, offer
        self.grants.used(offer)
        self.audit.record("floor", task=task_id, action=a.label, released=True, grant=row.get("id"),
                          because=because, granted_at=row.get("granted_at"))
        return True, offer

    def _confirm_pending(self, shown: dict[str, Any], because: list[str], offer: dict[str, Any] | None) -> dict[str, Any]:
        pending: dict[str, Any] = {"confirm": shown, "because": because}
        if offer:     # the caller can offer "always allow this" — and is told how the person does it
            pending["remember"] = offer
            pending["remember_how"] = ("resume with remember=true" if self.cfg.get("grants.from_mcp", False)
                                       else "the person runs: macwork grants allow <task-id>")
        return pending

    @staticmethod
    def approval_key(a: Affordance) -> str:
        """What an approval is *for*.

        It used to be the bare label, and labels repeat constantly — 「删除」, "OK", "Send", "Don't Save".
        Confirming one dialog's button released every later action in the task called the same thing, in
        any app. This names the action: its language-independent identity where the Mac gives one
        (`Affordance.key`, so a relabelled button is still the same button), else the label, and where it
        sits either way.
        """
        return f"{a.key or a.label}\x1f{a.context}"

    def approve(self, task: Task, a: Affordance) -> None:
        """The caller said yes to this one action."""
        task.approved.add(self.approval_key(a))

    def resume_approval(self, task: Task) -> None:
        """The caller said yes. Approve what was actually asked about — `task.confirm_key` when the
        question was about more than the held action alone."""
        if task.confirm_key:
            task.approved.add(task.confirm_key)
            task.confirm_key = ""
        elif task.held is not None:
            self.approve(task, task.held)

    def spend(self, task: Task, a: Affordance) -> None:
        """...and it has now run. A confirmation answers "do this now", not "do this whenever": a task
        that deletes five files asks five times, which is what a floor is."""
        task.approved.discard(self.approval_key(a))
        task.confirm_key = ""

    def _needs_confirm(self, a: Affordance, risky_screen: float, approved: set[str],
                       harmless: Callable[[Affordance], bool] | None = None, floor: list[str] | None = None) -> bool:
        """The safety floor always asks (``floor``: its categories for this action, as classified; by default
        the words alone, for callers that have no context to classify in). On a screen the decider judged risky,
        anything else asks too — unless the decider, asked about this very action, judges that it only backs out
        (closes, cancels, postpones) and commits nothing."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        if conf.get("mode", "caller") == "never" or self.approval_key(a) in approved:
            return False
        if self._floor_hits(a) if floor is None else floor:
            return True
        th = float(self.cfg.get("engine.thresholds.risky_screen", 0.6))
        exempt = set(conf.get("screen_gate_exempt") or [])
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
        if self.approval_key(a) in task.approved:
            return True
        q, fills = self.cfg.question("goal_calls_for"), {"action": a.label}
        try:
            ans = ctx.gate.decide(redactor, self._goal_state(task, ctx, a), {"calls_for": noul(q, fills=fills)}, task=task.id)
        except DeciderError:
            return True    # cannot tell: let the caller decide
        return float(ans.get("calls_for", {}).get("noul", 1.0)) >= float(self.cfg.get("engine.thresholds.goal_calls_for", 0.3))

    def _serves_goal(self, task: Task, ctx: Ctx, a: Affordance) -> bool:
        """Leaving the app being worked in — opening or switching to another app, running a Shortcut, researching
        on the web — must make sense for the goal as the user stated it (judged without the screen's text)."""
        if self.approval_key(a) in task.approved or a.id == "launch":
            return True
        cache = task.memory.serves
        if a.label not in cache:
            q, fills = self.cfg.question("serves_goal"), {"action": a.label}
            try:
                ans = ctx.gate.decide(self.redactor(task.id), self._goal_state(task, ctx, a), {"serves": noul(q, fills=fills)}, task=task.id)
                cache[a.label] = float(ans.get("serves", {}).get("noul", 1.0)) >= float(self.cfg.get("engine.thresholds.serves_goal", 0.35))
            except DeciderError:
                cache[a.label] = True
        return cache[a.label]

    def _backs_out(self, task: Task, ctx: Ctx, redactor: Redactor, state: dict[str, Any], a: Affordance) -> bool:
        """Does this action just back out of where it is — close a panel, cancel, dismiss, go back?

        Asked on a screen judged risky, and also before dropping a gated action the goal never asked for:
        getting out of a dialog is never what a goal asks for, and a task that cannot do it is stuck there.
        """
        q, fills = self.cfg.question("backs_out"), {"action": a.label}
        try:
            ans = ctx.gate.decide(redactor, state, {"backs_out": noul(q, fills=fills)}, task=task.id)
        except DeciderError:
            return False
        return float(ans.get("backs_out", {}).get("noul", 0.0)) >= float(self.cfg.get("engine.thresholds.backs_out", 0.8))
