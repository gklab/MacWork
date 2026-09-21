"""What a provider offers, something has to be able to carry out.

`observe` hands the caller a list of actions. Two of the channels in it are not executors — `vision` means
"read this window from the screen and look again" and `skill` means "replay a whole learned routine" — and
the loop reads both by name before `_execute` is ever reached. The step level has no loop, so it passed
them straight through and answered `no channel 'vision'`: an option that was offered and could not be
taken.

The same sweep found the `web` channel still being tested for in four places after the subsystem was torn
out, including a default that exempted it from the risky-screen gate and a test asserting that exemption.
"""

import ast
import pathlib
import re

import pytest

import macwork.act as act
import macwork.observe as observe
from macwork.config import Config

CFG = Config.load()
# every channel name a provider puts into an Affordance
OFFERED = sorted(set(re.findall(r'Affordance\(\s*[^,]+,\s*["\']([a-z_]+)["\']',
                                pathlib.Path("macwork/observe.py").read_text(encoding="utf-8"))))


def handled_by_name(where: str) -> set[str]:
    """Channels a module singles out with `a.channel == "x"` or `in ("x", ...)` — the non-executors."""
    tree = ast.parse(pathlib.Path(where).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and "channel" in ast.unparse(node.left):
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    out.add(c.value)
                elif isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                    out |= {e.value for e in c.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def test_there_are_channels_to_check():
    assert len(OFFERED) > 8


@pytest.mark.parametrize("channel", OFFERED)
def test_the_goal_level_can_carry_out_everything_it_is_offered(channel):
    assert act.get_channel(channel) or channel in handled_by_name("macwork/loop.py"), \
        f"a provider offers {channel!r} and neither a channel nor the loop can take it"


@pytest.mark.parametrize("channel", OFFERED)
def test_the_step_level_can_too(channel):
    """`mac_observe` then `mac_act` is a documented way to drive this engine, not a lesser one."""
    assert act.get_channel(channel) or channel in handled_by_name("macwork/engine.py"), \
        f"a provider offers {channel!r} and `act` would answer \"no channel\""


@pytest.mark.parametrize("name", CFG.get("observe.providers") or [])
def test_every_provider_the_defaults_name_exists(name):
    """An unknown name only logs a warning, so a typo silently costs a whole capability."""
    assert observe.get_provider(name) is not None


def test_every_registered_provider_is_switched_on():
    assert not set(observe.PROVIDERS) - set(CFG.get("observe.providers") or []), \
        "a provider exists and nothing lists it: it will never run"


def test_the_web_channel_is_gone_from_every_last_corner():
    """It was torn out, and four places went on testing for it — one of them a default that exempted it
    from the risky-screen gate, and one a test asserting that exemption held."""
    for path in pathlib.Path("macwork").rglob("*.py"):
        assert '"web"' not in path.read_text(encoding="utf-8"), f"{path} still singles out the web channel"
