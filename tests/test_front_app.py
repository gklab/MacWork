""""Never drive the app the caller runs in" is about driving.

Found on the first real task run from a terminal: the goal named a file, the Mac can open a file by its path
without touching any window, and every such option — open, show on disk, read, move to the Trash — was
denied, because the terminal was in front when the engine looked and the rule asked "which app is in
front?" of actions that do not act on the app in front. Only "open with <another app>" survived, since
those name an app of their own. The task went looking for the app by hand and ended up trying to type a
shell command.
"""

from macwork.engine import Engine
from macwork.model import Affordance, Observation
from macwork.observe import PROVIDERS, Ctx
from tests.english_mac import APP, EnglishMac
from tests.test_engine import ScriptedDecider, cfg


def engine(tmp_path):
    eng = Engine(cfg(tmp_path), helper=EnglishMac(), decider=ScriptedDecider([]))
    eng.cache["host.bundles"] = {APP["bundle_id"]}          # the caller runs in the app that is in front
    return eng


def test_what_works_through_the_system_is_not_driving_the_app_in_front(tmp_path):
    eng = engine(tmp_path)
    for a in (Affordance("p0", "file", "open", "open the file /tmp/x.csv", {"path": "/tmp/x.csv"}),
              Affordance("T0", "file", "read", "read the text of /tmp/x.csv", {"path": "/tmp/x.csv"}),
              Affordance("s0", "shortcut", "run", "run the shortcut 「Tidy」", {"name": "Tidy"})):
        assert not eng._denied(a, APP), a.label


def test_driving_the_callers_own_app_is_still_refused(tmp_path):
    eng = engine(tmp_path)
    for a in (Affordance("w0", "window", "press", "button 「Send」", {}),
              Affordance("k0", "keys", "type", "type at the cursor", {}),
              Affordance("h0", "hold", "hold", "move forward — hold w for 1 s", {"keys": ["w"], "ms": 1000}),
              Affordance("a0", "app", "activate", "switch to app TextEdit", {"bundle_id": APP["bundle_id"]}),
              Affordance("p1", "file", "open", "open x.csv with TextEdit", {"path": "/tmp/x.csv", "bundle_id": APP["bundle_id"]})):
        assert eng._denied(a, APP), a.label


def test_a_channel_nobody_vouched_for_counts_as_driving(tmp_path):
    assert engine(tmp_path)._denied(Affordance("x0", "some_plugin", "do", "do the thing", {}), APP)


def test_the_plain_open_says_which_app_that_is(tmp_path):
    (tmp_path / "q3.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    helper = EnglishMac()
    helper.openers = [{"name": "Sheets Pro", "bundle_id": "com.example.sheets", "path": "/Applications/Sheets Pro.app", "default": True},
                      {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "path": "/System/Applications/TextEdit.app"}]
    obs = Observation(app=None, window=None, affordances=[])
    PROVIDERS["files"](Ctx(cfg(tmp_path), helper, goal=f"open {tmp_path}/q3.csv in Sheets Pro", running=[]), obs)
    opens = [a.label for a in obs.affordances if a.verb == "open"]
    assert opens == [f"open the file {tmp_path}/q3.csv in Sheets Pro, the app this Mac opens it with", "open q3.csv with TextEdit"]
