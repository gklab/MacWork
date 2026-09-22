"""MCP server: the Mac as tools, at three levels of control.

* ``mac_do`` / ``mac_resume`` / ``mac_cancel`` — give a goal; the decider drives; answer what it asks
* ``mac_observe`` / ``mac_act``                — see the affordances and pick yourself (no decider involved)
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer

from .config import Config
from .engine import Engine

INSTRUCTIONS = """Operate this Mac through native interfaces (apps, menus, Accessibility, files, shortcuts, web).
Use mac_do for a goal; it returns status done | failed | blocked | need_input | need_confirm | ambiguous | cancelled.
blocked: something only the user can do (sign in, a password, a system permission) stands in the way — tell them.
need_continue: the task is making progress but used this turn's step or time budget; call mac_resume with no
arguments to carry on (it keeps everything it has learned, and stops for good at its whole-task ceiling).
need_input: call mac_resume with inputs for the listed keys (the decider cannot write text, so text to type,
search queries and file names always come from you; pass them up front in `inputs` when you know them —
common keys: text, query, url, file). For something steered rather than operated (a game, a map, a 3D view), declare its controls in inputs.controls — {"move forward": {"keys": ["w"], "ms": [300, 1200]}, "look left": {"move": {"dx": -200}, "ms": 150}, "attack": {"buttons": ["left"], "ms": 100}} — and each becomes an option held for exactly that long. need_confirm: ask the user, then mac_resume(confirm=true/false). If pending.remember is present, also tell them they can have this remembered for that app (pending.remember_how) — that choice is theirs, never yours.
ambiguous: mac_resume(choice=<id>) with one of the offered ids. To drive step by step yourself, call
mac_observe then mac_act with an affordance id from that observation."""


def build(cfg: Config | None = None, engine: Engine | None = None) -> Any:
    cfg = cfg or Config.load()
    eng = engine or Engine(cfg)
    # What goes back to an MCP caller leaves this process: redacted like a decider request, unless a
    # local caller has said it wants the screen as it is.
    redact = not bool(cfg.get("server.raw_observe", False))
    mcp = MCPServer(cfg.get("server.name", "macwork"), instructions=INSTRUCTIONS)
    mcp._macwork_engine = eng      # so serve() can let go of it when the transport ends

    def reporter(ctx: Context | None):
        count = {"n": 0}

        def report(message: str) -> None:
            if ctx is None:
                return
            count["n"] += 1
            try:
                anyio.from_thread.run(ctx.report_progress, count["n"], None, message)
            except Exception:  # noqa: BLE001  (progress is best effort)
                pass
        return report

    @mcp.tool()
    async def mac_do(goal: str, inputs: dict[str, Any] | None = None, app: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Accomplish a goal on this Mac. `inputs` carries any text the task needs (text, query, url, file…);
        `app` names the app to work in (default: the frontmost one). A long task may come back as
        `need_continue`: it is getting somewhere but this turn's budget is spent — call mac_resume."""
        return await anyio.to_thread.run_sync(lambda: eng.do(goal, inputs, app, reporter(ctx)))

    @mcp.tool()
    async def mac_submit(goal: str, inputs: dict[str, Any] | None = None, app: str | None = None) -> dict[str, Any]:
        """Hand in a goal and get its id back at once, without waiting for the Mac to be free. Tasks still run
        one at a time; this returns the queue position instead of blocking. Follow it with mac_status."""
        return await anyio.to_thread.run_sync(lambda: eng.submit(goal, inputs, app))

    def _may_remember(asked: bool) -> tuple[bool, dict[str, Any]]:
        """A caller here is usually a model. It may confirm one action on the person's word, as it always
        could; a *standing* permission is a different thing to take on someone's word, so by default it is
        given where only the person can be: `macwork grants allow <task-id>`."""
        if not asked or cfg.get("grants.from_mcp", False):
            return asked, {}
        return False, {"remember_ignored": "a standing grant is the person's to give: macwork grants allow <task-id> "
                                           "(or set grants.from_mcp: true)"}

    @mcp.tool()
    async def mac_revert(task_id: str, max_steps: int | None = None) -> dict[str, Any]:
        """Put back what a task changed, most recent change first, using each app's own undo command.
        Refuses while someone has been using the Mac, and stops at the first change it cannot put back."""
        return await anyio.to_thread.run_sync(lambda: eng.revert(task_id, max_steps))

    @mcp.tool()
    async def mac_resume(task_id: str, inputs: dict[str, Any] | None = None, confirm: bool | None = None,
                         choice: str | None = None, remember: bool = False, ctx: Context | None = None) -> dict[str, Any]:
        """Continue a task that returned need_input (pass inputs), need_confirm (pass confirm) or ambiguous
        (pass choice). A chosen option still meets the safety floor — picking one says which, not whether —
        so pass confirm=true alongside choice to answer both at once.

        When `pending.remember` is present the person may have this confirmation remembered, so the same
        action in the same app is not asked about again. That is theirs to decide, never the caller's: ask
        them, and follow `pending.remember_how`. remember=true is honoured only where the person has turned
        `grants.from_mcp` on."""
        keep, note = _may_remember(remember)
        res = await anyio.to_thread.run_sync(lambda: eng.resume(task_id, inputs, confirm, choice, reporter(ctx), keep))
        return {**res, **note}

    @mcp.tool()
    async def mac_feedback(task_id: str, ok: bool, note: str = "") -> dict[str, Any]:
        """Tell the engine whether a finished task really did what was asked; a wrong "done" is then not learned."""
        return eng.feedback(task_id, ok, note)

    @mcp.tool()
    async def mac_cancel(task_id: str) -> dict[str, Any]:
        """Stop a running or pending task."""
        return eng.cancel(task_id)

    @mcp.tool()
    async def mac_observe(app: str | None = None, goal: str = "", limit: int = 120,
                          inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """What can be done right now: affordances (menus, buttons, fields, apps, keys…) of `app` (default frontmost),
        in a fixed provider order (window, menus, keys, apps…), plus the readable screen text. `inputs` is what
        mac_do takes: a path in it is offered for opening, `controls` become holdable options."""
        return await anyio.to_thread.run_sync(lambda: eng.observe(app, goal, inputs, limit, redact=redact))

    @mcp.tool()
    async def mac_act(affordance_id: str, params: dict[str, Any] | None = None, confirm: bool = False,
                      remember: bool = False) -> dict[str, Any]:
        """Execute one affordance from the latest mac_observe. Irreversible actions need confirm=true.
        remember=true keeps that confirmation as a standing grant — see mac_resume for who may give one."""
        keep, note = _may_remember(remember)
        res = await anyio.to_thread.run_sync(lambda: eng.act(affordance_id, params, confirm, keep, redact=redact))
        return {**res, **note}

    @mcp.tool()
    async def mac_status(task_id: str | None = None) -> dict[str, Any]:
        """Helper permissions, decider, cost so far, queue, dry-run state — or, with a task_id (from mac_submit
        or mac_do), that task: its status, result so far, and its place in the queue while it waits."""
        return await anyio.to_thread.run_sync(lambda: eng.status(task_id))

    return mcp


def serve(cfg: Config | None = None) -> None:
    cfg = cfg or Config.load()
    mcp = build(cfg)
    transport = cfg.get("server.transport", "stdio")
    try:
        if transport == "stdio":
            mcp.run("stdio")
        else:
            mcp.run(transport, host=cfg.get("server.host", "127.0.0.1"), port=int(cfg.get("server.port", 8977)))
    finally:   # the helper child, Chromium and the task database do not go away on their own
        engine = getattr(mcp, "_macwork_engine", None)
        if engine is not None:
            engine.close()
