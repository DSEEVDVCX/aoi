#!/usr/bin/env pwsh
[CmdletBinding(SupportsShouldProcess)]
param([string]$TaskName = "FomoActivityHead")

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "run_activity_head.py"
if (-not (Test-Path $scriptPath)) { throw "Missing launcher: $scriptPath" }
$projectPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\pythonw.exe"
$pythonw = if (Test-Path $projectPython) { $projectPython } else { (Get-Command pythonw.exe -ErrorAction Stop).Source }

$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like "*run_activity_head*" }
if ($running) {
    Write-Warning ("An activity-head worker is already running (PID {0})." -f ($running.ProcessId -join ', '))
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$scriptPath`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "Backfills current trading activity from the upstream head" `
        -Force | Out-Null
    Write-Output "$TaskName registered: at logon; cadence from config.ACTIVITY_HEAD_INTERVAL_SECONDS"
    Write-Output "Start now: Start-ScheduledTask -TaskName $TaskName"
}
