"""Read-only Windows Scheduled Task evidence for dashboard health."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

TASK_NAMES = (
    "FomoApiServer", "FomoRecorder", "FomoChain", "FomoLabeler",
    "FomoEVMReplay", "FomoBuildRows", "FomoActivityHead", "FomoDashboard",
    "FomoBars1m", "FomoBackup",
)


def read_task_states(timeout: float = 2.0) -> dict[str, dict[str, Any]]:
    """Return scheduler evidence without changing task state or exposing secrets."""
    if os.name != "nt":
        return {}
    script = (
        "$names = @(" + ",".join(json.dumps(name) for name in TASK_NAMES) + "); "
        + "$names | ForEach-Object { "
        + "try { $t = Get-ScheduledTask -TaskName $_ -ErrorAction Stop; "
        + "$i = Get-ScheduledTaskInfo -TaskName $_ -ErrorAction Stop; "
        + "[pscustomobject]@{Name=$_.ToString(); State=$t.State.ToString(); "
        + "LastRunTime=$i.LastRunTime.ToString('o'); LastTaskResult=$i.LastTaskResult} } "
        + "catch { [pscustomobject]@{Name=$_; State='Missing'; LastRunTime=$null; LastTaskResult=$null} } "
        + "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-Command", script],
            capture_output=True, text=True, timeout=max(0.1, timeout), check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        payload = json.loads(result.stdout)
        items = payload if isinstance(payload, list) else [payload]
        return {
            str(item["Name"]): {
                "state": item.get("State"),
                "last_run_time": item.get("LastRunTime"),
                "last_task_result": item.get("LastTaskResult"),
            }
            for item in items if isinstance(item, dict) and item.get("Name")
        }
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {}
