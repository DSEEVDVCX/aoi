"""Launcher for the 1m bar archiving task — separate from the training-rows builder.

The separation is deliberate: both are SQLite writers; a single process/asyncio
loop would hit database locked. The task runs hourly, resumes partial states,
and picks up newly matured tokens.
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> None:
    import backfill_bars_1m

    raise SystemExit(asyncio.run(backfill_bars_1m.main()))


if __name__ == "__main__":
    main()
