"""MCP server: the Mac as tools, at three levels of control.

* ``mac_do`` / ``mac_resume`` / ``mac_cancel`` — give a goal; the decider drives; answer what it asks
* ``mac_observe`` / ``mac_act``                — see the affordances and pick yourself (no decider involved)
* ``web_research``                             — look something up on the web and get the relevant passages
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
common keys: text, query, url, file). need_confirm: ask the user, then mac_resume(confirm=true/false).
ambiguous: mac_resume(choice=<id>) with one of the offered ids. To drive step by step yourself, call
mac_observe then mac_act with an affordance id from that observation."""


def build(cfg: Config | None = None, engine: Engine | None = None) -> Any:
    cfg = cfg or Config.load()
    eng = engine or Engine(cfg)
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
    async def mac_resume(task_id: str, inputs: dict[str, Any] | None = None, confirm: bool | None = None,
                         choice: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Continue a task that returned need_input (pass inputs), need_confirm (pass confirm) or ambiguous (pass choice)."""
        return await anyio.to_thread.run_sync(lambda: eng.resume(task_id, inputs, confirm, choice, reporter(ctx)))

    @mcp.tool()
    async def mac_feedback(task_id: str, ok: bool, note: str = "") -> dict[str, Any]:
        """Tell the engine whether a finished task really did what was asked; a wrong "done" is then not learned."""
        return eng.feedback(task_id, ok, note)

    @mcp.tool()
    async def mac_cancel(task_id: str) -> dict[str, Any]:
        """Stop a running or pending task."""
        return eng.cancel(task_id)

    @mcp.tool()
    async def mac_observe(app: str | None = None, goal: str = "", limit: int = 120) -> dict[str, Any]:
        """What can be done right now: affordances (menus, buttons, fields, apps, keys…) of `app` (default frontmost),
        in a fixed provider order (window, menus, keys, apps…), plus the readable screen text."""
        return await anyio.to_thread.run_sync(lambda: eng.observe(app, goal, None, limit))

    @mcp.tool()
    async def mac_act(affordance_id: str, params: dict[str, Any] | None = None, confirm: bool = False) -> dict[str, Any]:
        """Execute one affordance from the latest mac_observe. Irreversible actions need confirm=true."""
        return await anyio.to_thread.run_sync(lambda: eng.act(affordance_id, params, confirm))

    @mcp.tool()
    async def web_research(goal: str, query: str = "", url: str = "") -> dict[str, Any]:
        """Search the web (or start at `url`), open and read pages, and return the passages relevant to `goal` with sources."""
        from .web import research

        def run() -> dict[str, Any]:
            eng.system()          # so the browser opens with this Mac's locale, not one written into the code
            return research(cfg, eng.gate, eng.redactor("web"), goal=goal, query=query, url=url, cache=eng.cache)
        return await anyio.to_thread.run_sync(run)

    @mcp.tool()
    async def mac_status() -> dict[str, Any]:
        """Helper permissions, decider, cost so far, dry-run state."""
        return await anyio.to_thread.run_sync(eng.status)

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
