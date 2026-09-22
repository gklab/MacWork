"""Every setting the defaults ship has to be read by something.

A key that exists and does nothing is worse than no key: it reads as a promise. `release_only_for` sat in
`policy.yaml` from the day it was written, shaping only which options a classifier was offered, while the
decision it names asked a function that never saw the action — and `engine.revert.on_failure` described a
behaviour nothing implemented. Both were found by grepping for the keys, which is what this does on every
run instead.

It is a text search, so it cannot see a key reached by variable name. Those are listed below with the
reason — a short list somebody has to justify adding to, rather than a check quietly switched off.
"""

import pathlib
import re

import pytest
import yaml

DEFAULTS = pathlib.Path("macwork/defaults")

# Keys reached without their own name appearing in the source, each with why.
NOT_BY_NAME = {
    # a lookup table: the keys are macOS action names and the code indexes the dict with whatever the
    # element reports, so no individual name is ever written down (that is the point of it)
    "observe.window.action_labels.*": "an AX action -> label table, indexed by what the element answers",
    # the endpoint *names* are the user's own, so nothing here can name them; each endpoint's settings are
    # handed to the backend, which reads them by their own names in another file
    "planner.endpoints.*": "iterated by name; its fields are read by the backend it configures",
    # a verdict's prose, looked up by whatever `confirm.release` lists — the point is that which verdicts
    # release an action is policy, so the code must not name them
    "confirm.navigate": "read via the names in confirm.release",
    "confirm.enter": "read via the names in confirm.release",
    "confirm.edit": "read via the names in confirm.release",
    "confirm.back_out": "read via the names in confirm.release",
    "confirm.read": "read via the names in confirm.release",
    "confirm.release_only_for.*": "read via the names in confirm.release",
    # name -> regex, and regex -> replacement: both iterated whole, so no individual one is written down
    "redact.patterns.*": "iterated as name -> regex",
    "redact.replace.*": "iterated as regex -> replacement",
    # question texts named as a plain argument — `self._back_out(task, app, "cancel_quit")` — rather than
    # through cfg.get. Verified by hand; both are reached from tidy.py.
    "cancel_quit": "passed to _back_out by name (tidy.py)",
    "dismiss_window": "passed to _back_out by name (tidy.py)",
    # the floor's own vocabulary: `conf.get("categories")` is walked, so no category and neither of its two
    # fields is ever named in the code — which is the point, since the set of them is policy
    "confirm.categories.*": "iterated: name -> {what, patterns} (policy.py)",
    # read through `cfg.docs["questions"]` and walked; the reasons are options offered to the decider
    "unfinished.*": "read whole through cfg.docs and walked (judge.py)",
}


def leaves(node, prefix=""):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from leaves(v, f"{prefix}.{k}" if prefix else k)
    else:
        yield prefix


def excused(path: str) -> bool:
    return any(re.fullmatch(pat.replace(".", r"\.").replace("*", "[^.]*") + r"(\..*)?", path) for pat in NOT_BY_NAME)


FILES = {p: p.read_text(encoding="utf-8") for p in pathlib.Path("macwork").rglob("*.py")}
SOURCE = "\n".join(FILES.values())


def read_by(path: str) -> str | None:
    """How the code reads this setting, or None if nothing does.

    The first version searched the whole source for the key's *last segment*, so `decider.backoff_s` passed
    because `planner` also has a `backoff_s`, and deleting the line that read it would not have failed
    anything. Widening a text search until everything passes turns it back into that, so this is narrow on
    purpose and anything it cannot see is listed in NOT_BY_NAME with a reason.

    Two shapes, and both require the file to be reading the *configuration* — `st.get("decider", {})` in
    cli.py reads a status dict and used to vouch for every `decider.*` key in the file.
    """
    parts = path.split(".")
    if _cfg_path(SOURCE, path):
        return "read by its whole dotted path"
    # `cfg.section("observe.vision")` then `vc.get("min_conf")` — the two halves, in one file. Split
    # anywhere, because the code chooses where to stop: some sections are one level deep, some three.
    for i in range(len(parts) - 1, 0, -1):
        head, tail = ".".join(parts[:i]), ".".join(parts[i:])
        for f, src in FILES.items():
            if _cfg_path(src, head) and re.search(rf'["\']{re.escape(tail)}["\']', src):
                return f"{head} is opened and {tail} read from it, in {f.name}"
    # named as a plain argument to something that then reads it — `self._ask("planner_plan", …)`
    for f, src in FILES.items():
        if _opens_config(src) and re.search(rf'["\']{re.escape(path)}["\']', src):
            return f"named as a literal in {f.name}"
    return None


