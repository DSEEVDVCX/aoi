"""Wake-lock tests — decision logic only, not the Windows call itself."""
import ctypes
import sys

import keep_awake
import pytest


def _stub(monkeypatch, ret=1, exc=None):
    """Replaces kernel32 and returns the list of observed flags.

    `keep_awake` imports ctypes **inside** the function, so patching the ctypes
    module itself is what it sees — not patching the keep_awake module.
    """
    seen: list[int] = []

    class _K32:
        @staticmethod
        def SetThreadExecutionState(flags):
            seen.append(flags)
            if exc is not None:
                raise exc
            return ret

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": _K32})(),
                        raising=False)
    return seen


def test_flags_match_winbase():
    """The flags are pinned to their winbase.h values — a mistake here means a
    silent lock failure."""
    assert keep_awake._ES_CONTINUOUS == 0x80000000
    assert keep_awake._ES_SYSTEM_REQUIRED == 0x00000001


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert keep_awake.keep_awake() is False
    assert keep_awake.release() is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_requests_continuous_system_required(monkeypatch):
    """Requests both flags together: without CONTINUOUS it is a pulse that
    ends immediately."""
    seen = _stub(monkeypatch)
    assert keep_awake.keep_awake() is True
    assert seen == [0x80000000 | 0x00000001]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_release_clears_only_continuous(monkeypatch):
    seen = _stub(monkeypatch)
    assert keep_awake.release() is True
    assert seen == [0x80000000]   # no SYSTEM_REQUIRED ⇒ the request is dropped


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_failure_is_not_fatal(monkeypatch):
    """A failed lock must not sink collection — an enhancement, not a
    requirement."""
    _stub(monkeypatch, exc=OSError("denied"))
    assert keep_awake.keep_awake() is False
    assert keep_awake.release() is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_zero_return_reported_as_failure(monkeypatch):
    """Zero from the API means refusal — we do not report fake success."""
    _stub(monkeypatch, ret=0)
    assert keep_awake.keep_awake() is False
