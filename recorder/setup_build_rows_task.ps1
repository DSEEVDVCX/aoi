#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Registers the FomoBuildRows scheduled task (pipeline stage 3, previously manual).

.DESCRIPTION
    The recorder collects every minute and the labeler labels every 15 minutes,
    but building training_rows waited on a human. This task closes that gap.

    Settings deliberately mirror FomoLabeler (AtLogon, PT0S, IgnoreNew, restart
    999x every minute) because run_build_rows.py is an infinite loop just like
    run_labeler.py:
      - ExecutionTimeLimit = PT0S is required; the 3-day default silently kills it.
      - IgnoreNew prevents a second builder if the task restarts while one runs.

    ASCII only, no BOM: Windows PowerShell 5.1 decodes BOM-less .ps1 as ANSI, so
    non-ASCII comments become mojibake and break the parser. setup_backup_task.ps1
    follows the same rule.

.EXAMPLE
    powershell.exe -ExecutionPolicy Bypass -File setup_build_rows_task.ps1
    powershell.exe -ExecutionPolicy Bypass -File setup_build_rows_task.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "FomoBuildRows"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "run_build_rows.py"
if (-not (Test-Path $scriptPath)) { throw "Missing launcher: $scriptPath" }
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source

# Validate before touching Task Scheduler: a pythonw task has no window, so a
# configuration error would fail silently every cycle.
& py $scriptPath --check-config
if ($LASTEXITCODE -ne 0) { throw "build rows configuration validation failed." }

# A builder already running? Registering stays safe (INSERT OR REPLACE is
# idempotent) but two processes would contend for the write lock and duplicate work.
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like "*build_training_rows*" -or $_.CommandLine -like "*run_build_rows*" }
if ($running) {
    Write-Warning ("A builder is running now (PID {0}). The task will be registered and starts at next logon." -f ($running.ProcessId -join ', '))
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
        -Description "Builds training_rows from labeled outcomes (pipeline stage 3)" `
        -Force | Out-Null
    Write-Output "$TaskName registered: at logon; cadence from config.BUILD_ROWS_INTERVAL_SECONDS"
    Write-Output "Start now without waiting for logon:  Start-ScheduledTask -TaskName $TaskName"
}
