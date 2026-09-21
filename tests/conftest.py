"""Tests never touch the real Mac: every subprocess the engine could start (open -a, shortcuts, mdfind…) is
replaced by a no-op unless a test installs its own fake."""

import subprocess

import pytest


class _Done:
    returncode, stdout, stderr = 0, "", ""


class _NoPopen(subprocess.Popen):
    def __init__(self, args, *a, **k):   # a class, so type hints like Popen[bytes] still work
        raise AssertionError(f"a test tried to start {args!r}")


@pytest.fixture(autouse=True)
def _no_real_task_store(monkeypatch, tmp_path_factory):
    """Tests never write to the person's own task store.

    Only test_store.py pointed the store anywhere; every other Engine a test built persisted its fake tasks
    to ~/Library/Application Support/macwork/tasks.db. `macwork profile` found it on its first run: a
    median decide of 29 ms and an observe of 0 ms are not numbers any real task produces. An explicit
    `engine.store` override in a test still wins over this — the environment is merged first.
    """
    monkeypatch.setenv("MACWORK__ENGINE__STORE", str(tmp_path_factory.mktemp("store") / "tasks.db"))
    # …and the same for standing grants, which matter more: a grant written by a test would be a real
    # permission on this Mac.
    monkeypatch.setenv("MACWORK__GRANTS__PATH", str(tmp_path_factory.mktemp("grants") / "grants.json"))


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    calls = []

    def fake(args, *a, **k):
        calls.append(args)
        return _Done()
    monkeypatch.setattr(subprocess, "run", fake)   # every module's subprocess.run is this one
    monkeypatch.setattr(subprocess, "Popen", _NoPopen)
    return calls


# --------------------------------------------------------------------------------------------------
# What the count means.
#
# The collected number is quoted as evidence of soundness in this project's own commit messages, and it
# overstates what it measures: most of it comes from a handful of parametrized static checks. One of them,
# `test_settings.py`, is four functions producing three hundred cases — one per shipped setting, which is
# the right shape for naming a dead key and the wrong shape for counting. Printing the breakdown at the end
# of every run means nobody has to be told twice.
_STATIC = {"test_settings.py", "test_wiring.py", "test_readme.py", "test_evals.py", "test_english_first.py"}


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    import ast
    import pathlib

    items = getattr(terminalreporter, "_macwork_items", None)
    if not items:
        return
    cases = {}
    for item in items:
        cases[pathlib.Path(str(item.fspath)).name] = cases.get(pathlib.Path(str(item.fspath)).name, 0) + 1
    functions = {}
    for f in pathlib.Path(str(config.rootpath) if hasattr(config, "rootpath") else ".").glob("tests/test_*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        functions[f.name] = sum(1 for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
    total_cases = sum(cases.values())
    total_funcs = sum(functions.get(name, 0) for name in cases)
    static_cases = sum(v for k, v in cases.items() if k in _STATIC)
    if not total_cases:
        return
    terminalreporter.write_line("")
    terminalreporter.write_line(
        f"{total_cases} cases from {total_funcs} test functions — "
        f"{total_cases - static_cases} behaviour, {static_cases} static checks "
        f"({', '.join(sorted(_STATIC & set(cases)))}). Quote the functions, not the cases.")


def pytest_collection_modifyitems(session, config, items):
    session.config.pluginmanager.get_plugin("terminalreporter")._macwork_items = list(items)
