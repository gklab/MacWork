"""macwork — the command line.

  macwork doctor                         permissions, helper, decider
  macwork observe [--app A] [--goal G]   what can be done right now
  macwork surfaces [--app A]             read-only: what each capability surface can see, and how long it takes
  macwork do "goal" [-i key=value] [--app A]   let the decider drive (asks you when it must)
  macwork serve [--transport T]          MCP server
  macwork key set [typesafe|deepseek|…]  store an API key in the Keychain
  macwork learn <app>                    explore an app safely ahead of time (builds its model)
  macwork skills [--delete ID]           learned routines
  macwork eval [--only a,b]              run the unseen-app suite and write a report
  macwork helper build|install|path      build / install the native helper app
  macwork daemon install|uninstall|status  keep the engine running as a LaunchAgent
  macwork audit [-n N]                   what was sent to the decider
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config, expand

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "dev.macwork.helper"


def _print(obj: Any, as_json: bool = True) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json else obj)


def _inputs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .engine import Engine

    eng = Engine(cfg)
    st = eng.status()
    probed = None
    if getattr(args, "planners", False):
        from .planner import probe_planners
        st["planners"] = probed = probe_planners(cfg, eng.helper)
    _print(st)
    ok = st.get("helper", {}).get("ax_trusted") and "error" not in st.get("decider", {})
    if probed is not None and str(st.get("decider", {}).get("using") or "").startswith("local:"):
        backend = str(st["decider"]["using"]).split(":", 1)[1]
        answer = next((p for p in probed if p.get("planner") == backend), None)
        if answer and answer.get("status") != "ok":
            print(f"\n→ the decider is {st['decider']['using']} and {backend} says: {answer.get('detail') or answer['status']}.\n"
                  f"  Nothing can decide, so nothing will run: set TYPESAFE_API_KEY, or fix that backend.", file=sys.stderr)
            ok = False
    if getattr(args, "ask", False):
        # The Mac will not show these prompts for someone else: they have to be asked for by the process that
        # wants the permission, which is the helper. `permissions.request` existed for this and had no caller,
        # so the only advice on offer was a path to click through by hand.
        for kind in ("accessibility", "screen_capture"):
            try:
                got = eng.helper.call("permissions.request", kind=kind, timeout=20).get("granted")
            except Exception as exc:                                        # noqa: BLE001
                print(f"→ could not ask for {kind}: {exc}", file=sys.stderr)
                continue
            print(f"→ {kind}: {'granted' if got else 'not granted — the prompt is open, or it was refused before'}")
        print("→ a permission refused once is not asked again: System Settings ▸ Privacy & Security", file=sys.stderr)
        return 0
    if not st.get("helper", {}).get("ax_trusted"):
        print("\n→ grant Accessibility to the helper: `macwork doctor --ask` opens the prompts, or "
              "System Settings ▸ Privacy & Security ▸ Accessibility", file=sys.stderr)
    using = str(st.get("decider", {}).get("using") or "")
    route = None
    if "error" not in st.get("decider", {}):
        try:
            route = getattr(getattr(eng, "decider", None), "route_ms", lambda: None)()
        except Exception:                           # noqa: BLE001  (a diagnostic line must not fail the doctor)
            route = None
    if route is not None:
        # what a step costs before the engine does anything at all; see JevDecider.route_ms. The store keeps
        # tasks 30 minutes, and an empty one made this line say nothing: the audit keeps every step.
        from .profile import audit_files, profile, profile_audit
        from .store import Store
        stages = profile(Store(cfg).path)["stages"] or \
            profile_audit(audit_files(expand(cfg.get("audit.path")), int(cfg.get("audit.keep", 3))))["stages"]
        step = sum(v["median_ms"] for k, v in stages.items() if k in ("observe", "decide", "act", "wait"))
        share = f" — about {route / step:.0%} of a {step:.0f} ms step on this Mac" if step else ""
        print(f"\n→ one round trip to the decider takes {route:.0f} ms from this network{share}.", file=sys.stderr)
        if route > 250:
            print("  That is the floor under every decision, and it is the route, not the engine: a request a game\n"
                  "  would send costs nearly the same from here. A proxy rule that sends the decider's host through a\n"
                  "  faster node is worth more than any optimisation inside this program.", file=sys.stderr)
    if using.startswith("local:"):
        # `auto` fell back, and both halves of that are worth saying: this decider's confidence is not
        # calibrated, and whether the backend answers at all is not something `status()` can know without
        # a round trip — `--planners` is what asks it. Reporting "using: local:x" on its own reads as green
        # while that backend may be handing back 401.
        print(f"\n→ the decider fell back to {using}: no TypeSafe key, so its confidence is a model's word "
              f"rather than a measured frequency (capped by decider.local.confidence_ceiling).\n"
              f"  Every threshold in config.yaml is a cut-off on a calibrated probability, so none of them\n"
              f"  apply to it. The safety floor asks it a plain yes/no instead — which means that on a Mac\n"
              f"  whose language policy.yaml's word lists do not cover, one answer from a text model is all\n"
              f"  that stands between the engine and an irreversible action.\n"
              f"  `macwork doctor --planners` asks that backend for a real answer; this line does not.",
              file=sys.stderr)
    if st.get("helper", {}).get("secure_input"):
        # not a failure: it comes and goes with whatever has focus. But while it is on, nothing can be typed.
        print("\n→ secure input is on right now (something with a password field has focus): keystrokes are refused",
              file=sys.stderr)
    return 0 if ok else 1


def cmd_surfaces(cfg: Config, args: argparse.Namespace) -> int:
    """What the Mac can tell us right now, surface by surface. Read-only: nothing is acted on, nothing is sent
    to the decider. This is how the capability surfaces are checked without running a task."""
    from .engine import Engine

    eng = Engine(cfg)
    res = eng.observe(args.app, limit=int(args.limit))
    notes, affs = res["notes"], res["affordances"]
    by_channel: dict[str, list[str]] = {}
    for a in affs:
        by_channel.setdefault(a["channel"], []).append(a["label"])

    print(f"app: {(res.get('app') or {}).get('name')}   window: {res.get('window')}   "
          f"offered: {res['total']}   total observed: {sum(v for k, v in notes.items() if k.endswith('_n'))}\n")
    print(f"{'provider':18} {'found':>6} {'ms':>6}  note")
    for name in cfg.get("observe.providers") or []:
        n, ms = notes.get(f"{name}_n"), notes.get(f"{name}_ms")
        print(f"{name:18} {n if n is not None else '-':>6} {ms if ms is not None else '-':>6}  "
              f"{notes.get(f'{name}_error') or ''}")

    print(f"\n{'channel':18} {'offered':>7}  example")
    for channel, labels in sorted(by_channel.items(), key=lambda kv: -len(kv[1])):
        print(f"{channel:18} {len(labels):>7}  {labels[0][:64]}")

    print()
    for label, value in _extra_surfaces(eng).items():
        print(f"{label:18} {value}")
    return 0


def _extra_surfaces(eng: Any) -> dict[str, Any]:
    """Facts that are not affordances: how much of the Mac each enumeration actually reaches."""
    from .appmodel import parse_app_intents
    out: dict[str, Any] = {}
    try:
        from .observe import installed_apps
        dirs = eng.cfg.get("observe.apps.dirs") or []
        out["apps"] = (f"{len(installed_apps(eng.cfg, eng.helper))} via LaunchServices, "
                       f"{len(eng.helper.call('apps.installed', dirs=dirs, source='dirs'))} by scanning folders")
    except Exception as exc:  # noqa: BLE001
        out["apps (dirs)"] = f"error: {exc}"
    try:
        on = eng.helper.call("screen.windows")
        every = eng.helper.call("screen.windows", all=True)
        out["windows"] = f"{len(on)} on this Space, {len(every)} in all"
    except Exception as exc:  # noqa: BLE001
        out["windows"] = f"error: {exc}"
    try:
        out["clipboard"] = eng.helper.call("clipboard.read")
    except Exception as exc:  # noqa: BLE001
        out["clipboard"] = f"error: {exc}"
    try:
        running = eng.helper.call("apps.running")
        counts = {a.get("name"): len(parse_app_intents(a["path"])) for a in running if a.get("path")}
        have = {k: v for k, v in counts.items() if v}
        out["app intents"] = f"{sum(have.values())} actions declared by {len(have)} of {len(counts)} running apps"
    except Exception as exc:  # noqa: BLE001
        out["app intents"] = f"error: {exc}"
    return out


def cmd_observe(cfg: Config, args: argparse.Namespace) -> int:
    from .engine import Engine

    res = Engine(cfg).observe(args.app, args.goal or "", _inputs(args.input), args.limit)
    if args.json:
        _print(res)
        return 0
    app = res.get("app") or {}
    print(f"app: {app.get('name')}  window: {res.get('window')}  total affordances: {res['total']}  notes: {res['notes']}")
    for a in res["affordances"]:
        print(f"  {a['id']:>8}  [{a['channel']}] {a['label']}" + (f"  — {a['context']}" if a.get("context") else ""))
    if res.get("screen_text"):
        print("\nscreen text:\n  " + res["screen_text"][:600].replace("\n", "\n  "))
    return 0


def cmd_do(cfg: Config, args: argparse.Namespace) -> int:
    from .engine import Engine

    eng = Engine(cfg)
    res = eng.do(args.goal, _inputs(args.input), args.app, progress=lambda m: print(f"  → {m}", file=sys.stderr))
    while res["status"] in ("need_input", "need_confirm", "ambiguous", "need_continue") and sys.stdin.isatty():
        p = res.get("pending", {})
        if res["status"] == "need_continue":
            print(f"  → {res.get('reason')}; carrying on", file=sys.stderr)
            res = eng.resume(res["task_id"], progress=lambda m: print(f"  → {m}", file=sys.stderr))
            continue
        if res["status"] == "need_confirm":
            # "a": yes, and remember it — offered only where a standing grant may be given at all. This
            # prompt is a terminal with a person at it, which is exactly who may give one.
            can = bool(p.get("remember"))
            said = input(f"confirm: {p['confirm']['label']} ? [y/N{'/a = always allow this here' if can else ''}] ").strip().lower()
            res = eng.resume(res["task_id"], confirm=said in ("y", "a") and (can or said == "y"),
                             remember=can and said == "a", progress=lambda m: print(f"  → {m}", file=sys.stderr))
        elif res["status"] == "need_input":
            vals = {k: input(f"{k} ({d}): ") for k, d in p.get("inputs", {}).items()}
            res = eng.resume(res["task_id"], inputs=vals, progress=lambda m: print(f"  → {m}", file=sys.stderr))
        else:
            for o in p.get("choose_one_of", []):
                print(f"  {o['id']}: {o['label']} (p={o['p']})")
            res = eng.resume(res["task_id"], choice_id=input("choose id: ").strip(), progress=lambda m: print(f"  → {m}", file=sys.stderr))
    _print(res)
    return 0 if res["status"] == "done" else 1


def cmd_revert(cfg: Config, args: argparse.Namespace) -> int:
    """Put back what a task changed, most recent change first."""
    from .engine import Engine

    res = Engine(cfg).revert(args.task_id, args.max_steps)
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    """The engine's state, or — given a task id — that task's, from the store: a task that ran under
    `macwork serve` is on disk and can be read here."""
    from .engine import Engine

    _print(Engine(cfg).status(args.task_id))
    return 0


def cmd_tidy(cfg: Config, args: argparse.Namespace) -> int:
    """Close what a task opened and no longer needs (`engine.tidy: never` leaves that to this command)."""
    from .engine import Engine

    eng = Engine(cfg)
    if eng.tasks.get(args.task_id) is None and eng._recall(args.task_id) is None:
        print(f"unknown or expired task {args.task_id}", file=sys.stderr)
        return 1
    _print(eng.tidy(args.task_id))
    return 0


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    """Wait for the Mac to announce something and hand the matching work to the engine."""
    from .engine import Engine
    from .watch import Watcher, triggers_path

    eng = Engine(cfg)
    w = Watcher(eng, cfg)
    if not w.triggers:
        print(f"no triggers in {triggers_path(cfg)} — see docs/triggers.md for the shape", file=sys.stderr)
        return 1
    started = w.start()
    _print(started)
    if args.check:                       # what it would watch, without waiting for anything
        w.stop()
        return 0
    print(f"watching; {len(w.triggers)} trigger(s). ^C to stop.", file=sys.stderr)
    try:
        w.run(args.every, args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
        eng.close()
    _print({"fired": w.fired})
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .server import serve

    if args.transport:
        cfg.docs["config"].setdefault("server", {})["transport"] = args.transport
    if args.port:
        cfg.docs["config"].setdefault("server", {})["port"] = args.port
    serve(cfg)
    return 0


def cmd_key(cfg: Config, args: argparse.Namespace) -> int:
    from .decider import store_keychain_key

    name = args.name or "typesafe"
    key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass(f"{name} API key: ").strip()
    if not key:
        print("no key given", file=sys.stderr)
        return 1
    service = cfg.get("decider.keychain_service", "macwork")
    store_keychain_key(service, key, account=name)
    print(f"stored in the login Keychain (service {service}, account {name})")
    return 0


def cmd_grants(cfg: Config, args: argparse.Namespace) -> int:
    """Standing grants: list them, give one for a task that is waiting on a confirmation, take them back."""
    from .grants import Grants
    from .privacy import Audit
    from .store import Store

    grants = Grants(cfg)
    if args.action == "allow":
        if not args.id:
            print("which task? macwork grants allow <task-id>", file=sys.stderr)
            return 1
        task = Store(cfg).get(args.id)
        offer = (task.pending or {}).get("remember") if task else None
        if not offer:
            print("no such task" if task is None else
                  "that task is not waiting on a confirmation that may be remembered", file=sys.stderr)
            return 1
        row = grants.add(offer, "cli")
        Audit(cfg).record("grant", task=args.id, id=row["id"], app=row.get("bundle_id"), action=row.get("action"),
                          because=row.get("because"), source="cli")
        print(f"{row['id']}  {row.get('app') or row.get('bundle_id') or '(no app)'}: {row.get('action')}\n"
              f"will not be asked about again. It still has to be confirmed for the waiting task "
              f"(mac_resume confirm=true). Take it back with: macwork grants revoke {row['id']}")
        return 0
    if args.action in ("revoke", "clear"):
        if args.action == "revoke" and not args.id:
            print("which one? macwork grants revoke <id>   (macwork grants clear removes all)", file=sys.stderr)
            return 1
        try:
            n = grants.revoke(args.id if args.action == "revoke" else None)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        Audit(cfg).record("grant", revoked=args.id or "all", count=n)
        print(f"{n} grant{'s' if n != 1 else ''} taken back")
        return 0 if n or args.action == "clear" else 1
    rows = grants.all()
    if not rows:
        print("no standing grants: every gated action asks")
    for r in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(r.get("granted_at", 0)))
        print(f"{r['id']}  {(r.get('app') or r.get('bundle_id') or '(no app)'):<18} {r.get('action', '')[:70]}\n"
              f"{'':14}because {', '.join(r.get('because') or [])} · granted {when} via {r.get('source')} · used {r.get('used', 0)}×")
    return 0


def cmd_learn(cfg: Config, args: argparse.Namespace) -> int:
    from .engine import Engine

    _print(Engine(cfg).learn(args.app, progress=lambda m: print(f"  → {m}", file=sys.stderr)))
    return 0


def cmd_skills(cfg: Config, args: argparse.Namespace) -> int:
    from .skills import Skills

    store = Skills(cfg)
    if args.delete:
        f = store.dir / f"{args.delete}.json"
        if f.exists():
            f.unlink()
            print(f"deleted {args.delete}")
        return 0
    for sk in store.all():
        print(f"{sk['id']}  uses={sk.get('uses', 0)} fails={sk.get('fails', 0)}  {sk['goal']}")
        print("      " + " → ".join(s["label"] for s in sk["steps"]))
    return 0


def cmd_eval(cfg: Config, args: argparse.Namespace) -> int:
    from .evals import report_from, run_suite

    out = Path(args.out) if args.out else ROOT / "evals" / "reports"
    if args.report_from:
        # one report from the rows of runs already made: a run stopped half way and the rest of it, say
        try:
            report = report_from([Path(x) for x in args.report_from], out)
        except (OSError, ValueError) as exc:
            print(f"no report: {exc}", file=sys.stderr)
            return 2
        _print(report["summary"])
        return 0
    from .engine import Engine

    suite = Path(args.suite) if args.suite else ROOT / "evals" / "unseen.yaml"
    report = run_suite(Engine(cfg), suite, [x for x in (args.only or "").split(",") if x] or None,
                       out, progress=lambda m: print(m, file=sys.stderr),
                       repeat=args.repeat, same_draw=Path(args.same_draw) if args.same_draw else None)
    _print(report["summary"])
    if report["summary"].get("interrupted"):
        print(f"\ninterrupted: the report holds what ran ({', '.join(report.get('files') or [])})", file=sys.stderr)
        return 130
    if args.compare:
        # a total on its own reads like progress whether or not anything moved; this says which tasks did
        from .evals import baseline_for, compare, format_compare

        against = baseline_for(suite) if args.compare == "baseline" else Path(args.compare)
        if not against.exists():
            print(f"\nno baseline to compare with: {against} (see evals/baseline/README.md)", file=sys.stderr)
            return 0
        base = json.loads(against.read_text(encoding="utf-8"))
        print("\nagainst " + str(against), file=sys.stderr)
        print(format_compare(compare(base, report)), file=sys.stderr)
    return 0


def cmd_profile(cfg: Config, args: argparse.Namespace) -> int:
    """Where the time goes, from the tasks this Mac has actually run: the audit's step records by default,
    only one eval run's tasks with --report (and that report's own rows once the audit has rotated), or the
    task store's older view with --store. Drives nothing."""
    from .profile import audit_files, format_profile, format_timing, profile, profile_audit, profile_report
    if args.store:
        from .store import Store
        got = profile(Store(cfg).path, args.n)
        _print(got if args.json else format_profile(got), as_json=args.json)
        return 0
    report, ids = None, None
    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        ids = {str(r["task_id"]) for r in report.get("rows") or [] if r.get("task_id")}
    got = profile_audit(audit_files(expand(cfg.get("audit.path")), int(cfg.get("audit.keep", 3))), ids)
    if not got["steps"] and report is not None:
        got = profile_report(report)          # the audit has rotated past this run: its rows keep the totals
    _print(got if args.json else format_timing(got), as_json=args.json)
    return 0


