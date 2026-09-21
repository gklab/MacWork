"""After a task: close what it opened and no longer needs — apps it started, and windows, dialogs and sheets it
left in apps that were already running. What was there before the task is never touched."""

from __future__ import annotations

import logging
import time
from typing import Any

from .decider import DeciderError, choice, noul
from .helper import HelperError
from .model import Task
from .observe import arrange, observe

log = logging.getLogger(__name__)


def _near(a: list[int] | None, b: list[int] | None, slack: int = 3) -> bool:
    return bool(a and b) and all(abs(x - y) <= slack for x, y in zip(a, b))


class TidyMixin:
    # --------------------------------------------------------------- what the task found and what it opened
    def _tidy_later(self, task_id: str) -> None:
        with self._lock:   # after the task's own run has returned; a next task waits for it (it may close an app)
            try:
                res = self.tidy(task_id)
                task = self.tasks.get(task_id)
                if task is not None:
                    task.outputs["tidy"] = res or "nothing to close"
            except (HelperError, DeciderError) as exc:
                log.info("tidy: %s", exc)

    def _window_ids(self) -> dict[int, set[int]]:
        """Every window by owner (the system's window numbers: stable for a window's life).

        Every window, not only the ones on this Space: a task that opened a window and then switched away
        would otherwise look as if the window had gone, and it would never be cleaned up.
        """
        try:
            wins = self.helper.call("screen.windows", all=True)
        except HelperError:
            return {}
        out: dict[int, set[int]] = {}
        for w in wins:
            if w.get("layer", 0) == 0 and w.get("id") and w.get("alpha", 1):
                out.setdefault(int(w["pid"]), set()).add(int(w["id"]))
        return out

    def _note_opened(self, task: Task, running: list[dict[str, Any]]) -> None:
        """Apps that were not running when the task began and started while it ran: the task opened them. The
        windows on screen when it began are remembered too: any others are the task's."""
        if task.desktop.initial_pids is None:
            task.desktop.initial_pids = {a["pid"] for a in running}
            task.desktop.initial_windows = self._window_ids()
            return
        for a in running:
            if a["pid"] not in task.desktop.initial_pids and float(a.get("launched") or 0) >= task.started_wall - 1:
                task.desktop.opened.setdefault(a["pid"], a)

    def _app_facts(self, app: dict[str, Any]) -> dict[str, Any]:
        """What the Mac says about an app, for the decider: its declared category, whether it is a menu-bar-only
        agent, and which windows it has open."""
        import plistlib
        from pathlib import Path
        facts: dict[str, Any] = {"app": app.get("name")}
        try:
            info = plistlib.loads((Path(app.get("path") or "") / "Contents" / "Info.plist").read_bytes())
            if info.get("LSApplicationCategoryType"):
                facts["category"] = str(info["LSApplicationCategoryType"]).rsplit(".", 1)[-1]
            if info.get("LSUIElement"):
                facts["menu_bar_agent"] = True
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        try:
            wins = self.helper.call("ax.snapshot", pid=app["pid"], scope="windows", max_depth=0, max_nodes=20, actions=False).get("nodes", [])
            facts["windows"] = [n.get("title") or "(untitled)" for n in wins][:6]
        except HelperError:
            pass
        return facts

    def _new_windows(self, task: Task, running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Windows, dialogs and sheets that appeared during the task in apps that were already running (the host
        app excluded), each matched to its Accessibility element so it can be closed like a person would."""
        if task.desktop.initial_windows is None:
            return []
        alive = {a["pid"]: a for a in running}
        host = self._host_bundles()
        try:
            now = self.helper.call("screen.windows")
        except HelperError:
            return []
        out: list[dict[str, Any]] = []
        snaps: dict[int, list[dict[str, Any]]] = {}
        for w in now:
            pid = int(w.get("pid") or 0)
            if (w.get("layer", 0) != 0 or not w.get("alpha", 1) or pid not in alive or pid in task.desktop.opened
                    or pid not in (task.desktop.initial_pids or set()) or alive[pid].get("bundle_id") in host
                    or int(w.get("id") or 0) in task.desktop.initial_windows.get(pid, set())):
                continue
            if pid not in snaps:
                try:
                    snaps[pid] = self.helper.call("ax.snapshot", pid=pid, scope="windows", max_depth=1, max_nodes=400).get("nodes", [])
                except HelperError:
                    snaps[pid] = []
            nodes = snaps[pid]
            el = next((n for n in nodes if n.get("role") == "AXWindow" and _near(n.get("frame"), w.get("frame"))), None)
            sheet_of = None
            if el is None:   # a sheet: its own system window, but a child of a window in Accessibility
                el = next((n for n in nodes if n.get("role") == "AXSheet" and _near(n.get("frame"), w.get("frame"))), None)
                sheet_of = el and next((n for n in nodes if n["ref"] == el.get("parent")), None)
            if el is None:
                continue
            close = next((n for n in nodes if n.get("parent") == el["ref"] and n.get("subrole") == "AXCloseButton"), None)
            out.append({"pid": pid, "app": alive[pid].get("name"), "bundle_id": alive[pid].get("bundle_id"), "id": int(w["id"]),
                        "title": el.get("title") or (sheet_of or {}).get("title") or "(untitled)",
                        "kind": "sheet" if sheet_of else ("dialog" if el.get("subrole") in ("AXDialog", "AXSystemDialog") or not close else "window"),
                        "close": close and close["ref"]})
        return out

    # --------------------------------------------------------------- tidying up
    def tidy(self, task_id: str) -> dict[str, Any]:
        """Close what this task opened and no longer needs; what was there before it is never touched.
        Apps it started: the decider judges, from the goal, the result and the app's own facts, whether the user
        still needs it and whether it is the kind that keeps running in the background (chat, mail, sync, playing
        media); only when both are no is it quit, politely. Windows, dialogs and sheets it left in apps that were
        already running: closed unless the decider judges the user still needs them (the goal was to show them).
        Whatever asks something on the way out (save changes?) is backed out of and left as it was."""
        task = self.tasks.get(task_id)
        if task is None or task.pace.tidied:
            return {}
        task.pace.tidied = True
        running = self.helper.call("apps.running")
        self._note_opened(task, running)
        alive = {a["pid"]: a for a in running}
        apps = [alive[pid] for pid in task.desktop.opened if pid in alive]
        windows = self._new_windows(task, running)
        if not apps and not windows:
            return {}
        th = self.cfg.section("engine.thresholds")
        facts = [self._app_facts(a) for a in apps]
        state = {"goal": task.goal, "status": task.status, "reason": task.reason, "steps": [s.action for s in task.steps][-8:],
                 "result_screen": task.outputs.get("result_screen"), "apps_opened_by_the_task": facts,
                 "windows_opened_by_the_task": [{"app": w["app"], "window": w["title"], "kind": w["kind"]} for w in windows]}
        questions: dict[str, Any] = {}
        for i, f in enumerate(facts):
            questions[f"keep{i}"] = noul(self.cfg.question("keep_open"), fills={"app": str(f["app"])})
            questions[f"bg{i}"] = noul(self.cfg.question("stays_running"), fills={"app": str(f["app"])})
        for j, w in enumerate(windows):
            questions[f"win{j}"] = noul(self.cfg.question("keep_window"),
                                        fills={"window": str(w["title"]), "app": str(w["app"])})
        try:
            ans = self.gate.decide(self.redactor(task.id), state, questions, task=task.id)
        except DeciderError as exc:
            task.outputs["left_open"] = [{"app": f["app"], "why": f"could not decide: {exc}"[:120]} for f in facts]
            return {"left_open": task.outputs["left_open"]}
        closed, left = [], []
        for j, w in enumerate(windows):   # windows first: an app quit next would take its own along anyway
            name = f"{w['app']}: {w['title']}"
            if float(ans.get(f"win{j}", {}).get("noul", 1.0)) >= float(th.get("keep_window", 0.5)):
                left.append({"window": name, "why": "still needed for the goal"})
            elif self._close_window(task, w):
                closed.append(name)
            else:
                left.append({"window": name, "why": "it asked something before closing; left as it was"})
        for i, (a, f) in enumerate(zip(apps, facts)):
            keep = float(ans.get(f"keep{i}", {}).get("noul", 1.0))
            bg = float(ans.get(f"bg{i}", {}).get("noul", 1.0))
            if keep >= float(th.get("keep_open", 0.5)):
                left.append({"app": f["app"], "why": "still needed for the goal"})
            elif bg >= float(th.get("stays_running", 0.5)):
                left.append({"app": f["app"], "why": "meant to keep running in the background"})
            elif self._quit(task, a):
                closed.append(f["app"])
            else:
                left.append({"app": f["app"], "why": "it asked something before quitting; left as it was"})
        if closed:
            task.outputs["closed"] = closed
        if left:
            task.outputs["left_open"] = left
        self.audit.record("tidy", task=task.id, closed=closed, left=left)
        return {"closed": closed, "left_open": left}

    def _gone(self, pid: int, wid: int) -> bool:
        return wid not in self._window_ids().get(pid, set())

    def _close_window(self, task: Task, w: dict[str, Any]) -> bool:
        """Its own close button when it has one; a dialog or sheet without one is backed out of (Cancel…) with
        what the decider picks. If closing asks something (save?), that is backed out of and the window stays."""
        if w.get("close"):
            try:
                self.helper.call("ax.perform", ref=w["close"], action="AXPress")
            except HelperError as exc:
                log.info("close window: %s", exc)
            if self._wait_gone(w["pid"], w["id"]):
                return True
        self._back_out(task, {"pid": w["pid"], "name": w["app"], "bundle_id": w.get("bundle_id")}, "dismiss_window",
                       window=w["title"])
        gone = self._wait_gone(w["pid"], w["id"])
        return bool(not w.get("close") and gone)

    def _wait_gone(self, pid: int, wid: int) -> bool:
        """A window is closed when it is no longer there — which is a thing to watch for, not to wait out.
        Closing can also put a question on screen instead (save?), and then it never goes: hence a ceiling."""
        deadline = time.monotonic() + float(self.cfg.get("engine.close_wait_s", 0.6))
        while True:
            if self._gone(pid, wid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _quit(self, task: Task, app: dict[str, Any]) -> bool:
        if not self.still_the_app(app):
            log.info("not quitting pid %s: it is no longer %s", app.get("pid"), app.get("bundle_id") or app.get("name"))
            return False
        r = self.helper.call("apps.quit", pid=app["pid"], timeout_ms=int(self.cfg.get("engine.quit_wait_ms", 3000)), timeout=15)
        if r.get("terminated"):
            return True
        self._back_out(task, app, "cancel_quit")   # it is asking something (save changes?): keep everything as is
        return False

    # --------------------------------------------------------------- putting back what a task changed
    def revert(self, task_id: str, max_steps: int | None = None) -> dict[str, Any]:
        """Undo what this task altered, most recent first.

        `tidy` closes what a task *opened*. What it *changed* stayed changed: a task that typed into the
        wrong document, renamed the wrong thing or half-finished an edit left the Mac that way, and the only
        recovery was the person doing it by hand.

        Nothing here knows the word "undo". Every app publishes its own undo command, in its own words, in
        its own menus, and the menu provider already offers it like any other action — so this asks the
        decider to pick, among what the app itself offers, the one that puts the last change back. On a
        German Mac it picks "Widerrufen" without this file having heard of it.

        Which steps changed anything is not judged again either: the safety floor classifies every action
        before it runs, and anything it did not call navigating or entering text is a change (`Step.effect`).

        Two things it will not do. It will not undo while the person has been using the Mac since — their
        keystrokes went into these same apps and an undo would take those back instead (HID idle time, the
        same signal `take_hands` uses). And it stops at the first step it cannot put back rather than
        carrying on down the stack, because an undo that has lost its place does more harm than none.
        """
        task = self.tasks.get(task_id) or self._recall(task_id)
        if task is None:
            return {"ok": False, "error": f"no task {task_id}"}
        rc = self.cfg.section("engine.revert")
        if not task.changed:
            return {"ok": True, "reverted": [], "note": "this task changed nothing that the floor could see"}
        idle_needed = float(rc.get("require_user_idle_s", 5))
        try:
            idle = float(self.helper.call("input.idle").get("idle_s", 0))
        except HelperError:
            idle = 0.0
        if idle < idle_needed:
            return {"ok": False, "error": f"someone has been using the Mac ({idle:.0f}s ago): undoing now would "
                                          f"take back their work, not this task's", "reverted": []}
        budget = int(max_steps if max_steps is not None else rc.get("max_steps", 8))
        done: list[str] = []
        with self._lock:      # reverting drives the Mac; a next task waits, as it does for tidy
            for change in list(reversed(task.changed))[:budget]:
                app = change.get("app") or {}
                if not app.get("pid"):
                    break
                picked = self._undo_one(task, app, change)
                if picked is None:
                    return {"ok": False, "reverted": done, "stopped_at": change["action"],
                            "error": "nothing here puts that back — stopping rather than guessing further"}
                done.append(f"{change['action']} -> {picked}")
        task.outputs["reverted"] = done
        return {"ok": True, "reverted": done, "of": len(task.changed)}

    def _undo_one(self, task: Task, app: dict[str, Any], change: dict[str, Any]) -> str | None:
        """Ask the app — through the decider — which of its own actions puts one change back. None = nothing does."""
        try:
            ctx = self._ctx(task.goal, task.inputs, app, task.id)
            obs = observe(ctx)
            # its own commands and keys; not another app, not a file, not a URL. A slot would need text that
            # nobody has, and the floor gates the rest.
            pool = [a for a in obs.affordances if a.channel in ("menu", "window", "keys") and not a.slots]
            flat, _ = arrange(pool, int(self.cfg.get("engine.max_options", 200)) - 1, set())
            options = {"none": "nothing here puts that back"} | {a.id: a.describe() for a in flat}
            q = self.cfg.question("revert_which")
            fills = {"action": change["action"], "app": str(app.get("name") or "")}
            ans = self.gate.decide(self.redactor(task.id),
                                   {"app": app.get("name"), "window": obs.window,
                                    "screen_text": obs.screen_text[:600],
                                    "what_was_done": change["action"], "the_goal_it_was_for": task.goal},
                                   {"back": choice(q, options, fills=fills)}, task=task.id)
            pick = next((a for a in flat if a.id == (ans.get("back") or {}).get("choice")), None)
            if pick is None:
                return None
            # An undo is itself an action, and it meets the floor like any other: putting something back by
            # deleting it, or by sending something, is not a quieter thing to do than what it undoes.
            if self._floor(task.id, ctx, pick, obs.window):
                log.info("revert: %r is gated by the floor; not doing it unasked", pick.label[:60])
                return None
            out, _ = self._execute(ctx, pick, {})
            return pick.label if out.ok else None
        except (HelperError, DeciderError) as exc:
            log.info("revert: %s", exc)
            return None

    def _back_out(self, task: Task, app: dict[str, Any], question: str, window: str = "") -> None:
        """The decider picks, among what the app shows, the control that backs out and keeps everything as
        it is. The floor then gates that pick like any other action — it used to say so and not do it, and
        this runs on exactly the screens where the dangerous buttons are (a "save changes?" sheet reached
        while tidying up)."""
        try:
            ctx = self._ctx(task.goal, task.inputs, {"pid": app["pid"], "name": app.get("name"), "bundle_id": app.get("bundle_id")}, task.id)
            obs = observe(ctx)
            pool = [a for a in obs.affordances if a.channel in ("window", "keys", "pointer") and not a.slots and not self._risky(a, ctx)]
            flat, _ = arrange(pool, int(self.cfg.get("engine.max_options", 200)) - 1, set())
            options = {"none": "none of these backs out"} | {a.id: a.describe() for a in flat}
            q = self.cfg.question(question)
            fills = {"app": str(app.get("name")), "window": window}
            ans = self.gate.decide(self.redactor(task.id), {"app": app.get("name"), "window": obs.window, "screen_text": obs.screen_text[:600]},
                                   {"back": choice(q, options, fills=fills)}, task=task.id)
            pick = self.vetted(task.id, ctx, next((a for a in flat if a.id == (ans.get("back") or {}).get("choice")), None),
                               obs.window)
            if pick is not None:
                self._execute(ctx, pick, {})
        except (HelperError, DeciderError) as exc:
            log.info("back out: %s", exc)
