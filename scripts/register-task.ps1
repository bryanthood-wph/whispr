<#
.SYNOPSIS
  Registers (or previews registering) whispr's scheduled tasks:
    - "whispr-nightly-ingest"          — nightly-ingest.ps1,            Mon-Fri 22:00
    - "whispr-weekly-lint-compile"     — weekly-lint-compile.ps1,       Sunday  22:00
    - "whispr-nightly-freshness-check" — check-nightly-freshness.ps1,   daily 09:00 & 20:00
                                          (watchdog — 2026-07-24 finding: a catch-up run
                                          can be killed externally, STATUS_CONTROL_C_EXIT,
                                          before its own try/finally ever gets a chance to
                                          alert. See check-nightly-freshness.ps1's own
                                          header for the full mechanism.)
    - "whispr-recorder"                — watch-recorder.ps1,            at logon + every 15 min

  RECORDER LAUNCH — supersedes the Startup-shortcut design (2026-08-26).
  whispr the recorder is started by the "whispr-recorder" task above, whose
  action is the watch-recorder.ps1 liveness watchdog (NOT pythonw directly).
  The watchdog is the recorder's single start path: at logon and then on every
  repetition it checks whispr's own single-instance mutex and launches only if
  no instance holds it.

  This replaces two earlier mechanisms, both of which failed:
    - The Startup-folder shortcut (register-startup-shortcut.ps1) was the
      documented design but was NOT present on the machine as of 2026-08-26,
      so it provided no logon start at all. That script is retained for the
      distribution case; it is no longer the mechanism here.
    - A hand-made "whispr-recorder" task existed instead, registered outside
      this script, running pythonw directly with a bare -AtLogOn trigger and
      no repetition. When whispr was terminated on 2026-08-23 the trigger
      never re-fired (the machine neither rebooted nor logged off), and the
      recorder stayed dead for three days with nothing reporting it.

  HISTORICAL NOTE, now contradicted by observation: this file previously
  recorded that -AtLogOn triggers were empirically blocked by policy for this
  non-admin account (isolated by testing identical Principal/Settings with only
  the trigger type changed). That finding no longer matches reality — the
  hand-made task's -AtLogOn trigger demonstrably fired on 2026-08-23 06:19:33.
  Either the policy changed or the original isolation missed a variable. The
  registration below therefore VERIFIES the trigger read-back rather than
  trusting either account; if -AtLogOn is ever blocked again the read-back is
  what will show it.

.USAGE
  pwsh -NoProfile -File scripts\register-task.ps1              # preview only, no changes
  pwsh -NoProfile -File scripts\register-task.ps1 -Confirm     # actually register all four

.NOTES
  -Confirm here is a plain custom switch, not the built-in ShouldProcess -Confirm
  common parameter — this script does NOT declare [CmdletBinding(SupportsShouldProcess)]
  precisely to avoid that collision, so -Confirm means exactly one thing: "yes,
  actually call Register-ScheduledTask for all four tasks," nothing more.

  Triggers:
    whispr-nightly-ingest      : weekly, Mon-Fri, 22:00. ExecutionTimeLimit 1 hour.
    whispr-weekly-lint-compile : weekly, Sunday,  22:00. ExecutionTimeLimit 2 hours
                                 (measured /lint cost is ~19 min; the 2-hour cap
                                 leaves headroom for -MaxCompiles compile calls
                                 stacked after it).

  Logon requirement (nightly-ingest and weekly-lint-compile): "run only when
  user is logged on" (NOT "run
  whether logged on or not"). This is deliberate — both scripts shell out to the
  Claude Code CLI under the interactive Deloitte enterprise auth session. Running
  in Session 0 (the non-interactive service session used by "run whether logged
  on or not") could break that auth/interactive context entirely, so we trade
  "runs even if nobody is logged on" for "auth actually works."

  RestartCount caveat (2026-07-17 finding): empirically tested against two
  disposable tasks (one exiting 0, one exiting 1, both under RestartCount) and
  confirmed NEITHER restarts. Task Scheduler's RestartOnFailure only fires when
  the SCHEDULER fails to run the task (killed by the scheduler, hit the
  execution-time limit) — not when the launched process exits on its own, even
  with a non-zero code. So the RestartCount=2/RestartCount=1 settings below are
  not doing what their comments originally assumed for an ordinary script
  failure; they'd only help if the *scheduler* itself killed the run (e.g. an
  ExecutionTimeLimit timeout). Left in place since they're harmless and do cover
  that narrower case, but not relied on as the transient-failure safety net the
  original design intended. Revisit if real transient failures are observed.
