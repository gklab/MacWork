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
def _no_real_processes(monkeypatch):
    calls = []

    def fake(args, *a, **k):
        calls.append(args)
        return _Done()
    monkeypatch.setattr(subprocess, "run", fake)   # every module's subprocess.run is this one
    monkeypatch.setattr(subprocess, "Popen", _NoPopen)
    return calls
