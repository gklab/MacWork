"""What an action *is*, as opposed to what it is called.

A screen's signature and a learned routine's steps were both keyed on label text — and labels are the app's
own words. So switching the Mac's interface language threw away every app model and every routine, silently:
nothing errored, the engine simply never recognised a screen again. That gets worse the more the labels come
from the system's own localized descriptions, which is exactly where the de-hardcoding work is heading.

Menu items almost always carry an Accessibility identifier. Measured on a real Mac:

* 93-97% of menu items have one (Finder 236/244, Chrome 243/261, Sublime 644/723, WeChat 162/170);
* it is either the selector behind the command (`_systemSettingsRequested:`) or, for menus loaded from a
  nib, AppKit's own `_NS:42` — and **both survive an app restart**: Dictionary quit and reopened gave
  36/36 identical placeholders and 105/105 identical selectors, because the numbering comes from the
  compiled nib and is fixed for a given build of the app;
* window controls mostly have none (Cursor 0/434, Terminal 1/9). There the label is the only identity the
  Mac offers, and these tests say so rather than pretend otherwise.

An app *update* can renumber the `_NS:` ones. App models are already filed per `bundle_id@version`, so they
are safe; a routine is not versioned, but `find` falls back to the label and a miss only stops the replay and
hands back to the ordinary loop.
"""

from macwork.appmodel import signature
from macwork.engine import Engine
from macwork.model import Affordance, Observation
from macwork.skills import Skills
from tests.test_engine import FakeHelper, ScriptedDecider, cfg


def _menubar(about: str, settings: str, new: str) -> dict:
    """The same menus, in whatever language — the identifiers do not move."""
    return {"nodes": [
        {"ref": "g1.0", "role": "AXMenuBar", "depth": 0},
        {"ref": "g1.1", "role": "AXMenuBarItem", "title": "Apple", "parent": "g1.0"},
        {"ref": "g1.2", "role": "AXMenuBarItem", "title": "File", "parent": "g1.0"},
        {"ref": "g1.3", "role": "AXMenu", "parent": "g1.2"},
        {"ref": "g1.4", "role": "AXMenuItem", "title": about, "ident": "_aboutRequested:", "parent": "g1.3"},
        {"ref": "g1.5", "role": "AXMenuItem", "title": settings, "ident": "_settingsRequested:", "parent": "g1.3"},
        {"ref": "g1.6", "role": "AXMenuItem", "title": new, "ident": "_newDocument:", "parent": "g1.3"},
    ], "ms": 1}


class InLanguage(FakeHelper):
    def __init__(self, menubar, **kw):
        super().__init__(**kw)
        self.menubar = menubar

    def call(self, method, timeout=30.0, **p):
        if method == "ax.snapshot" and p.get("scope") == "menubar":
            self.calls.append((method, p))
            return self.menubar
        return super().call(method, timeout, **p)


ENGLISH = _menubar("About This App", "Settings…", "New Document")
GERMAN = _menubar("Über diese App", "Einstellungen…", "Neues Dokument")


def _observe(tmp_path, menubar):
    engine = Engine(cfg(tmp_path, config={"observe": {"providers": ["menu"]}}),
                    helper=InLanguage(menubar), decider=ScriptedDecider([]))
    engine.observe()
    return engine, engine._last[1]


def test_a_menu_action_keeps_its_identity_across_languages(tmp_path):
    _, english = _observe(tmp_path, ENGLISH)
    _, german = _observe(tmp_path, GERMAN)

    assert [a.label for a in english.affordances] != [a.label for a in german.affordances]
    assert [a.identity() for a in english.affordances] == [a.identity() for a in german.affordances]


def test_a_screen_keeps_its_signature_across_languages(tmp_path):
    """Everything the app model learned about a screen is filed under this."""
    engine_en, english = _observe(tmp_path, ENGLISH)
    engine_de, german = _observe(tmp_path, GERMAN)
    app = {"pid": 42, "bundle_id": "com.apple.TextEdit"}

    # the window title is the app's own words and cannot be made language-independent; leave it out to see
    # what the rest of the signature is worth
    def sig(engine, obs):
        parts = [a.identity() for a in obs.affordances if a.channel in ("window", "menu", "pointer")]
        return signature(app, obs.window, parts, by_window=False)

    assert sig(engine_en, english) == sig(engine_de, german)

    by_label = [signature(app, None, [a.label for a in o.affordances], by_window=False) for o in (english, german)]
    assert by_label[0] != by_label[1], "keyed on labels it would have been a different screen"


def test_a_routine_recorded_in_one_language_replays_in_another(tmp_path):
    _, english = _observe(tmp_path, ENGLISH)
    _, german = _observe(tmp_path, GERMAN)
    settings_en = next(a for a in english.affordances if "Settings" in a.label)

    step = {"channel": "menu", "verb": "press", "label": "menu File ▸ Settings…",
            "key": settings_en.identity(), "context": "File", "inputs": []}
    found = Skills.find(step, german)
    assert found is not None and "Einstellungen" in found.label


def test_a_routine_recorded_before_identities_still_replays(tmp_path):
    """Skills already on disk have no key; they must keep working by label."""
    _, english = _observe(tmp_path, ENGLISH)
    old = {"channel": "menu", "verb": "press", "label": "menu File ▸ Settings…", "context": "File", "inputs": []}

    found = Skills.find(old, english)
    assert found is not None and "Settings" in found.label
    assert Skills.find({**old, "label": "menu File ▸ Einstellungen…"}, english) is None


def test_a_control_the_app_gives_no_identity_for_falls_back_to_its_label(tmp_path):
    """Most window controls carry no identifier. That is a limit of what the Mac exposes, not a choice."""
    plain = Affordance("w0", "window", "press", "button 「Send」", {"ref": "r0", "pid": 1})
    assert plain.key == "" and plain.identity() == "button 「Send」"

    obs = Observation(app=None, window=None, affordances=[plain])
    assert Skills.find({"channel": "window", "verb": "press", "label": "button 「Send」", "inputs": []}, obs) is plain