#>

[CmdletBinding()]
param(
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot                       # C:\github\whispr

# Resolve a host executable whose path SURVIVES A POWERSHELL UPDATE.
#
# Do NOT just use (Get-Command pwsh).Source (2026-08-27 finding). When pwsh is
# installed from the Store/MSIX, that resolves to a VERSION-STAMPED package
# directory — on this machine
#   C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe
# — and Register-ScheduledTask bakes the literal string into every task action.
# The next PowerShell update installs a new versioned directory and removes the
# old one, at which point all four tasks silently fail to start with "the system
# cannot find the file specified". That would take out the recorder's ONLY start
# path, and nothing would report it: exactly the 2026-08-23 outage, on a timer set
# by an unrelated software update.
#
# Preference order, most durable first:
#   1. C:\Program Files\PowerShell\7\pwsh.exe   MSI/ZIP install; version-agnostic
#                                               path, updated in place
#   2. %LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe   the MSIX app-execution
#                                               alias — a stable reparse point that
#                                               is re-pointed by the update rather
#                                               than deleted (verified launchable)
#   3. whatever is on PATH                      last resort, warned about below
#   4. powershell.exe                           only if pwsh is absent entirely.
#      NOTE: Windows PowerShell 5.1 CANNOT parse sync-common.ps1, which is UTF-8
#      without a BOM and whose em-dashes 5.1 mis-decodes into a parse error, so
#      this fallback will not actually work for the scripts that dot-source it.
#      Left in place only so the failure is a loud parse error rather than a
#      missing-file error.
$HostExe = $null
foreach ($candidate in @(
    (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe')
)) {
    if (Test-Path -LiteralPath $candidate) { $HostExe = $candidate; break }
}
if (-not $HostExe) {
    $hostCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $hostCmd) { $hostCmd = Get-Command powershell -ErrorAction SilentlyContinue }
    if (-not $hostCmd) { throw 'Neither pwsh.exe nor powershell.exe was found on PATH.' }
    $HostExe = $hostCmd.Source
}
# Catch the bad shape whatever produced it, including a future edit to the list.
if ($HostExe -match 'WindowsApps\\Microsoft\.PowerShell_\d') {
    Write-Host "WARNING: host executable '$HostExe' is a version-stamped MSIX path." -ForegroundColor Red
    Write-Host "         It will stop existing at the next PowerShell update and every task" -ForegroundColor Red
    Write-Host "         registered here will silently fail to start. Install PowerShell 7 via" -ForegroundColor Red
    Write-Host "         the MSI, or re-run once the WindowsApps alias is available." -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Task 1: whispr-nightly-ingest
# ---------------------------------------------------------------------------
$NightlyTaskName   = 'whispr-nightly-ingest'
$NightlyScriptPath = Join-Path $RepoRoot 'scripts\nightly-ingest.ps1'

if (-not (Test-Path -LiteralPath $NightlyScriptPath)) {
    throw "Cannot find '$NightlyScriptPath' — register-task.ps1 must live alongside nightly-ingest.ps1."
}

$nightlyActionArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$NightlyScriptPath`"")
$nightlyAction    = New-ScheduledTaskAction -Execute $HostExe -Argument ($nightlyActionArgs -join ' ') -WorkingDirectory $RepoRoot
$nightlyTrigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 22:00
$nightlyPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# Scheduler-level backstop time limit, independent of nightly-ingest.ps1's own
# per-claude-call -TimeoutSeconds watchdog — this guards against the whole
# script somehow hanging outside any single claude call.
# Laptop-resilient settings (host is often off/asleep/logged-out at 22:00):
#   -StartWhenAvailable          run ASAP after a missed 22:00 (the watermark means
#                                one make-up run processes ALL accumulated transcripts)
#   -WakeToRun                   wake from sleep to run (AC only; can't power on a
#                                fully shut-down laptop)
#   -AllowStartIfOnBatteries / -DontStopIfGoingOnBatteries   don't skip/kill on battery
#   -RestartCount 2              nightly is cheap (~$3), so retry transient failures twice
$nightlySettings  = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

# ---------------------------------------------------------------------------
# Task 2: whispr-weekly-lint-compile
# ---------------------------------------------------------------------------
$WeeklyTaskName   = 'whispr-weekly-lint-compile'
$WeeklyScriptPath = Join-Path $RepoRoot 'scripts\weekly-lint-compile.ps1'

if (-not (Test-Path -LiteralPath $WeeklyScriptPath)) {
    throw "Cannot find '$WeeklyScriptPath' — register-task.ps1 must live alongside weekly-lint-compile.ps1."
}

$weeklyActionArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$WeeklyScriptPath`"")
$weeklyAction    = New-ScheduledTaskAction -Execute $HostExe -Argument ($weeklyActionArgs -join ' ') -WorkingDirectory $RepoRoot
$weeklyTrigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 22:00
$weeklyPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# /lint alone measured at ~19 min; 2 hours leaves headroom for -MaxCompiles
# /compile calls stacked after it. Same laptop-resilient switches as nightly,
# EXCEPT -RestartCount 1 (not 2): a retry re-runs the WHOLE script = another full
# ~$8 lint, and the likeliest weekly failure (lint timeout) won't benefit from an
# immediate retry — so weekly is bounded to a single transient-failure retry.
$weeklySettings  = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)

