"""Measure a command's peak working set and CPU time without changing state."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="+", help="command and arguments to measure")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    process = subprocess.Popen(args.command)
    peak = 0
    while process.poll() is None:
        try:
            import psutil
            peak = max(peak, psutil.Process(process.pid).memory_info().rss)
        except (ImportError, OSError):
            pass
        time.sleep(0.05)
    elapsed = time.perf_counter() - started
    result = {"returncode": process.returncode, "elapsed_seconds": round(elapsed, 3), "peak_rss_bytes": peak}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
