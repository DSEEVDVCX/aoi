#!/usr/bin/env pwsh
[CmdletBinding(SupportsShouldProcess)]
param(
    [string[]]$TaskName = @(
        "FomoRecorder",
        "FomoChain",
        "FomoEVMReplay",
        "FomoLabeler",
        "FomoBuildRows"
    )
)

$ErrorActionPreference = "Stop"

foreach ($name in $TaskName) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    $logon = New-ScheduledTaskTrigger `
        -AtLogOn -User $env:USERNAME -RandomDelay (New-TimeSpan -Minutes 5)
    $startup = New-ScheduledTaskTrigger `
        -AtStartup -RandomDelay (New-TimeSpan -Minutes 5)
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    if ($PSCmdlet.ShouldProcess($name, "Enable startup and idle-safe recovery")) {
        Set-ScheduledTask -TaskName $name -Trigger @($startup, $logon) -Settings $settings | Out-Null
        Write-Output "$name updated: startup/logon recovery enabled"
    }
}