# ---------------------------------------------------------------------------
# Task 3: whispr-nightly-freshness-check (watchdog, not the sync jobs themselves)
# ---------------------------------------------------------------------------
$FreshnessTaskName   = 'whispr-nightly-freshness-check'
$FreshnessScriptPath = Join-Path $RepoRoot 'scripts\check-nightly-freshness.ps1'

if (-not (Test-Path -LiteralPath $FreshnessScriptPath)) {
    throw "Cannot find '$FreshnessScriptPath' — register-task.ps1 must live alongside check-nightly-freshness.ps1."
}

$freshnessActionArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$FreshnessScriptPath`"")
$freshnessAction     = New-ScheduledTaskAction -Execute $HostExe -Argument ($freshnessActionArgs -join ' ') -WorkingDirectory $RepoRoot
# Two fixed daily fire times, one task — Register-ScheduledTask's -Trigger takes an array.
$freshnessTrigger09 = New-ScheduledTaskTrigger -Daily -At 09:00
$freshnessTrigger20 = New-ScheduledTaskTrigger -Daily -At 20:00
$freshnessPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# Interactive logon here is ONLY for toast delivery — this script never calls
# claude, so unlike the two jobs above it has no CLI-auth reason to need it.
# No -WakeToRun: waking the machine solely to check whether ANOTHER task ran
# isn't worth the disruption — if the laptop's asleep nobody's there to see
# the toast anyway, and the check just runs at the next natural wake/logon
# via -StartWhenAvailable. No -RestartCount: per this file's own documented
# 2026-07-17 finding, RestartCount only helps when the SCHEDULER itself kills
# a run (e.g. hits ExecutionTimeLimit) — this script finishes in well under a
# second, so that scenario essentially can't occur; setting it would just be
# unused ceremony copied from the other two blocks (P6).
$freshnessSettings  = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# ---------------------------------------------------------------------------
# Task 4: whispr-recorder (the recorder's ONLY start path — see .SYNOPSIS)
# ---------------------------------------------------------------------------
$RecorderTaskName   = 'whispr-recorder'
$RecorderScriptPath = Join-Path $RepoRoot 'scripts\watch-recorder.ps1'
$RecorderIntervalMin = 15

if (-not (Test-Path -LiteralPath $RecorderScriptPath)) {
    throw "Cannot find '$RecorderScriptPath' — register-task.ps1 must live alongside watch-recorder.ps1."
}

$recorderActionArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$RecorderScriptPath`"")
$recorderAction     = New-ScheduledTaskAction -Execute $HostExe -Argument ($recorderActionArgs -join ' ') -WorkingDirectory $RepoRoot

