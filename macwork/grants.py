"""Standing grants: a decision the person made once, remembered.

The safety floor stops an action when nobody can vouch for it — a classifier at 0.69 against a bar of 0.7, a
word that means "save" in one app and something harmless in another. The person says yes, the task goes on,
and the next day the same action in the same app stops again. The only way to make it stop asking used to be
to change a threshold, which is a code change standing in for a decision that was never the code's to make:
no number is right for every app anyone will ever install.

So the decision is kept where it was made. This is what macOS does with its own privacy prompts: it asks
once, remembers the answer per app, and lets it be taken back in one place.

The direction is fixed. The engine becomes more careful on its own — it learns what did nothing, what it
took back — and *only a person* makes it more permissive, explicitly, for one action in one app. Nothing
here is inferred from how often someone said yes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .model import Affordance

log = logging.getLogger("macwork.grants")

RISKY_SCREEN = "risky_screen"      # the gate that comes from the screen, not from the action


def identity(app: dict[str, Any] | None, a: Affordance) -> str:
    """Which action, in which app — and nothing about the screen it was on or the task it was for.

    Where the Mac gives the action a language-independent identity (`Affordance.key`, the selector behind a
    menu command) that is used alone, so a grant survives the Mac's language being changed. Elsewhere the
    label and where it sits are all there is. The app is its bundle id without a version: like the system's
    own grants, this one survives an update.

    An action that carries text is judged with its text in it, and is granted the same way — "type this
    command" is not "type anything".
    """
    what = a.key or f"{a.name()}\x1f{a.context}"
    return "\x1f".join([str((app or {}).get("bundle_id") or ""), a.channel, a.verb, what])


class Grants:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path = Path(str(cfg.get("grants.path", "~/Library/Application Support/macwork/grants.json"))).expanduser()
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._stamp: float | None = None

    # ------------------------------------------------------------------ what may be granted
    def offer(self, app: dict[str, Any] | None, a: Affordance, because: list[str]) -> dict[str, Any] | None:
        """What a standing grant for this action would be, or None when it may not have one.

        Some kinds of action ask every time whatever anyone said before (`grants.never_for`): a grant for
        "delete" would be a grant to delete anything that button ever points at.
        """
        if not self.cfg.get("grants.enabled", True):
            return None
        kinds = [RISKY_SCREEN if " " in c else c for c in because] or [RISKY_SCREEN]
        if set(kinds) & set(self.cfg.get("grants.never_for") or []):
            return None
        ident = identity(app, a)
        return {"id": hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12], "identity": ident,
                "app": (app or {}).get("name") or "", "bundle_id": (app or {}).get("bundle_id") or "",
                "action": a.label[:200], "because": kinds}

    # ------------------------------------------------------------------ the record
    def _load(self) -> None:
        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            self._rows, self._stamp = {}, None
            return
        if stamp == self._stamp:
            return
        try:     # read again when it changed: `macwork grants revoke` runs in another process
            rows = json.loads(self.path.read_text(encoding="utf-8")).get("grants") or []
            self._rows = {r["identity"]: r for r in rows if isinstance(r, dict) and r.get("identity")}
        except (OSError, ValueError, AttributeError):
            # unreadable is not "everything is allowed" and not "start over": nothing is granted, and the
            # file is left alone for the person to look at
            log.warning("the grants file %s could not be read; nothing is granted", self.path)
            self._rows = {}
        self._stamp = stamp

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".grants-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:       # mkstemp creates it 0600
            json.dump({"grants": list(self._rows.values())}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._stamp = self.path.stat().st_mtime

    def covers(self, offer: dict[str, Any] | None) -> dict[str, Any] | None:
        """The grant that covers this, if the person gave one — and it still may be given."""
        if not offer:
            return None       # not grantable now: a grant from before `never_for` changed no longer counts
        with self._lock:
            self._load()
            return self._rows.get(offer["identity"])

    def add(self, offer: dict[str, Any], source: str) -> dict[str, Any]:
        with self._lock:
            self._load()
            row = self._rows.get(offer["identity"]) or {**offer, "granted_at": time.time(), "source": source, "used": 0}
            self._rows[offer["identity"]] = row
            self._save()
            return row

    def used(self, offer: dict[str, Any]) -> None:
        with self._lock:
            self._load()
            row = self._rows.get(offer["identity"])
            if row:
                row["used"], row["last_used"] = int(row.get("used", 0)) + 1, time.time()
                self._save()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            return sorted(self._rows.values(), key=lambda r: (r.get("bundle_id", ""), r.get("action", "")))

    def revoke(self, grant_id: str | None = None) -> int:
        """Take back one grant (by id or unambiguous prefix), or all of them."""
        with self._lock:
            self._load()
            gone = [k for k, r in self._rows.items() if grant_id is None or str(r.get("id", "")).startswith(grant_id)]
            if grant_id is not None and len(gone) > 1:
                raise ValueError(f"{grant_id!r} matches {len(gone)} grants")
            for k in gone:
                del self._rows[k]
            if gone:
                self._save()
            return len(gone)
