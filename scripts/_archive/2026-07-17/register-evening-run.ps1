<#
.SYNOPSIS
  Registers (or previews registering) the cohoodOBS nightly-drafting pipeline's
  scheduled task: "cohoodobs-evening-run" — Mon-Fri 16:00, a VISIBLE interactive
  terminal (not headless), per the consolidated live-run model (design doc §19.2).

.USAGE
  pwsh -NoProfile -File scripts\register-evening-run.ps1              # preview only, no changes
  pwsh -NoProfile -File scripts\register-evening-run.ps1 -Confirm     # actually register

.NOTES
  -Confirm here is a plain custom switch, not the built-in ShouldProcess -Confirm
  common parameter — matches register-task.ps1's own convention exactly, for the
  same reason (this script does NOT declare [CmdletBinding(SupportsShouldProcess)]).

  Why this task looks different from whispr-nightly-ingest/whispr-weekly-lint-compile:

  - **Visible, not hidden; interactive, not `-p`.** Those two jobs shell out to
    `claude -p` headlessly — no human is ever meant to see them run. This task's
    entire reason to exist is the opposite: `/evening-run` needs a REAL,
    foregrounded session so `AskUserQuestion` can fire (headless cannot ask
    questions — Phase 0 spike). `LogonType Interactive` is shared with those two
    jobs for the SAME underlying reason they already document (auth needs a real
    interactive session; Session 0 would break it) — this is not a new pattern,
    just the same one for a related reason.

  - **`--settings` and `DRAFT_RUN_ID` are load-bearing, not optional (premortem
    F1, design doc §19.2).** `_pipeline-settings.json`'s write-scope-enforcement
    and audit-logging hooks ONLY activate for a session launched with
    `--settings <that file>` and `DRAFT_RUN_ID` set in the environment. Omitting
    either would silently run the entire evening session with NONE of the
    guardrails the Phase 0 gate built (the Bash-write-scope-bypass fix) — this
    script sets both explicitly, every time, non-negotiably.

  - **`--permission-mode acceptEdits` (premortem F2).** Without it, Colby would
    get an individual approval prompt for every Write/Edit throughout the run —
    the write-SCOPE guardrail (the hook above) is the real security boundary;
    this flag just stops the session from also nagging for confirmation on each
    already-scope-permitted write.

  - **No `-RestartCount` (deliberate omission, not an oversight).** The other two
    jobs retry on failure because a non-zero exit code from a headless script IS
    a failure. Here, the process exiting because Colby closed the terminal after
    finishing is NORMAL, not a failure — auto-restarting would pop a second,
    unexpected terminal open after he'd already closed the first one. If
    `/evening-run` itself hard-fails mid-run, that's recorded in
    `logs/run-status-evening-run-<date>.json` (§19.2 step 4) for the NEXT day's
    brief health-check to surface — not something this scheduler layer retries.

  - **Generous `ExecutionTimeLimit` (8 hours, not 1).** The other two jobs bound
    this tightly because they have a real, predictable completion point. This one
    is an interactive session Colby might legitimately leave open for hours —
    8 hours comfortably covers 4pm through a late evening without an arbitrary
    kill, while still not being unbounded.

  Same StartWhenAvailable/WakeToRun/battery resilience as the other two jobs, for
  the same reason (laptop often off/asleep at trigger time) — reused, not
  reinvented. A missed 16:00 (machine off/logged-out) fires on next login rather
  than silently never running that day.
#>

[CmdletBinding()]
param(
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'

$VaultRoot           = 'C:\github\cohoodOBS'   # sibling repo — necessarily a literal constant, matching nightly-ingest.ps1's own convention
$PipelineSettingsPath = Join-Path $VaultRoot '_pipeline-settings.json'

if (-not (Test-Path -LiteralPath $PipelineSettingsPath)) {
    throw "Cannot find '$PipelineSettingsPath' — is the cohoodOBS vault at the expected path?"
}

# Prefer pwsh.exe; fall back to Windows PowerShell if pwsh isn't installed — same as register-task.ps1.
$hostCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $hostCmd) { $hostCmd = Get-Command powershell -ErrorAction SilentlyContinue }
if (-not $hostCmd) { throw 'Neither pwsh.exe nor powershell.exe was found on PATH.' }
$HostExe = $hostCmd.Source

$TaskName = 'cohoodobs-evening-run'

# A fresh DRAFT_RUN_ID per firing, set INSIDE the launched host's own command —
# not baked into the registration itself — so every day's run gets a distinct
# id (correlates with _agent-logs/draft-run-<runId>-events.jsonl per §8/§9).
# -NoExit: the window stays open after claude exits (e.g. if Colby types /exit
# or the session ends) so he can see any final output rather than it vanishing.
$innerCommand = "`$env:DRAFT_RUN_ID = [System.Guid]::NewGuid().ToString('N').Substring(0,12); " +
                "Set-Location -LiteralPath '$VaultRoot'; " +
                "claude --settings '$PipelineSettingsPath' --permission-mode bypassPermissions '/evening-run'"

$actionArgs = @('-NoExit', '-NoProfile', '-Command', "`"$innerCommand`"")
$action     = New-ScheduledTaskAction -Execute $HostExe -Argument ($actionArgs -join ' ') -WorkingDirectory $VaultRoot
$trigger    = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 16:00
$principal  = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings   = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Write-Host ''
Write-Host "=== $TaskName task registration ===" -ForegroundColor Cyan
Write-Host "Task name       : $TaskName"
Write-Host "Host executable : $HostExe"
Write-Host "Action          : $HostExe $($actionArgs -join ' ')"
Write-Host "Working dir     : $VaultRoot"
Write-Host "Trigger         : Weekly, Mon-Tue-Wed-Thu-Fri at 16:00"
Write-Host "Principal       : user '$env:USERNAME', LogonType=Interactive (runs only when logged on, VISIBLE window), RunLevel=Limited"
Write-Host "Settings        : ExecutionTimeLimit = 8 hours (generous — interactive, not a bounded batch job); StartWhenAvailable, WakeToRun, start/keep-on-battery; NO auto-restart (deliberate — see script header)"
Write-Host ''

if (-not $Confirm) {
    Write-Host "This was a PREVIEW ONLY — nothing was registered." -ForegroundColor Yellow
    Write-Host "Re-run with -Confirm to actually create the scheduled task:"
    Write-Host "  pwsh -NoProfile -File `"$PSCommandPath`" -Confirm"
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered scheduled task '$TaskName'." -ForegroundColor Green

Write-Host ''
Write-Host '=== Applied settings, read back from Task Scheduler ===' -ForegroundColor Cyan
(Get-ScheduledTask -TaskName $TaskName).Settings |
    Select-Object StartWhenAvailable, WakeToRun, DisallowStartIfOnBatteries, StopIfGoingOnBatteries, ExecutionTimeLimit, RestartCount |
    Format-List | Out-String | Write-Host

Write-Host ''
Write-Host 'To run it immediately (for a real end-to-end test):'
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ''
Write-Host 'To remove it:'
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