# TWO triggers, and the split is load-bearing (2026-08-27 finding).
#
# -AtLogOn covers "start whispr when I sit down". It does NOT cover "whispr died
# at 11am and the machine won't reboot for days" — the 2026-08-23 outage — and
# grafting a repetition onto the logon trigger does not fix that, even though the
# resulting task XML looks entirely correct. Task Scheduler arms a trigger's
# repetition when that TRIGGER FIRES, so a repetition hung off -AtLogOn stays
# dormant until the next logon. Registering the task mid-session therefore leaves
# NextRunTime empty and nothing firing at all.
#
# Measured directly rather than inferred: with only the grafted logon repetition
# registered, the task's LastRunTime did not advance across a 16-minute idle
# window (2026-08-27). That is the outage's exact failure mode, reintroduced by a
# construct that reads as correct.
#
# So the heartbeat gets its own -Once trigger, anchored at midnight TODAY — a time
# already in the past, so the repetition is live the moment the task is registered
# and keeps firing regardless of logon activity. Midnight rather than (Get-Date)
# so re-registering is deterministic instead of baking in a launch timestamp.
$recorderTriggerLogon  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$recorderTriggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes $RecorderIntervalMin) `
    -RepetitionDuration (New-TimeSpan -Days 1)

# Duration is then cleared, because in the Task Scheduler XML schema an ABSENT
# <Duration> is what means "repeat indefinitely". Do NOT reach for the widely
# cited [TimeSpan]::MaxValue idiom here: it serialises to P99999999DT23H59M59S,
# which this build rejects outright with "task XML contains a value which is
# incorrectly formatted or out of range" (observed 2026-08-26). Note also that
# it fails at Register-ScheduledTask, NOT at the assignment below — so a
# try/catch around the graft would look like it was protecting something while
# catching nothing. The real guard is around the Register call further down.
# StopAtDurationEnd is forced off explicitly: paired with an absent Duration it is
# meaningless at best, and it defaults to true, which reads as a contradiction to
# anyone auditing the XML later.
$recorderTriggerRepeat.Repetition.Duration          = $null
$recorderTriggerRepeat.Repetition.StopAtDurationEnd = $false

$recorderTriggers = @($recorderTriggerLogon, $recorderTriggerRepeat)

$recorderPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# ExecutionTimeLimit 5 min, NOT PT0S: the watchdog finishes in ~8 s, so a run
# still alive at 5 minutes is hung, and killing it lets the next repetition
# proceed. Capping it is only safe because watch-recorder.ps1 launches whispr
# via Win32_Process::Create — the new process is parented to the WMI provider
# host, OUTSIDE this task's job object (verified 2026-08-26: relaunched PID's
# parent was WmiPrvSE.exe), so terminating a hung watchdog cannot take the
# recorder down with it. If that launch mechanism is ever changed back to an
# ordinary child process, this limit MUST go with it.
#   -MultipleInstances IgnoreNew   a slow run must not let checks pile up
#   -DontStopOnIdleEnd             disarm the idle-stop default outright; the
#                                  hand-made task carried StopOnIdleEnd=true and
#                                  was saved only by RunOnlyIfIdle being unset
#   no -WakeToRun                  waking the laptop purely to check the recorder
#                                  isn't worth it — nobody's in a call while it
#                                  sleeps, and logon re-fires the trigger anyway
#   no -RestartCount               per this file's 2026-07-17 finding it never
#                                  fires on a process's own exit; the 2026-08-23
#                                  outage re-confirmed it (the hand-made task had
#                                  RestartCount=3 and still did not recover). The
#                                  repetition above is the real retry mechanism.
$recorderSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew

# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host "=== whispr-nightly-ingest task registration ===" -ForegroundColor Cyan
Write-Host "Task name       : $NightlyTaskName"
Write-Host "Host executable : $HostExe"
Write-Host "Action          : $HostExe $($nightlyActionArgs -join ' ')"
Write-Host "Working dir     : $RepoRoot"
Write-Host "Trigger         : Weekly, Mon-Tue-Wed-Thu-Fri at 22:00"
Write-Host "Principal       : user '$env:USERNAME', LogonType=Interactive (runs only when logged on), RunLevel=Limited"
Write-Host "Settings        : ExecutionTimeLimit = 1 hour; StartWhenAvailable, WakeToRun, start/keep-on-battery, RestartCount=2 @ 5 min"
Write-Host ''
Write-Host "=== whispr-weekly-lint-compile task registration ===" -ForegroundColor Cyan
Write-Host "Task name       : $WeeklyTaskName"
Write-Host "Host executable : $HostExe"
Write-Host "Action          : $HostExe $($weeklyActionArgs -join ' ')"
Write-Host "Working dir     : $RepoRoot"
Write-Host "Trigger         : Weekly, Sunday at 22:00"
Write-Host "Principal       : user '$env:USERNAME', LogonType=Interactive (runs only when logged on), RunLevel=Limited"
Write-Host "Settings        : ExecutionTimeLimit = 2 hours; StartWhenAvailable, WakeToRun, start/keep-on-battery, RestartCount=1 @ 5 min"
Write-Host ''
Write-Host "=== whispr-nightly-freshness-check task registration ===" -ForegroundColor Cyan
Write-Host "Task name       : $FreshnessTaskName"
Write-Host "Host executable : $HostExe"
Write-Host "Action          : $HostExe $($freshnessActionArgs -join ' ')"
Write-Host "Working dir     : $RepoRoot"
Write-Host "Trigger         : Daily at 09:00 and 20:00"
Write-Host "Principal       : user '$env:USERNAME', LogonType=Interactive (toast delivery only, no CLI auth need), RunLevel=Limited"
Write-Host "Settings        : ExecutionTimeLimit = 5 min; StartWhenAvailable, start/keep-on-battery; no WakeToRun, no RestartCount (see script comments)"
Write-Host ''
Write-Host "=== whispr-recorder task registration ===" -ForegroundColor Cyan
Write-Host "Task name       : $RecorderTaskName"
Write-Host "Host executable : $HostExe"
Write-Host "Action          : $HostExe $($recorderActionArgs -join ' ')"
Write-Host "Working dir     : $RepoRoot"
Write-Host "Trigger         : (1) at logon  +  (2) every $RecorderIntervalMin minutes indefinitely from midnight"
Write-Host "                  two separate triggers on purpose — a repetition hung off the logon"
Write-Host "                  trigger stays dormant until the next logon (see Task 4 comments)."
Write-Host "Principal       : user '$env:USERNAME', LogonType=Interactive, RunLevel=Limited"
Write-Host "Settings        : ExecutionTimeLimit = 5 min; StartWhenAvailable, start/keep-on-battery, IgnoreNew, DontStopOnIdleEnd; no WakeToRun, no RestartCount"
Write-Host "NOTE            : this REPLACES the hand-made whispr-recorder task that ran pythonw directly." -ForegroundColor Yellow
Write-Host ''

if (-not $Confirm) {
    Write-Host "This was a PREVIEW ONLY — nothing was registered." -ForegroundColor Yellow
    Write-Host "Re-run with -Confirm to actually create all four scheduled tasks:"
    Write-Host "  pwsh -NoProfile -File `"$PSCommandPath`" -Confirm"
    return
}

