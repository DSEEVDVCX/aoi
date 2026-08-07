#!/usr/bin/env pwsh
param(
    [string]$Destination = $(if ($env:AOI_BACKUP_DIR) {
        $env:AOI_BACKUP_DIR
    } elseif ($env:OneDrive) {
        Join-Path $env:OneDrive "aoi-backups"
    } else {
        ""
    }),
    [int]$Keep = 3,
    [string]$At = "03:15"
)

$ErrorActionPreference = "Stop"
if (-not $Destination) {
    throw "Set AOI_BACKUP_DIR or pass -Destination to an external/synchronized directory."
}
if ($Keep -lt 1) { throw "Keep must be at least 1." }

$scriptPath = Join-Path $PSScriptRoot "backup_db.py"
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$arguments = '"{0}" --destination "{1}" --keep {2}' -f $scriptPath, $Destination, $Keep

# Validate before touching Task Scheduler.
& py $scriptPath --destination $Destination --keep $Keep --check-config
if ($LASTEXITCODE -ne 0) { throw "Backup configuration validation failed." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask -TaskName "FomoBackup" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "Daily verified backup of fomo recorder.db" `
    -Force | Out-Null

Write-Output "FomoBackup registered: daily $At -> $Destination (keep $Keep)"
