"""Which apps this Mac has.

Scanning four fixed folders is both too narrow and too wide. Measured on a real Mac: 146 apps found that
way against 405 the system knows about — `/System/Library/CoreServices/Finder.app` and Safari (which lives
in a cryptex) among the missing — while a naive "every bundle on disk" would drag in build products,
simulator copies and helper apps buried inside other apps.

Neither judgement needs a list of paths. Spotlight says where the bundles are; LaunchServices says which
copy it would open for a bundle identifier, and that copy is the one reported.
"""

from macwork.engine import Engine
from macwork.observe import installed_apps
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


class Registry(FakeHelper):
    """Answers apps.installed differently depending on how it is asked."""

    def call(self, method, timeout=30.0, **p):
        if method == "apps.installed":
            self.calls.append((method, p))
            if p.get("source") == "dirs":
                return [{"name": "计算器", "file": "Calculator", "path": "/System/Applications/Calculator.app",
                         "bundle_id": "com.apple.calculator"}]
            return [
                {"name": "计算器", "file": "Calculator", "path": "/System/Applications/Calculator.app",
                 "bundle_id": "com.apple.calculator"},
                {"name": "Finder", "file": "Finder", "path": "/System/Library/CoreServices/Finder.app",
                 "bundle_id": "com.apple.finder"},
                {"name": "ClaudeBar", "file": "ClaudeBar", "path": "/Applications/ClaudeBar.app",
                 "bundle_id": "dev.claudebar", "background": True},
            ]
        return super().call(method, timeout, **p)


def test_the_system_is_asked_which_apps_it_would_open(tmp_path):
    engine = Engine(cfg(tmp_path), helper=Registry(), decider=ScriptedDecider([]))
    found = installed_apps(engine.cfg, engine.helper)

    assert {a["bundle_id"] for a in found} >= {"com.apple.finder", "com.apple.calculator"}
    asked = engine.helper.did("apps.installed")[0]
    assert asked["source"] == "launchservices"


def test_scanning_folders_is_still_available_as_a_fallback(tmp_path):
    engine = Engine(cfg(tmp_path, config={"observe": {"apps": {"source": "dirs"}}}),
                    helper=Registry(), decider=ScriptedDecider([]))
    found = installed_apps(engine.cfg, engine.helper)

    assert [a["bundle_id"] for a in found] == ["com.apple.calculator"]
    assert "com.apple.finder" not in {a["bundle_id"] for a in found}, "this is what the old behaviour missed"


def test_an_app_that_runs_without_a_window_says_so(tmp_path):
    """Not hidden — the engine reaches such apps through their menu bar items — but "open it" shows nothing."""
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["apps"]}}),
                    helper=Registry(), decider=ScriptedDecider([]))
    labels = [a["label"] for a in engine.observe()["affordances"]]

    claudebar = next(label for label in labels if "ClaudeBar" in label)
    assert "background app" in claudebar
    assert not any("background app" in label for label in labels if "计算器" in label)
