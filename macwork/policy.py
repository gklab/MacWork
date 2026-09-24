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

import json
import logging
import re
import time
from typing import Any, Callable

from .decider import DeciderError, choice, noul
from .helper import HelperError
from .model import Affordance, Task
from .observe import Ctx, apps_named, known_apps, names_of
from .planner import Planning
from .privacy import Redactor
from .words import languages_wanted

log = logging.getLogger(__name__)


class PolicyMixin:
    def _host_bundles(self) -> set[str]:
        """The apps this engine runs under (the terminal or client that started it): typing there would type
        into the caller itself. Found by walking up the process tree once, and from the bundle the process was
        started from, `__CFBundleIdentifier`: LaunchServices sets it for every process an app starts, children
        inherit it (this Mac's shells say com.apple.Terminal), and it is still there when the terminal is not
        among the ancestors any more — a process reparented to launchd walks up to nothing. Said once in the
        log, so a run can be told which apps it would not touch."""
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
            found = {a["bundle_id"] for a in running if a.get("pid") in chain and a.get("bundle_id")}
            started_from = os.environ.get("__CFBundleIdentifier", "").strip()
            if started_from:
                found.add(started_from)
            self.cache["host.bundles"] = found
            log.info("the engine runs under %s: never driven", ", ".join(sorted(found)) or "no app it can name")
        return self.cache["host.bundles"]

    def _host_app(self, app: dict[str, Any] | None) -> bool:
        """Is this the app the engine runs under, with the policy saying never to touch that (deny.host_app)?"""
        deny = self.cfg.policy.get("deny", {}) or {}
        bundle = (app or {}).get("bundle_id")
        return bool(bundle) and bool(deny.get("host_app", True)) and bundle in self._host_bundles()

    def _bundle_of(self, pid: Any, app: dict[str, Any] | None) -> str | None:
        """The bundle of the process `pid`: the working app's when it is that one, else what the Mac lists for
        every running process — the menu bar's extras and the prompts of other processes belong mostly to
        processes with no Dock icon, which the list of regular apps leaves out. The list is kept for a minute
        and asked again for a process it does not hold: a process number is given out again only once the
        system's counter has gone round, not within a minute."""
        if app and app.get("pid") == pid:
            return app.get("bundle_id")
        known = self.cache.get("bundles.by_pid")
        now = time.monotonic()
        if known is None or now - known[0] > 60 or pid not in known[1]:
            try:
                running = self.helper.call("apps.running", all=True) or []
            except HelperError:
                running = []
            known = (now, {a.get("pid"): a.get("bundle_id") for a in running if a.get("pid")})
            known[1].setdefault(pid, None)            # a process the Mac does not list: not asked again for it
            self.cache["bundles.by_pid"] = known
        return known[1].get(pid)

    def _denied(self, a: Affordance, app: dict[str, Any] | None) -> bool:
        deny = self.cfg.policy.get("deny", {}) or {}
        allow = self.cfg.policy.get("allow", {}) or {}
        # Which app this action touches, and so whose rules judge it: the app it names (a link, the app that
        # declares its scheme); else the process it names, by that process's bundle; else, for a channel that
        # drives the UI, the app being worked in. A channel that works through the system touches none of them
        # unless it names one. The menu bar's extras name only their owner's pid, and were charged to the
        # working app, so the host's own status menu was judged as whatever app was being worked in.
        from .act import THROUGH_THE_SYSTEM
        bundle = a.target.get("bundle_id") or a.target.get("declared_by")
        if not bundle and a.target.get("pid"):
            bundle = self._bundle_of(a.target["pid"], app)
        if not bundle and a.channel not in THROUGH_THE_SYSTEM:
            if app is None:
                # No app is being worked in, and this drives whatever is in front — which, when a task begins
                # in no app because the one in front runs this engine, is the engine's own terminal — or a
                # process that no bundle names. Charged to nobody, nothing below would judge it. Measured
                # offline with the host in front: the thirteen physical keys went from none offered to all of
                # them, and so did the planner's typing; and while an action naming a pid was let through
                # unjudged, the host's own status menu was offered, and a task pressed it.
                return True
            bundle = app.get("bundle_id")
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
        # the label as it reads without a tooltip that is a control's only name (`floor_text`, observe.py): the
        # action's own verb and title still count, the tooltip's sentence about it does not
        text = f"{a.verb} {a.target.get('floor_text', a.label)} {a.context}"
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
        planning = Planning(self.cfg, backend, None, self.audit)
        if not getattr(backend, "local", False):
            return planning.floor_words(lang, meanings)
        # No task's question and nothing to stop: it only waits its turn at a local server (consult._planner_call)
        with self._planner_lock:
            return planning.floor_words(lang, meanings)

    def _floor_key(self, ctx: Ctx, a: Affordance) -> str:
        """Per app *version*: an update can move a command or change what a label means.

        What the field happens to hold is left out of the key. A label carries it for the decider's benefit —
        「type into 文本栏 (now: /Users/…)」 — and it changes with every keystroke, so the same action came back
        as a new one to classify each time: 81 single-action classifications for 37 distinct actions in one
        run. What an action *does* is the same whatever is in the field, which is what is being classified.
        """
        app = ctx.app or {}
        key = f"{self.models.key(app) or app.get('bundle_id')}|{a.name()}|{a.context}"
        # ...and what the Mac declares about it (`Affordance.facts`): a key in a sheet, in a text field, in a
        # window read only in part or in one that could not be read is another question from the same key
        # elsewhere, and a verdict formed blind serves only looks that are as blind
        return key + "|" + json.dumps(a.facts, sort_keys=True, ensure_ascii=False, default=str) if a.facts else key

    def _declared(self, a: Affordance) -> dict[str, Any]:
        """`the_mac_declares` for a floor question's state, or nothing (policy confirm.declared_facts).

        Off by default: it changes the classifier's input, and how the classifier scores with it has not been
        measured. When on, only what native structure owns: the window or sheet the action is in, the
        control's role and subrole, a menu item's identifier. Nothing about an element of a web page, whose
        roles and identifiers the page writes, and not where the keyboard is, which may be such an element."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        if not conf.get("declared_facts", False) or a.facts.get("_web_content"):
            return {}
        told = {k: v for k, v in a.facts.items() if k in ("in", "control") or (k == "identifier" and a.channel == "menu")}
        return {"the_mac_declares": told} if told else {}

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
                 "where": a.context, "kind": f"{a.channel} {a.verb}", **self._declared(a)}
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

    def floor_categories(self) -> dict[str, str]:
        """What the floor stops for, category by category, in policy's own words — the catch-all last."""
        conf = self.cfg.policy.get("confirm", {}) or {}
        cats = {k: str((v or {}).get("what") or k) for k, v in (conf.get("categories") or {}).items()}
        cats["other"] = str(conf.get("other") or "it does something else irreversible or outward-facing").strip()
        return cats

    def _floor_options(self, a: Affordance, hits: list[str]) -> dict[str, str]:
        cats = self.floor_categories()
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
            actions.append({"action": a.label, "where": a.context, "kind": f"{a.channel} {a.verb}", **self._declared(a)})
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
                 "where": a.context, "kind": f"{a.channel} {a.verb}", **self._declared(a)}
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
        must not be answerable by text on the screen — that is how instructions hidden in a page would get in.

        Which of the user's words are the names of apps on this Mac is neither: it is the goal read against the
        Mac's own list of apps, and a page can change neither. 「在词典里查」 is "look it up in a dictionary"
        until it is known that 词典 is an app here."""
        out = {"goal": task.goal, "inputs": dict(task.inputs), "working_in": (ctx.app or {}).get("name")}
        try:
            named = [str(x.get("name")) for x in apps_named(task.goal, known_apps(ctx)) if x.get("name")]
        except HelperError:
            named = []
        if named:
            out["apps_the_goal_names"] = named
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

    def _leaves_for(self, ctx: Ctx, a: Affordance) -> str | None:
        """Where this action takes the task, when that is out of the app it is working in: the app's name, ""
        for wherever the system hands it, None when it stays.

        Asked of what the action does, not of which executor carries it out. The question below was asked for
        the `app` and `shortcut` channels alone, and real runs left by three other doors without it being
        asked once: the Apple menu's Recent Items (a menu item, 「Google Chrome」, into the person's own
        browser), a link the planner suggested (the file channel, into whatever opens links), and an app's
        icon in a Finder window. A menu item or a control whose own name is the name of another app on this
        Mac opens that app; the Mac's list of apps says which names those are.
        """
        here = (ctx.app or {}).get("bundle_id")
        if a.channel in ("app", "shortcut"):
            # Bringing back the main window of the app in front, or switching to it, stays in it: 34 of 125
            # serves_goal questions in this Mac's audit asked that, and one was declined (0.22), so Finder, with
            # no window open, was not given one back. Only by bundle, and only where the app in front has one: a
            # Shortcut carries none, and neither does every app.
            if here and a.target.get("bundle_id") == here:
                return None
            return str(a.target.get("name") or "")
        if a.channel == "service":      # content handed to another app, which usually comes to the front with it
            return ""
        if a.channel == "file" and a.verb in ("open", "reveal"):
            # whoever the system hands a file or a link to comes to the front — for a web link, the browser
            return None if here and a.target.get("bundle_id") == here else ""
        title = " ".join(str(a.target.get("title") or "").split()).casefold()
        if not title:
            return None
        try:
            apps = known_apps(ctx)
        except HelperError:
            return None
        for app in apps:
            if app.get("bundle_id") != here and title in {n.casefold() for n in names_of(app)}:
                return str(app.get("name") or a.target.get("title"))
        return None

    def _serves_goal(self, task: Task, ctx: Ctx, a: Affordance, leaving_for: str = "") -> bool:
        """Leaving the app being worked in — opening or switching to another app, running a Shortcut, opening a
        link or a file elsewhere — must make sense for the goal as the user stated it (judged without the
        screen's text). `leaving_for` names the app it leaves for when the action's own words do not."""
        if self.approval_key(a) in task.approved or a.id == "launch":
            return True
        cache = task.memory.serves
        if a.label not in cache:
            what = a.label if not leaving_for or leaving_for in a.label else f"{a.label} (it opens {leaving_for})"
            q, fills = self.cfg.question("serves_goal"), {"action": what}
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
