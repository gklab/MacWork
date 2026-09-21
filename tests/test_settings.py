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
    # passed through to the backend verbatim; naming any of it here would be knowledge about one server
    "planner.endpoints.*.extra.*": "handed to the endpoint as it stands",
    # a verdict's prose, looked up by whatever `confirm.release` lists — the point is that which verdicts
    # release an action is policy, so the code must not name them
    "confirm.navigate": "read via the names in confirm.release",
    "confirm.enter": "read via the names in confirm.release",
    "confirm.edit": "read via the names in confirm.release",
    "confirm.release_only_for.*": "read via the names in confirm.release",
    # name -> regex, and regex -> replacement: both iterated whole, so no individual one is written down
    "redact.patterns.*": "iterated as name -> regex",
    "redact.replace.*": "iterated as regex -> replacement",
}


def leaves(node, prefix=""):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from leaves(v, f"{prefix}.{k}" if prefix else k)
    else:
        yield prefix


def excused(path: str) -> bool:
    return any(re.fullmatch(pat.replace(".", r"\.").replace("*", "[^.]*") + r"(\..*)?", path) for pat in NOT_BY_NAME)


SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in pathlib.Path("macwork").rglob("*.py"))
KEYS = [(f.name, path) for f in sorted(DEFAULTS.glob("*.yaml"))
        for path in leaves(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        if path and not excused(path)]


@pytest.mark.parametrize("name,path", KEYS, ids=[f"{n}:{p}" for n, p in KEYS])
def test_the_code_reads_this_setting(name, path):
    last = path.split(".")[-1]
    assert re.search(rf'["\']{re.escape(last)}["\']', SOURCE) or f".{last}" in SOURCE, \
        f"{name} ships {path} and nothing reads it: a setting that does nothing reads as a promise"


def test_there_are_settings_to_check():
    assert len(KEYS) > 100


def test_every_excuse_still_matches_something():
    """An excuse for a key that no longer exists is an excuse nobody will notice going stale."""
    every = [p for f in DEFAULTS.glob("*.yaml") for p in leaves(yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if p]
    for pat in NOT_BY_NAME:
        assert any(re.fullmatch(pat.replace(".", r"\.").replace("*", "[^.]*") + r"(\..*)?", p) for p in every), \
            f"nothing matches the excuse {pat!r} any more"
