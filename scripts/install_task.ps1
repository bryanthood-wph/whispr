<#
.SYNOPSIS
  Install (or remove) the per-user Task Scheduler entry that autostarts whispr at
  logon. No admin elevation required - runs in the current user's context.

.USAGE
  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # install
  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove    # uninstall
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'whispr-recorder'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Pythonw  = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'   # pythonw = no console window

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task '$TaskName' found."
    }
    return
}

if (-not (Test-Path $Pythonw)) {
    throw "pythonw.exe not found at $Pythonw - create the venv first."
}

# Run 'python -m whispr' with the repo as working dir so config.yaml resolves.
$action  = New-ScheduledTaskAction -Execute $Pythonw -Argument '-m whispr' -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Restart on failure (covers a crash); keep the task alive indefinitely.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # 0 = no time limit
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' (starts whispr at logon)."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
