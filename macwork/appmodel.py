"""What the engine knows about an app it may never have seen before — learned, not written down.

Static part (read once per app version, no UI touched):
  * scripting dictionary (sdef): commands the app itself says it can do without its UI
  * URL schemes and document types from Info.plist
Dynamic part (grows while operating, see ``record``):
  * screens: signature -> title and a sample of what was on it
  * edges:   (screen, action) -> resulting screen, how often it worked
  * dead:    (screen, action) pairs that had no effect, so they are not retried blindly

Stored per ``bundle_id@version`` as JSON under ``appmodel.dir`` — readable and editable by the user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import plistlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .config import Config, expand

log = logging.getLogger(__name__)


def _info_plist(app_path: str) -> dict[str, Any]:
    try:
        with open(Path(app_path) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def parse_sdef(xml_text: str) -> dict[str, Any]:
    """Commands and classes of a scripting definition (hidden items and xi:includes skipped)."""
    xml_text = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.info("sdef parse: %s", exc)
        return {"commands": [], "classes": []}
    commands, classes = [], []
    for suite in root.iter("suite"):
        sname = suite.get("name", "")
        for c in suite.findall("command"):
            if c.get("hidden") == "yes":
                continue
            d = c.find("direct-parameter")
            commands.append({
                "suite": sname, "name": c.get("name", ""), "desc": (c.get("description") or "").strip(),
                "direct": None if d is None else {"type": d.get("type") or _types(d), "optional": d.get("optional") == "yes",
                                                  "desc": (d.get("description") or "").strip()},
                "params": [{"name": p.get("name", ""), "type": p.get("type") or _types(p), "optional": p.get("optional") == "yes",
                            "desc": (p.get("description") or "").strip()}
                           for p in c.findall("parameter") if p.get("hidden") != "yes"],
                "result": (c.find("result").get("type") if c.find("result") is not None else None),
            })
        for k in suite.findall("class"):
            if k.get("hidden") != "yes":
                classes.append({"suite": sname, "name": k.get("name", ""), "desc": (k.get("description") or "").strip()})
    return {"commands": commands, "classes": classes}


def _types(el: ET.Element) -> str:
    return "|".join(t.get("type", "") for t in el.findall("type")) or "any"


def static_model(app: dict[str, Any]) -> dict[str, Any]:
    path = app.get("path") or ""
    info = _info_plist(path)
    model: dict[str, Any] = {
        "bundle_id": app.get("bundle_id") or info.get("CFBundleIdentifier"),
        "name": app.get("name") or info.get("CFBundleDisplayName") or info.get("CFBundleName"),
        "version": str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "0"),
        "path": path,
        "url_schemes": sorted({s for t in info.get("CFBundleURLTypes") or [] for s in t.get("CFBundleURLSchemes") or []}),
        "document_types": sorted({e for t in info.get("CFBundleDocumentTypes") or []
                                  for e in (t.get("CFBundleTypeExtensions") or []) + (t.get("LSItemContentTypes") or [])})[:40],
        "scriptable": bool(info.get("NSAppleScriptEnabled") or info.get("OSAScriptingDefinition")),
        "sdef": {"commands": [], "classes": []},
    }
    sdef_name = info.get("OSAScriptingDefinition")
    if sdef_name:
        f = Path(path) / "Contents" / "Resources" / str(sdef_name)
        try:
            model["sdef"] = parse_sdef(f.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            log.info("sdef %s: %s", f, exc)
    return model


def signature(app: dict[str, Any] | None, window: str | None, labels: list[str], take: int = 30) -> str:
    """A screen's identity: app + window title + the set of what can be done there (numbers ignored, so a
    calculator's display or a counter does not make every screen new)."""
    norm = sorted({re.sub(r"\d+", "#", lab) for lab in labels})[:take]
    raw = json.dumps([(app or {}).get("bundle_id"), re.sub(r"\d+", "#", window or ""), norm], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class AppModels:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dir = expand(cfg.get("appmodel.dir")) or Path("~/Library/Application Support/macwork/apps").expanduser()
        if not self.dir.exists():                      # data written before the project was named macwork
            older = Path(str(self.dir).replace("/macwork/", "/jev-mac-use/"))
            if older.exists():
                self.dir = older
        self._mem: dict[str, dict[str, Any]] = {}
        self._paths: dict[str, str] = {}      # bundle id -> app path, from any app info that carried one
        self._lock = threading.Lock()

    def _key(self, app: dict[str, Any]) -> str | None:
        bid = app.get("bundle_id")
        if not bid:
            return None
        if app.get("path"):
            self._paths[bid] = app["path"]
        path = app.get("path") or self._paths.get(bid)
        if path and not app.get("path"):
            app = {**app, "path": path}
        version = str(_info_plist(path).get("CFBundleShortVersionString") or "0") if path else "0"
        return f"{bid}@{version}"

    def get(self, app: dict[str, Any] | None) -> dict[str, Any] | None:
        if not app:
            return None
        key = self._key(app)
        if not key:
            return None
        with self._lock:
            if key in self._mem:
                return self._mem[key]
            f = self.dir / f"{key}.json"
            model: dict[str, Any] | None = None
            if f.exists():
                try:
                    model = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    model = None
            if model is None:
                model = static_model({**app, "path": app.get("path") or self._paths.get(app.get("bundle_id", ""), "")})
                model.update({"screens": {}, "edges": [], "dead": [], "created": time.time()})
            self._mem[key] = model
            return model

    def save(self, app: dict[str, Any] | None) -> None:
        if not app or not self.cfg.get("appmodel.persist", True):
            return
        key = self._key(app)
        if not key or key not in self._mem:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{key}.json").write_text(json.dumps(self._mem[key], ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ learning
    def see(self, app: dict[str, Any] | None, sig: str, window: str | None, labels: list[str]) -> None:
        m = self.get(app)
        if m is None:
            return
        s = m["screens"].setdefault(sig, {"window": window, "sample": labels[: int(self.cfg.get("appmodel.sample", 12))], "seen": 0})
        s["seen"] += 1

    def record(self, app: dict[str, Any] | None, before: str, action: str, after: str | None, ok: bool, changed: bool) -> None:
        """One executed step: where it was taken, what it was, where it led."""
        m = self.get(app)
        if m is None:
            return
        if not changed or not ok:
            if [before, action] not in m["dead"]:
                m["dead"].append([before, action])
            return
        for e in m["edges"]:
            if e["from"] == before and e["action"] == action:
                e["to"], e["n"] = after, e.get("n", 0) + 1
                break
        else:
            m["edges"].append({"from": before, "action": action, "to": after, "n": 1})
        if [before, action] in m["dead"]:
            m["dead"].remove([before, action])
        cap = int(self.cfg.get("appmodel.max_edges", 2000))
        del m["edges"][:-cap]

    def hints(self, app: dict[str, Any] | None, sig: str, labels: set[str]) -> dict[str, Any]:
        """What was learned about this screen: where known actions lead, and which ones did nothing."""
        m = self.get(app)
        if m is None:
            return {}
        leads = []
        for e in m["edges"]:
            if e["from"] == sig and e["action"] in labels:
                to = m["screens"].get(e.get("to") or "", {})
                leads.append(f"「{e['action']}」 -> {to.get('window') or 'another screen'} (worked {e.get('n', 1)}x)")
        dead = [a for s, a in m["dead"] if s == sig and a in labels]
        out: dict[str, Any] = {}
        if leads:
            out["known_effects"] = leads[: int(self.cfg.get("appmodel.max_hints", 8))]
        if dead:
            out["no_effect_before"] = dead[: int(self.cfg.get("appmodel.max_hints", 8))]
        return out

    def brief(self, app: dict[str, Any] | None, limit: int = 40) -> dict[str, Any]:
        """A compact description for a planner."""
        m = self.get(app)
        if m is None:
            return {}
        cmds = [c["name"] for c in m["sdef"]["commands"] if c["suite"] not in (self.cfg.get("observe.sdef.skip_suites") or [])]
        return {"app": m.get("name"), "scriptable_commands": cmds[:limit], "url_schemes": m.get("url_schemes", [])[:10],
                "opens": m.get("document_types", [])[:15], "screens_known": len(m["screens"])}
