"""What is sent to a planner that is not on this Mac must be redacted — including after a fall-through.

`Planning.on_device` was worked out once, in `__init__`, from the planner in front of the chain. A `Chain`
drops a planner whose credentials are rejected and the next one answers — by design, so a stale key
degrades planning instead of switching it off. But `on_device` still said what it said at construction, so
a prompt built for a local model went to a cloud model in the clear.

It cannot be decided before the call at all: the fall-through happens *during* it. So it is decided by the
whole chain — on-device only if nothing in it can reach off this Mac.

The audit had the matching fault: it recorded the planner that was about to be tried, and the line was
written before the call, so after a fall-through the log named the wrong one.
"""

from typing import Any

import pytest

from macwork.config import Config
from macwork.planner import Chain, Planning, PlannerError


class Fake:
    def __init__(self, name, local=False, refuse=False):
        self.name, self.local, self.refuse = name, local, refuse
        self.saw: list[str] = []

    def complete(self, system, prompt, schema):
        self.saw.append(prompt)
        if self.refuse:
            raise PlannerError(f"{self.name}: credentials rejected", refused=True)
        return {"steps": ["do the thing"], "inputs": {}, "try": []}


class Redactor:
    def value(self, v):
        def go(x):
            if isinstance(x, str):
                return x.replace("wang.zhaokai@example.com", "⟦EMAIL_1⟧")
            if isinstance(x, dict):
                return {k: go(y) for k, y in x.items()}
            if isinstance(x, list):
                return [go(y) for y in x]
            return x
        return go(v)

    def restore(self, v):
        return v


class Audit:
    def __init__(self):
        self.lines: list[dict[str, Any]] = []

    def record(self, kind, **kw):
        self.lines.append({"kind": kind, **kw})


SECRET = "mail wang.zhaokai@example.com"


def planning(*planners, audit=None):
    chain = planners[0] if len(planners) == 1 else Chain(list(planners))
    return Planning(Config.load(), chain, redactor=Redactor(), audit=audit or Audit()), chain


# --------------------------------------------------------------------------- the fall-through

def test_a_cloud_planner_reached_through_a_chain_does_not_see_it_in_the_clear():
    on_device, cloud = Fake("foundation", local=True), Fake("deepseek", refuse=False)
    on_device.refuse = True
    p, _ = planning(on_device, cloud)
    p.plan(SECRET, {})
    assert cloud.saw and "wang.zhaokai@example.com" not in cloud.saw[0], \
        "the address went to a cloud planner unredacted after the chain fell through"


def test_a_chain_of_on_device_planners_is_still_trusted():
    """The point of `trust_on_device`: a local model does not need its text pseudonymised, and doing it
    anyway makes the plans worse."""
    a, b = Fake("foundation", local=True), Fake("lmstudio", local=True)
    p, _ = planning(a, b)
    p.plan(SECRET, {})
    assert "wang.zhaokai@example.com" in a.saw[0]


def test_one_cloud_member_is_enough_to_redact_the_whole_chain():
    """It cannot be decided when the call starts — the fall-through happens during it."""
    a, b = Fake("foundation", local=True), Fake("deepseek")
    p, _ = planning(a, b)
    p.plan(SECRET, {})
    assert "wang.zhaokai@example.com" not in a.saw[0]


def test_a_lone_cloud_planner_is_redacted_as_before():
    cloud = Fake("deepseek")
    p, _ = planning(cloud)
    p.plan(SECRET, {})
    assert "wang.zhaokai@example.com" not in cloud.saw[0]


def test_a_lone_on_device_planner_is_trusted_as_before():
    local = Fake("foundation", local=True)
    p, _ = planning(local)
    p.plan(SECRET, {})
    assert "wang.zhaokai@example.com" in local.saw[0]


# --------------------------------------------------------------------------- the audit

def test_the_log_names_the_planner_that_answered():
    a, b = Fake("foundation", local=True), Fake("deepseek")
    a.refuse = True
    audit = Audit()
    p, _ = planning(a, b, audit=audit)
    p.plan(SECRET, {})
    plans = [l for l in audit.lines if l["kind"] == "plan"]
    assert plans and plans[-1].get("answered_by") == "deepseek", plans


def test_the_log_says_every_planner_that_saw_the_prompt():
    """A refused planner read it before refusing; the record has to say so."""
    a, b = Fake("foundation", local=True), Fake("deepseek")
    a.refuse = True
    audit = Audit()
    p, _ = planning(a, b, audit=audit)
    p.plan(SECRET, {})
    tried = [l for l in audit.lines if l["kind"] == "plan"][-1].get("tried")
    assert tried == ["foundation", "deepseek"], tried


def test_the_prompt_in_the_log_is_the_redacted_one():
    a, b = Fake("foundation", local=True), Fake("deepseek")
    audit = Audit()
    p, _ = planning(a, b, audit=audit)
    p.plan(SECRET, {})
    assert "wang.zhaokai@example.com" not in str(audit.lines)
