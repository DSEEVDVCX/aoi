"""Keep the machine from sleeping automatically while the recorder runs (Windows only).

**Why**: measurement over August 1–9 showed **74.4 stalled hours out of 214**
(34.8%) spread over 21 gaps, all from automatic sleep after idle on battery —
confirmed by the system log (`Kernel-Power 42` then `107`) at times matching
the gaps minute for minute. The 04:00 UTC hour is missing from **all** nine
days.

**What sleep costs is not uniform**: bars are fetched historically after the
wake-up and fully recovered, but `signal_events`, `token_social` and
`token_holders` are moment-in-time measurements from the live feed **with no
archive** — a coin that fired at 4 a.m. was simply never seen by the bot. The
damage is not holes in the data but **time-of-day bias** in the training set:
only daytime coins get represented, and that is an effect that cannot be fixed
retroactively from raw data, unlike everything fixed before it.

**How**: `SetThreadExecutionState` tells the system this process is active, so
it refrains from automatic sleep. The lock is **narrower than a power setting**:
tied to the process's lifetime — if the recorder stops, the machine sleeps as
usual — unlike `powercfg`, which disables sleep forever.

**What it does not do** (nothing can): a manual shutdown or `Sleep` from the
start menu stops the process like any other. `ES_SYSTEM_REQUIRED` blocks
**automatic** sleep only and does not defy an explicit user command. Nor does
it keep the screen on (`ES_DISPLAY_REQUIRED` was not requested: the screen
means nothing to data collection, and keeping it lit is cost without benefit).
"""
from __future__ import annotations

import sys

# SetThreadExecutionState flags from winbase.h
_ES_CONTINUOUS = 0x80000000        # state persists until cleared, not a one-shot pulse
_ES_SYSTEM_REQUIRED = 0x00000001   # do not put the system to sleep automatically


def keep_awake() -> bool:
    """Requests a wake lock for the current process. Returns True on success.

    Failure is not fatal: the recorder keeps running and sleep keeps punching
    holes in the data as before, so there is no sense in dropping a working
    collection over an enhancement that failed. The lock is tied to the
    **thread** that calls it, which is why it is called from the main thread
    before `asyncio.run`, never from a subtask.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        r = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        )
        return r != 0
    except Exception:  # noqa: BLE001 — an enhancement, not a requirement
        return False


def release() -> bool:
    """Drops the lock so the machine sleeps as usual."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS) != 0
    except Exception:  # noqa: BLE001
        return False
