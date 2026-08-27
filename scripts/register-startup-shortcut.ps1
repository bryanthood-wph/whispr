<#
.SYNOPSIS
  Creates (or removes) a Windows Startup-folder shortcut that launches whispr
  at logon: pythonw.exe -m whispr, from the repo venv, no console window.

.USAGE
  pwsh -NoProfile -File scripts\register-startup-shortcut.ps1            # create
  pwsh -NoProfile -File scripts\register-startup-shortcut.ps1 -Remove    # remove

.NOTES
  FOR THE INSTALLED DISTRIBUTION ONLY. On the author's dev machine the recorder
  is started by the whispr-recorder scheduled task, whose action is the
  watch-recorder.ps1 liveness watchdog -- see register-task.ps1's .SYNOPSIS.
  This shortcut is not used there and creating one would only race that task.

  RETRACTED (2026-08-27): this file previously stated as fact that -AtLogOn
  scheduled-task triggers were "confirmed empirically to be blocked by policy
  for this non-admin account", and that this was why the Startup folder was
  chosen. Observation contradicts it -- an -AtLogOn trigger fired on 2026-08-23
  06:19:33, and the whispr-recorder task uses one today. Either the policy
  changed or the original isolation missed a variable. The claim is left here
  only as history; do not rely on it.

  whispr's own single-instance mutex (_acquire_single_instance in __main__.py)
  already prevents a double-launch if whispr happens to already be running at
  logon, so no "is it already running" check is needed here.
#>

[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$RepoRoot     = Split-Path -Parent $PSScriptRoot                       # C:\github\whispr
# Probe both layouts, dev first, exactly as run-whispr.cmd does. Previously this
# hardcoded the .venv path and hard-threw when it was missing, so the script could
# not serve the installed-distribution case it exists for: a distribution has
# python\pythonw.exe and no .venv at all (found 2026-08-27).
$AppExe = $null
foreach ($candidate in @(
    (Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'),   # development checkout
    (Join-Path $RepoRoot 'python\pythonw.exe')            # installed distribution
)) {
    if (Test-Path -LiteralPath $candidate) { $AppExe = $candidate; break }
}
$StartupDir   = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'whispr.lnk'

if ($Remove) {
    if (Test-Path -LiteralPath $ShortcutPath) {
        Remove-Item -LiteralPath $ShortcutPath -Force
        Write-Host "Removed '$ShortcutPath'." -ForegroundColor Green
    } else {
        Write-Host "No shortcut found at '$ShortcutPath' -- nothing to remove." -ForegroundColor Yellow
    }
    return
}

if (-not $AppExe) {
    throw "No whispr Python interpreter found. Tried '$RepoRoot\.venv\Scripts\pythonw.exe' (development checkout) and '$RepoRoot\python\pythonw.exe' (installed distribution)."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $AppExe
$shortcut.Arguments = '-m whispr'
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.Description = 'whispr - Teams call recorder (starts at logon)'
$shortcut.Save()

Write-Host "Created '$ShortcutPath'"
Write-Host "  Target      : $AppExe"
Write-Host "  Arguments   : -m whispr"
Write-Host "  Working dir : $RepoRoot"
Write-Host ''
Write-Host 'whispr will now start automatically at your next logon.'
Write-Host "To remove: pwsh -NoProfile -File `"$PSCommandPath`" -Remove"