def cmd_compare(cfg: Config, args: argparse.Namespace) -> int:
    """Did anything actually change between two eval runs? Reads two reports; runs nothing. Given one
    report, the other is the committed baseline for its suite."""
    from .evals import baseline_for, compare_files, format_compare

    before, after = Path(args.before), Path(args.after) if args.after else None
    if after is None:
        after = before
        suite = (json.loads(after.read_text(encoding="utf-8")).get("summary") or {}).get("suite") or ""
        before = baseline_for(suite)
        if not before.exists():
            print(f"no baseline for {suite or 'this suite'}: {before} (see evals/baseline/README.md)", file=sys.stderr)
            return 1
        print(f"against the baseline {before}", file=sys.stderr)
    c = compare_files(before, after)
    if args.json:
        _print(c)
    else:
        print(format_compare(c))
    return 2 if c.get("refused") else 0


def _swift_env() -> dict[str, str]:
    env = dict(os.environ)
    if subprocess.run(["xcrun", "--find", "swift"], capture_output=True, check=False).returncode != 0 or \
            subprocess.run(["swift", "--version"], capture_output=True, check=False).returncode != 0:
        env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"   # Xcode present but its license not accepted
    return env


def signing_identity() -> str | None:
    """A Developer ID on this Mac, if there is one.

    Accessibility is granted to a *code signature*, not to a path. Ad-hoc signing gives a new one on every
    rebuild, so the permission has to be granted again every time — which is why the helper could never
    simply stay installed. A Developer ID keeps the same identity across rebuilds. Never written into the
    repo: it is asked of the Keychain, and `--sign` overrides.
    """
    r = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                       capture_output=True, text=True, check=False)
    for line in (r.stdout or "").splitlines():
        if '"Developer ID Application:' in line:
            return line.split('"')[1]
    return None


