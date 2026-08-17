#!/usr/bin/env pwsh
[CmdletBinding(SupportsShouldProcess)]
param([string]$TaskName = "FomoEVMReplay")

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "run_evm_replay.py"
if (-not (Test-Path $scriptPath)) { throw "Missing launcher: $scriptPath" }
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
& py $scriptPath --check-config
if ($LASTEXITCODE -ne 0) { throw "EVM replay configuration validation failed." }

$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like "*run_evm_replay*" -or $_.CommandLine -like "*evm_replay.py*" }
if ($running) {
    Write-Warning ("A replay worker is running now (PID {0}). The task will be registered and starts at next logon." -f ($running.ProcessId -join ', '))
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
        -Description "Backfills EVM holder concentration snapshots incrementally" `
        -Force | Out-Null
    Write-Output "$TaskName registered: at logon; cadence from config.EVM_REPLAY_INTERVAL_SECONDS"
    Write-Output "Start now without waiting for logon: Start-ScheduledTask -TaskName $TaskName"
}