# `cfg.get(...)`, `cfg.section(...)`, `cfg.question(...)` — the configuration, named.
_CFG_CALL = r'\b\w*(?:cfg|config)\.(?:get|section|question)\('
# `cfg.policy`, `cfg.privacy`, `cfg.docs["questions"]` — a whole document, opened.
_CFG_DOC = r'\b\w*(?:cfg|config)\.(?:policy|privacy|docs)\b'


# `cfg.policy.get("confirm")`, `cfg.privacy.get("redact")`, `cfg.docs["questions"].get("moves")` — a
# document opened, then a section named inside it.
_CFG_DOC_GET = r'\b\w*(?:cfg|config)\.(?:policy|privacy|docs)(?:\[[^\]]+\])?\s*\.get\('


def _cfg_path(src: str, path: str) -> bool:
    quoted = rf'\s*["\']{re.escape(path)}["\']'
    return bool(re.search(_CFG_CALL + quoted, src) or re.search(_CFG_DOC_GET + quoted, src))


def _opens_config(src: str) -> bool:
    return bool(re.search(_CFG_CALL, src) or re.search(_CFG_DOC, src))


KEYS = [(f.name, path) for f in sorted(DEFAULTS.glob("*.yaml"))
        for path in leaves(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        if path and not excused(path)]


@pytest.mark.parametrize("name,path", KEYS, ids=[f"{n}:{p}" for n, p in KEYS])
def test_the_code_reads_this_setting(name, path):
    assert read_by(path), \
        f"{name} ships {path} and nothing reads it: a setting that does nothing reads as a promise"


def test_there_are_settings_to_check():
    assert len(KEYS) > 100


@pytest.mark.parametrize("key", [
    "decider.timeout_s", "engine.max_steps", "observe.vision.min_conf", "confirm.release_threshold",
    "redact.max_clauses", "watch.poll_s", "engine.verify.done_settle_ms", "observe.wheel.lines",
    "deny.host_app", "engine.revert.max_steps",
])
def test_the_check_notices_when_a_setting_stops_being_read(monkeypatch, key):
    """Proof that it has teeth, on one setting from each shape the config has.

    The version this replaced had none: it searched the whole source for the key's last segment, so
    `decider.timeout_s` passed because `planner` also has a `timeout_s`, and deleting the line that read
    it failed nothing. Every one of these ten was rescued by something unrelated then; none is now.
    """
    import tests.test_settings as mod
    leaf = key.split(".")[-1]
    without = {f: src.replace(f'"{key}"', '"GONE"').replace(f'"{leaf}"', '"GONE"') for f, src in FILES.items()}
    monkeypatch.setattr(mod, "FILES", without)
    monkeypatch.setattr(mod, "SOURCE", "\n".join(without.values()))
    assert mod.read_by(key) is None, f"the read of {key} was removed and the check did not notice"


def test_every_excuse_still_matches_something():
    """An excuse for a key that no longer exists is an excuse nobody will notice going stale."""
    every = [p for f in DEFAULTS.glob("*.yaml") for p in leaves(yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if p]
    for pat in NOT_BY_NAME:
        assert any(re.fullmatch(pat.replace(".", r"\.").replace("*", "[^.]*") + r"(\..*)?", p) for p in every), \
            f"nothing matches the excuse {pat!r} any more"
