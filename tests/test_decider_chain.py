"""A switch of decider holds for the task it happened in, and a lost request is asked once more first.

During an evaluation one ten-second network blip moved the whole rest of the session onto the uncalibrated
fallback, and the run measured that instead of the engine.
"""

from macwork.decider import ChainDecider, DeciderError


class Flaky:
    def __init__(self, name, fail_times):
        self.name, self.fail_times, self.asked = name, fail_times, 0
        self.calls, self.cost_usd, self.last_ms = 0, 0.0, 1.0

    def decide(self, state, questions):
        self.asked += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise DeciderError(f"{self.name} timed out")
        return {k: {"type": "noul", "noul": 1.0, "by": self.name} for k in questions}


def test_a_lost_request_is_asked_once_more_before_anyone_is_demoted():
    a, b = Flaky("a", 1), Flaky("b", 0)
    chain = ChainDecider([a, b])
    assert chain.decide({}, {"q": {}})["q"]["by"] == "a" and a.asked == 2 and b.asked == 0


def test_a_switch_holds_for_the_task_and_the_next_task_starts_with_the_one_asked_for():
    a, b = Flaky("a", 2), Flaky("b", 0)
    chain = ChainDecider([a, b])
    assert chain.decide({}, {"q": {}})["q"]["by"] == "b"
    assert chain.decide({}, {"q": {}})["q"]["by"] == "b" and a.asked == 2      # not asked again this task
    chain.begin_task()
    assert chain.decide({}, {"q": {}})["q"]["by"] == "a"


def test_the_last_decider_standing_raises():
    a = Flaky("a", 5)
    chain = ChainDecider([a])
    try:
        chain.decide({}, {"q": {}})
    except DeciderError:
        pass
    else:
        raise AssertionError("no decider left and no error")


class Counted(Flaky):
    """A decider that has already answered `calls` times this session, at `cost` each."""

    def __init__(self, name, fail_times, calls, cost=0.0002):
        super().__init__(name, fail_times)
        self.calls, self.cost_usd, self.unit = calls, calls * cost, cost

    def decide(self, state, questions):
        out = super().decide(state, questions)
        self.calls += 1
        self.cost_usd += self.unit
        self.last_ms = 7.0 if self.name == "b" else 3.0
        return out


def test_a_switch_never_makes_the_counts_go_backwards():
    """They were read off whoever was in front: 400 calls became 1 at a switch."""
    a, b = Counted("a", 0, 400), Counted("b", 0, 0)
    chain = ChainDecider([a, b])
    before = (chain.calls, chain.cost_usd)
    a.fail_times = 99                                     # the network goes: the next decision is b's
    chain.decide({}, {"q": {}})
    assert chain.calls == 401 and chain.calls >= before[0] and chain.cost_usd >= before[1]
    assert chain.calls == a.calls + b.calls and chain.last_ms == 7.0, "the time is the answering decider's"
    assert chain.name == "b", "who is answering is still whoever is in front"


def test_the_decision_ceiling_still_holds_after_a_switch(tmp_path):
    from macwork.engine import Engine
    from tests.test_engine import FakeHelper, cfg
    a, b = Counted("a", 99, 400), Counted("b", 0, 0)
    eng = Engine(cfg(tmp_path, config={"engine": {"max_decisions": 3}}), helper=FakeHelper(), decider=ChainDecider([a, b]))
    task = eng._new_task("anything", {}, None)
    task.begin_run(eng._decider.calls, eng._decider.cost_usd)
    for _ in range(3):
        eng._decider.decide({}, {"q": {}})
    spent = eng._overspent(task, eng.cfg.section("engine"))
    assert spent is not None and spent.whole_task, "three decisions after a switch did not count against the ceiling"