DAEMON_PLIST = "dev.macwork.agent"


def daemon_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{DAEMON_PLIST}.plist"


def cmd_daemon(cfg: Config, args: argparse.Namespace) -> int:
    """Keep the engine running, so a caller can reach it without starting it first."""
    path = daemon_path()
    if args.action == "status":
        r = subprocess.run(["launchctl", "list", DAEMON_PLIST], capture_output=True, text=True, check=False)
        _print({"plist": str(path) if path.exists() else None, "loaded": r.returncode == 0,
                "detail": (r.stdout or r.stderr).strip()[:400] or None})
        return 0
    if args.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{DAEMON_PLIST}"], capture_output=True, check=False)
        path.unlink(missing_ok=True)
        print(f"removed {path}")
        return 0

    exe = shutil.which("macwork") or str(Path(sys.executable).parent / "macwork")
    if not Path(exe).exists():
        print("macwork is not on PATH: install the package first (pip install -e .)", file=sys.stderr)
        return 1
    logs = Path("~/Library/Logs/macwork").expanduser()
    logs.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": DAEMON_PLIST,
        "ProgramArguments": [exe, "serve", "--transport", args.transport],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # come back from a crash, stay down after a clean stop
        "ProcessType": "Interactive",             # it drives the UI: not to be throttled as a background job
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{DAEMON_PLIST}"], capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True, check=False)
    print(f"wrote {path}")
    if r.returncode != 0:
        print(f"→ launchctl said: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
        return 1
    print(f"the engine is running at http://{cfg.get('server.host', '127.0.0.1')}:{cfg.get('server.port', 8977)}/mcp"
          if args.transport != "stdio" else "loaded")
    return 0


def cmd_helper(cfg: Config, args: argparse.Namespace) -> int:
    src = ROOT / "helper"
    binary = src / ".build" / "release" / "macwork-helper"
    app = expand(cfg.get("helper.app"))
    if args.action == "path":
        _print({"source": str(src), "binary": str(binary) if binary.exists() else None, "app": str(app) if app and app.exists() else None})
        return 0
    if args.action in ("build", "install"):
        r = subprocess.run(["swift", "build", "-c", "release"], cwd=src, env=_swift_env(), check=False)
        if r.returncode != 0:
            return r.returncode
    if args.action == "install":
        assert app is not None
        macos = app / "Contents" / "MacOS"
        if app.exists():
            shutil.rmtree(app)
        macos.mkdir(parents=True)
        shutil.copy2(binary, macos / "macwork-helper")
        plist = {
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "MacWork Helper", "CFBundleDisplayName": "MacWork Helper",
            "CFBundleExecutable": "macwork-helper", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
            "CFBundleShortVersionString": "0.1.0", "LSUIElement": True, "LSMinimumSystemVersion": "13.0",
            "NSAppleEventsUsageDescription": "macwork operates apps on your behalf when you ask it to.",
        }
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
        sign = args.sign or signing_identity() or "-"
        cmd = ["codesign", "--force", "--sign", sign, "--identifier", BUNDLE_ID]
        if sign != "-":
            cmd += ["--options", "runtime", "--timestamp"]   # what a Developer ID signature is expected to carry
        subprocess.run(cmd + [str(app)], check=True)
        print(f"installed {app} (signed with {'ad-hoc' if sign == '-' else sign})")
        print("next: open it once and grant Accessibility: System Settings ▸ Privacy & Security ▸ Accessibility ▸ MacWork Helper")
        if sign == "-":
            print("note: no Developer ID found, so this is ad-hoc — macOS ties Accessibility to the signature, and an\n"
                  "      ad-hoc one changes on every rebuild, so the permission has to be granted again each time")
    return 0


def cmd_audit(cfg: Config, args: argparse.Namespace) -> int:
    path = expand(cfg.get("audit.path"))
    if not path or not path.exists():
        print("no audit log yet")
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[-args.n:]:
        print(line)
    return 0


def cmd_selftest(cfg: Config, args: argparse.Namespace) -> int:
    """Run the paths unit tests only fake: planners, learning an app, replaying a routine (all harmless)."""
    from .selftest import run

    res = run(cfg, lambda m: print(m, file=sys.stderr), learn=not args.skip_learn, replay=not args.skip_replay)
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_privacy_check(cfg: Config, args: argparse.Namespace) -> int:
    """Run the synthetic corpus through the real redactor and on-device tagger; report leaks and over-redaction."""
    from .engine import Engine
    from .privacycheck import run

    eng = Engine(cfg)
    _print(run(cfg, eng._entities, eng._mac_vocabulary(), detect=eng._detect))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="macwork", description="Take over a Mac with native interfaces and TypeSafe Jev.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor")
    p.add_argument("--ask", action="store_true", help="ask the Mac for the permissions the helper needs (opens the system prompts)")
    p.add_argument("--planners", action="store_true", help="also ask every configured planner for a tiny real plan")
    p = sub.add_parser("surfaces", help="read-only: what each provider can see right now, and how long it took")
    p.add_argument("--app")
    p.add_argument("--limit", type=int, default=400)
    p = sub.add_parser("observe")
    p.add_argument("--app")
    p.add_argument("--goal")
    p.add_argument("-i", "--input", action="append", default=[])
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("do")
    p.add_argument("goal")
    p.add_argument("--app")
    p.add_argument("-i", "--input", action="append", default=[], help="key=value (text, query, url, file…)")
    p = sub.add_parser("revert", help="undo what a task changed (its own apps' undo commands, newest first)")
    p.add_argument("task_id")
    p.add_argument("--max-steps", type=int, default=None, help="how far back to go (default: engine.revert.max_steps)")
    p = sub.add_parser("status", help="the engine's state, or a task's (its result so far and its place in the queue)")
    p.add_argument("task_id", nargs="?")
    p = sub.add_parser("tidy", help="close what a task opened and no longer needs")
    p.add_argument("task_id")
    p = sub.add_parser("watch", help="run tasks when the Mac announces something (~/.config/macwork/triggers.yaml)")
    p.add_argument("--check", action="store_true", help="say what would be watched and stop")
    p.add_argument("--every", type=float, default=None, help="seconds between polls (default: watch.poll_s)")
    p.add_argument("--seconds", type=float, default=None, help="stop after this long (default: never)")
    p = sub.add_parser("serve")
    p.add_argument("--transport", choices=["stdio", "streamable-http"])
    p.add_argument("--port", type=int)
    p = sub.add_parser("key")
    p.add_argument("action", choices=["set"])
    p.add_argument("name", nargs="?", help="typesafe (default), deepseek, or any keychain_account from the config")
    p = sub.add_parser("grants")
    p.add_argument("action", nargs="?", default="list", choices=["list", "allow", "revoke", "clear"])
    p.add_argument("id", nargs="?", help="allow: a task id · revoke: a grant id (or the start of one)")
    p = sub.add_parser("learn")
    p.add_argument("app")
    p = sub.add_parser("skills")
    p.add_argument("--delete")
    p = sub.add_parser("eval")
    p.add_argument("--suite")
    p.add_argument("--only", help="comma-separated task ids")
    p.add_argument("--repeat", type=int, help="runs per task (default: the suite's repeat, else 1)")
    p.add_argument("--out")
    p.add_argument("--compare", nargs="?", const="baseline",
                   help="say which tasks moved against an earlier report.json, and whether that is more than chance; "
                        "with no path, against the committed baseline for the suite (evals/baseline/)")
    p.add_argument("--report-from", nargs="+", metavar="ROWS",
                   help="build one report from the <stamp>.rows.jsonl files of runs already made (one suite, one commit); runs nothing")
    p.add_argument("--same-draw", metavar="REPORT",
                   help="a sampled suite takes exactly the apps this earlier report drew, so the two runs can be paired")
    p = sub.add_parser("profile", help="where a task's time goes: each stage's share, the planner's, the slowest providers (reads the audit)")
    p.add_argument("--report", metavar="REPORT", help="only this eval run's tasks (from its report.json; its rows once the audit has rotated)")
    p.add_argument("--store", action="store_true", help="the task store's view: stage timings and wasted steps of tasks still kept")
    p.add_argument("-n", type=int, default=50, help="with --store: how many recent tasks to read")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compare", help="compare two eval reports (runs nothing); one report is compared with the committed baseline")
    p.add_argument("before")
    p.add_argument("after", nargs="?")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("helper")
    p.add_argument("action", choices=["build", "install", "path"])
    p.add_argument("--sign", help="codesign identity (default: ad-hoc)")
    p = sub.add_parser("daemon", help="keep the engine running (a LaunchAgent), so callers need not start it")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--transport", default="streamable-http", choices=["stdio", "streamable-http"])
    p = sub.add_parser("audit")
    p.add_argument("-n", type=int, default=20)
    sub.add_parser("privacy-check", help="measure redaction on a synthetic corpus (leaks, over-redaction)")
    p = sub.add_parser("selftest", help="run planners, learn and routine replay for real (harmless)")
    p.add_argument("--skip-learn", action="store_true")
    p.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.load()
    handlers = {"doctor": cmd_doctor, "observe": cmd_observe, "do": cmd_do, "revert": cmd_revert, "status": cmd_status, "tidy": cmd_tidy, "watch": cmd_watch, "serve": cmd_serve,
                "key": cmd_key, "grants": cmd_grants, "helper": cmd_helper, "surfaces": cmd_surfaces, "daemon": cmd_daemon, "audit": cmd_audit, "learn": cmd_learn, "skills": cmd_skills, "eval": cmd_eval, "compare": cmd_compare, "profile": cmd_profile, "privacy-check": cmd_privacy_check, "selftest": cmd_selftest}
    missing = {c for c in sub.choices} - set(handlers)   # a subcommand with no handler is a KeyError at run time
    assert not missing, f"no handler for {missing}"
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
