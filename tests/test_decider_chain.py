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
