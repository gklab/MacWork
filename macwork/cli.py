"""macwork — the command line.

  macwork doctor                         permissions, helper, decider
  macwork observe [--app A] [--goal G]   what can be done right now
  macwork surfaces [--app A]             read-only: what each capability surface can see, and how long it takes
  macwork do "goal" [-i key=value] [--app A]   let the decider drive (asks you when it must)
  macwork web "goal" [--query Q] [--url U]     web research
  macwork serve [--transport T]          MCP server
  macwork key set [typesafe|deepseek|…]  store an API key in the Keychain
  macwork learn <app>                    explore an app safely ahead of time (builds its model)
  macwork skills [--delete ID]           learned routines
  macwork eval [--only a,b]              run the unseen-app suite and write a report
  macwork helper build|install|path      build / install the native helper app
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
    if getattr(args, "planners", False):
        from .planner import probe_planners
        st["planners"] = probe_planners(cfg, eng.helper)
    _print(st)
    ok = st.get("helper", {}).get("ax_trusted") and "error" not in st.get("decider", {})
    if not st.get("helper", {}).get("ax_trusted"):
        print("\n→ grant Accessibility to the helper (System Settings ▸ Privacy & Security ▸ Accessibility)", file=sys.stderr)
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
    while res["status"] in ("need_input", "need_confirm", "ambiguous") and sys.stdin.isatty():
        p = res.get("pending", {})
        if res["status"] == "need_confirm":
            ok = input(f"confirm: {p['confirm']['label']} ? [y/N] ").strip().lower() == "y"
            res = eng.resume(res["task_id"], confirm=ok, progress=lambda m: print(f"  → {m}", file=sys.stderr))
        elif res["status"] == "need_input":
            vals = {k: input(f"{k} ({d}): ") for k, d in p.get("inputs", {}).items()}
            res = eng.resume(res["task_id"], inputs=vals, progress=lambda m: print(f"  → {m}", file=sys.stderr))
        else:
            for o in p.get("choose_one_of", []):
                print(f"  {o['id']}: {o['label']} (p={o['p']})")
            res = eng.resume(res["task_id"], choice_id=input("choose id: ").strip(), progress=lambda m: print(f"  → {m}", file=sys.stderr))
    _print(res)
    return 0 if res["status"] == "done" else 1


def cmd_web(cfg: Config, args: argparse.Namespace) -> int:
    from .engine import Engine
    from .web import research

    eng = Engine(cfg)
    eng.system()
    _print(research(cfg, eng.gate, eng.redactor("web"), goal=args.goal, query=args.query or "", url=args.url or "", cache=eng.cache))
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
    from .engine import Engine
    from .evals import run_suite

    suite = Path(args.suite) if args.suite else ROOT / "evals" / "unseen.yaml"
    report = run_suite(Engine(cfg), suite, [x for x in (args.only or "").split(",") if x] or None,
                       Path(args.out) if args.out else ROOT / "evals" / "reports", progress=lambda m: print(m, file=sys.stderr),
                       repeat=args.repeat)
    _print(report["summary"])
    return 0


def _swift_env() -> dict[str, str]:
    env = dict(os.environ)
    if subprocess.run(["xcrun", "--find", "swift"], capture_output=True, check=False).returncode != 0 or \
            subprocess.run(["swift", "--version"], capture_output=True, check=False).returncode != 0:
        env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"   # Xcode present but its license not accepted
    return env


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
        sign = args.sign or "-"
        subprocess.run(["codesign", "--force", "--sign", sign, "--identifier", BUNDLE_ID, str(app)], check=True)
        print(f"installed {app} (signed with {'ad-hoc' if sign == '-' else sign})")
        print("next: open it once and grant Accessibility: System Settings ▸ Privacy & Security ▸ Accessibility ▸ MacWork Helper")
        if sign == "-":
            print("note: ad-hoc signatures change on every rebuild, so macOS asks for the permission again after reinstalling")
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
    p = sub.add_parser("web")
    p.add_argument("goal")
    p.add_argument("--query")
    p.add_argument("--url")
    p = sub.add_parser("serve")
    p.add_argument("--transport", choices=["stdio", "streamable-http"])
    p.add_argument("--port", type=int)
    p = sub.add_parser("key")
    p.add_argument("action", choices=["set"])
    p.add_argument("name", nargs="?", help="typesafe (default), deepseek, or any keychain_account from the config")
    p = sub.add_parser("learn")
    p.add_argument("app")
    p = sub.add_parser("skills")
    p.add_argument("--delete")
    p = sub.add_parser("eval")
    p.add_argument("--suite")
    p.add_argument("--only", help="comma-separated task ids")
    p.add_argument("--repeat", type=int, help="runs per task (default: the suite's repeat, else 1)")
    p.add_argument("--out")
    p = sub.add_parser("helper")
    p.add_argument("action", choices=["build", "install", "path"])
    p.add_argument("--sign", help="codesign identity (default: ad-hoc)")
    p = sub.add_parser("audit")
    p.add_argument("-n", type=int, default=20)
    sub.add_parser("privacy-check", help="measure redaction on a synthetic corpus (leaks, over-redaction)")
    p = sub.add_parser("selftest", help="run planners, learn and routine replay for real (harmless)")
    p.add_argument("--skip-learn", action="store_true")
    p.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.load()
    handlers = {"doctor": cmd_doctor, "observe": cmd_observe, "do": cmd_do, "web": cmd_web, "serve": cmd_serve,
                "key": cmd_key, "helper": cmd_helper, "surfaces": cmd_surfaces, "audit": cmd_audit, "learn": cmd_learn, "skills": cmd_skills, "eval": cmd_eval, "privacy-check": cmd_privacy_check, "selftest": cmd_selftest}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