Register-ScheduledTask -TaskName $NightlyTaskName -Action $nightlyAction -Trigger $nightlyTrigger -Principal $nightlyPrincipal -Settings $nightlySettings -Force | Out-Null
Write-Host "Registered scheduled task '$NightlyTaskName'." -ForegroundColor Green

Register-ScheduledTask -TaskName $WeeklyTaskName -Action $weeklyAction -Trigger $weeklyTrigger -Principal $weeklyPrincipal -Settings $weeklySettings -Force | Out-Null
Write-Host "Registered scheduled task '$WeeklyTaskName'." -ForegroundColor Green

Register-ScheduledTask -TaskName $FreshnessTaskName -Action $freshnessAction -Trigger @($freshnessTrigger09, $freshnessTrigger20) -Principal $freshnessPrincipal -Settings $freshnessSettings -Force | Out-Null
Write-Host "Registered scheduled task '$FreshnessTaskName'." -ForegroundColor Green

# Guarded HERE, not around the trigger construction: an out-of-range repetition
# duration is only rejected when the task XML is submitted. Falling back to a
# finite 10-year duration keeps the recorder covered even on a build that
# refuses an open-ended one; the read-back below reports which form landed.
try {
    Register-ScheduledTask -TaskName $RecorderTaskName -Action $recorderAction -Trigger $recorderTriggers -Principal $recorderPrincipal -Settings $recorderSettings -Force | Out-Null
} catch {
    Write-Host "  (indefinite repetition rejected: $($_.Exception.Message.Trim()) — retrying with a 3650-day duration)" -ForegroundColor Yellow
    $recorderTriggerRepeat.Repetition.Duration = 'P3650D'
    $recorderTriggers = @($recorderTriggerLogon, $recorderTriggerRepeat)
    Register-ScheduledTask -TaskName $RecorderTaskName -Action $recorderAction -Trigger $recorderTriggers -Principal $recorderPrincipal -Settings $recorderSettings -Force | Out-Null
}
Write-Host "Registered scheduled task '$RecorderTaskName'." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Read the settings BACK from Task Scheduler and show them — prove they applied,
# don't assume the -Force register silently took them. Note the battery fields
# invert: passing -AllowStartIfOnBatteries makes DisallowStartIfOnBatteries=False,
# and -DontStopIfGoingOnBatteries makes StopIfGoingOnBatteries=False.
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Applied settings, read back from Task Scheduler ===' -ForegroundColor Cyan
foreach ($tn in @($NightlyTaskName, $WeeklyTaskName, $FreshnessTaskName, $RecorderTaskName)) {
    Write-Host ''
    Write-Host "[$tn]" -ForegroundColor Cyan
    # MultipleInstances and StopOnIdleEnd are here because both are load-bearing
    # and neither was being read back (added 2026-08-27): IgnoreNew is what stops
    # a slow run letting checks pile up, and StopOnIdleEnd=true is what the
    # hand-made task carried before this script existed. StopOnIdleEnd is nested
    # under IdleSettings, so it needs the calculated property — a bare name would
    # have silently printed nothing, which is the failure this block exists to
    # catch rather than commit.
    (Get-ScheduledTask -TaskName $tn).Settings |
        Select-Object StartWhenAvailable, WakeToRun, DisallowStartIfOnBatteries,
            StopIfGoingOnBatteries, ExecutionTimeLimit, RestartCount, RestartInterval,
            MultipleInstances, @{ n = 'StopOnIdleEnd'; e = { $_.IdleSettings.StopOnIdleEnd } } |
        Format-List | Out-String | Write-Host
}

