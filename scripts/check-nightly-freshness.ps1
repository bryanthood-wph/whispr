<#
.SYNOPSIS
  Watchdog for whispr-nightly-ingest: catches the two failure modes that leave
  the job silently broken with no trace of its own (2026-07-24 incident — a
  catch-up run was killed externally mid-flight, STATUS_CONTROL_C_EXIT, before
  its own try/finally ever got a chance to write a FAILED sentinel).

  Deliberately state-based, not time-based — no quiet-period or day-of-week
  threshold anywhere below. This week alone proved the watermark can
  legitimately sit multiple days stale (a laptop off Mon+Tue caught up
  together Wed morning) — a time/day heuristic generous enough to tolerate
  that would be too slow to catch a genuine same-day break. Task Scheduler's
  own State already answers "is it still running?" with no guessing needed.

.CHECKS
  A — task disabled or deleted (would never self-heal, unlike a laptop being
      off, which resolves itself the next time it's on).
  B — today's nightly-ingest-<date>.log exists, the task is not currently
      Running, and its last line reports neither success nor failure — i.e.
      a run started and never reached a terminal state. This is today's
      exact signature (external kill, no FAILED sentinel because the kill
      preempted the script's own error handling).

  A run that already self-reported failure (logs "FAIL THE JOB..." via
  Invoke-JobFailure, matched case-insensitively on "fail") is NOT re-flagged
  here — it already alerted through the existing durable trio; re-alerting
  would just be a confusing duplicate for the same one failure.

.USAGE
  pwsh -NoProfile -File scripts\check-nightly-freshness.ps1            # real check
  pwsh -NoProfile -File scripts\check-nightly-freshness.ps1 -DryRun    # report only

.NOTES
  Reuses whispr-nightly-ingest's own EventLogSource/EventLogName/EventId/JobName
  (below) rather than inventing a separate alert identity — this script exists
  to report on THAT job's health, so it should sound like that job, not a
  third distinct source. Reuses Invoke-JobFailure verbatim from sync-common.ps1
  — same sentinel + Event Log + toast trio, no new alerting mechanism.
#>

[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepoRoot   = Split-Path -Parent $PSScriptRoot                     # C:\github\whispr
$LogDir     = Join-Path $RepoRoot 'logs'
$TaskName   = 'whispr-nightly-ingest'

$EventLogSource = 'whispr-nightly'
$EventLogName   = 'Application'
$EventId        = 1001
$JobName        = 'whispr-nightly-ingest'

$script:DailyLogPath = Join-Path $LogDir ('check-nightly-freshness-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
if (-not (Test-Path -LiteralPath $script:DailyLogPath)) { New-Item -ItemType File -Path $script:DailyLogPath -Force | Out-Null }

. (Join-Path $PSScriptRoot 'sync-common.ps1')

function Invoke-CheckFailure {
    <#
      The one dispatch point for all three checks: log the failure, then either
      report what WOULD happen (-DryRun) or hand off to Invoke-JobFailure.

      Extracted 2026-08-27. These five lines had been copy-pasted once per check,
      so any change to how a dry run reports had to land correctly in three
      places — and the copies had already drifted. Local rather than added to
      sync-common.ps1 because -DryRun is this script's own parameter; the two
      nightly jobs that share that module have no use for it. Reading $DryRun
      from the script scope matches how Write-Log already reads $DailyLogPath.
    #>
    param(
        [Parameter(Mandatory)][string]$Check,
        [Parameter(Mandatory)][string]$StepName,
        [Parameter(Mandatory)][string]$Detail,
        [string]$FileContext = '(n/a)'   # same default as Invoke-JobFailure
    )
    Write-Log -Level ERROR -Message "Check $Check failed: $Detail"
    if ($script:DryRun) {
        Write-Log -Message "[DryRun] would call Invoke-JobFailure for: $Detail"
        return
    }
    Invoke-JobFailure -StepName $StepName -Detail $Detail -FileContext $FileContext
}

Write-Log -Message "=== check-nightly-freshness starting (DryRun=$DryRun) ==="

# -- Check C: the recorder's periodic recovery is armed -----------------------
# Added 2026-08-27. Until now this script watched whispr-nightly-ingest ONLY, so
# when the recorder died on 2026-08-23 it logged "Check A OK / Check B OK" every
# run for three days while whispr was dead. Monitoring the sync job but not the
# recorder that feeds it left the pipeline's first stage entirely unwatched.
#
# RUNS FIRST, ahead of Checks A and B, and that ordering is load-bearing (fixed
# 2026-08-27). Invoke-JobFailure ends in `exit 1`, so any check that fires
# terminates the process and every check below it is never evaluated. With this
# block last, a stuck nightly run masked the recorder entirely -- and the
# correlated case is the realistic one, since a single external kill can stop the
# recorder AND strand a nightly run in the same event. Ordered by how badly the
# condition self-heals: an unarmed recorder NEVER recovers on its own (three days,
# 2026-08-23), while a stuck nightly run gets a fresh attempt the next night.
#
# Deliberately asserts that RECOVERY IS ARMED, not that whispr is running right
# now. "whispr is not running" is a false alarm waiting to happen -- quitting from
# the tray is a legitimate thing to do, and the watchdog restarts it within one
# interval anyway. "nothing will ever restart whispr again" is the condition that
# actually cost three days, and it never self-heals.
#
# State-based, honouring this file's .SYNOPSIS: no quiet-period or day-of-week
# heuristic. NextRunTime is the scheduler's own answer to "when does this next
# run", and an empty value is exactly the symptom that was missed -- a repetition
# grafted onto a logon trigger sits inert, with the interval present in the task
# XML, until the next logon (see register-task.ps1's Task 4 comments).
$RecorderTaskName = 'whispr-recorder'
$recorderTask = Get-ScheduledTask -TaskName $RecorderTaskName -ErrorAction SilentlyContinue
# Read once and reuse for both the test and the message, as register-task.ps1's
# equivalent check does — NextRunTime is a live Task Scheduler query, not a field.
$recorderInfo = if ($recorderTask) { Get-ScheduledTaskInfo -TaskName $RecorderTaskName } else { $null }

if (-not $recorderTask) {
    $detail = "scheduled task '$RecorderTaskName' does not exist -- nothing will start or restart whispr. See register-task.ps1."
} elseif ($recorderTask.State -eq 'Disabled') {
    $detail = "scheduled task '$RecorderTaskName' exists but is Disabled -- nothing will restart whispr."
} elseif (-not $recorderInfo.NextRunTime) {
    $detail = "scheduled task '$RecorderTaskName' has no NextRunTime -- its repetition is not armed, so whispr has no periodic recovery. This is the 2026-08-27 failure mode; see register-task.ps1's Task 4 comments."
} else {
    $detail = $null
    Write-Log -Message "Check C OK: '$RecorderTaskName' is armed (next run $($recorderInfo.NextRunTime))."
}

if ($detail) {
    Invoke-CheckFailure -Check 'C' -StepName 'recorder-recovery-unarmed' -Detail $detail -FileContext $RecorderTaskName
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# -- Check A: task disabled or deleted ---------------------------------------
if (-not $task -or -not $task.Settings.Enabled) {
    $detail = if (-not $task) { "scheduled task '$TaskName' does not exist" } else { "scheduled task '$TaskName' exists but is disabled" }
    Invoke-CheckFailure -Check 'A' -StepName 'watchdog-task-registration' -Detail $detail
    Write-Log -Message "=== check-nightly-freshness done (Check A fired; Check B skipped — nothing to check without a task) ==="
    return
}
Write-Log -Message "Check A OK: task '$TaskName' exists and is enabled."

# -- Check B: today's run started but never reached a terminal state --------
$todayLog = Join-Path $LogDir ('nightly-ingest-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

if (-not (Test-Path -LiteralPath $todayLog)) {
    Write-Log -Message "Check B OK: no nightly-ingest log for today yet (nothing has started; not itself a failure — day-of-week/laptop-off deferral is normal, see .SYNOPSIS)."
} else {
    # Success and failure are NOT symmetric at "last line": a genuine failure
    # exits immediately inside Invoke-JobFailure (exit 1 right after logging
    # "FAIL THE JOB..."), so that IS the last line — but a genuine success
    # logs "=== nightly-ingest SUCCESS ===" and then one more informational
    # footer line before exit 0, so SUCCESS is second-to-last, not last.
    # Checking the last several lines for either exact anchor (rather than
    # just the final line) covers both shapes without hardcoding line counts
    # that would silently break again if the script's tail wording changes.
    $tailLines = Get-Content -LiteralPath $todayLog -Tail 5
    $reachedTerminalState = ($tailLines -join "`n") -match 'nightly-ingest SUCCESS|FAIL THE JOB'
    $isRunning = $task.State -eq 'Running'

    if ($isRunning) {
        Write-Log -Message "Check B OK: today's log exists and the task is currently Running (in progress, not stuck)."
    } elseif ($reachedTerminalState) {
        Write-Log -Message "Check B OK: today's log reached a terminal state (found in last 5 lines)."
    } else {
        $lastLine = $tailLines | Select-Object -Last 1
        $detail = "today's log ($todayLog) exists, the task is not Running, and no terminal marker (SUCCESS/FAIL) appears in its last 5 lines — the run started and never finished. Last line: $lastLine"
        Invoke-CheckFailure -Check 'B' -StepName 'watchdog-stuck-run' -Detail $detail -FileContext $todayLog
    }
}

Write-Log -Message "=== check-nightly-freshness done ==="