# The recorder's periodic recovery is the one thing here that can silently not
# take, so it is verified against NextRunTime — NOT against the trigger XML.
#
# An earlier version of this check read the repetition Interval out of the
# registered trigger and printed "VERIFIED" whenever it was present. That check
# passed on a task that was firing NOTHING (2026-08-27): the interval really was
# in the XML, it simply was not armed, because it had been grafted onto the logon
# trigger. Confirming a control by re-reading the value you just wrote is not
# verification — it is the same false-green that let check-nightly-freshness.ps1
# log "OK" through all three days of the 2026-08-23 outage.
#
# NextRunTime is the scheduler's own answer to "when will this actually run",
# so an empty value is exactly the symptom that was missed. Treat it as fatal.
#
# Fatal means `exit 1`, not red text (fixed 2026-08-27). Both FAILED branches
# used to print and then fall through to the help footer below, ending the
# script at exit 0 — so a wrapper, a CI step, or any caller reading the exit
# code saw success, under a green "Registered scheduled task" line, while the
# recorder had no periodic recovery at all. Colour is not a return value.
$recorderInfo = Get-ScheduledTaskInfo -TaskName $RecorderTaskName
$recorderRep  = (Get-ScheduledTask -TaskName $RecorderTaskName).Triggers |
    Where-Object { $_.Repetition -and $_.Repetition.Interval } |
    Select-Object -First 1

if (-not $recorderRep) {
    Write-Host "[$RecorderTaskName] FAILED: no repeating trigger registered — the recorder has NO periodic recovery, only an at-logon start." -ForegroundColor Red
    exit 1
} elseif (-not $recorderInfo.NextRunTime) {
    Write-Host "[$RecorderTaskName] FAILED: repetition is present in the task XML but NOT ARMED (NextRunTime is empty) — nothing will fire. This is the 2026-08-27 failure; see the Task 4 trigger comments." -ForegroundColor Red
    exit 1
} else {
    $durLabel = if ($recorderRep.Repetition.Duration) { $recorderRep.Repetition.Duration } else { 'indefinite' }
    Write-Host "[$RecorderTaskName] periodic recovery VERIFIED: interval=$($recorderRep.Repetition.Interval), duration=$durLabel, next run=$($recorderInfo.NextRunTime)" -ForegroundColor Green
}

Write-Host ''
Write-Host 'To run any of them immediately:'
Write-Host "  Start-ScheduledTask -TaskName $NightlyTaskName"
Write-Host "  Start-ScheduledTask -TaskName $WeeklyTaskName"
Write-Host "  Start-ScheduledTask -TaskName $FreshnessTaskName"
Write-Host "  Start-ScheduledTask -TaskName $RecorderTaskName"
Write-Host ''
Write-Host 'To remove them:'
Write-Host "  Unregister-ScheduledTask -TaskName $NightlyTaskName -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName $WeeklyTaskName -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName $FreshnessTaskName -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName $RecorderTaskName -Confirm:`$false   # NOTE: this leaves whispr with no start path at all"
