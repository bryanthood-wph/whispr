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
      preempted the script's own error handling). Also fires when there is no
      log at all but Task Scheduler shows a run today that exited non-zero —
      a kill before the first log line.

  C — the recorder's periodic recovery is not armed (runs FIRST; see the block
      comment above it for why the ordering is load-bearing).
  D — a file is parked in transcripts\_needs-attention\ awaiting a human, or
      today's run FAILED to park one (watermark frozen, expensive re-ingest
      loop live). Both are invisible to Check B, because the run continues and
      still logs SUCCESS. Runs LAST.
  E — call audio older than retention.audio_max_age_days remains. This check
      also runs the daily purge (`whispr purge-audio`). Runs just before D.

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
    # "No log" used to be read as "nothing started", but a run can be killed before
    # its first line (2026-10-02: woke at 05:00:17, back in standby at 05:00:29,
    # exit 0xC000013A, no log, and this check said OK). Task Scheduler's own record
    # tells the two apart. 0x41301 = still running.
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    $ranToday = $taskInfo.LastRunTime -and $taskInfo.LastRunTime.Date -eq (Get-Date).Date
    if ($ranToday -and $taskInfo.LastTaskResult -notin 0, 0x41301) {
        $detail = "the task started today at $($taskInfo.LastRunTime) and exited 0x{0:X} without writing $todayLog — killed before its first log line (standby, shutdown, or an external kill). Its transcripts wait for the next run." -f $taskInfo.LastTaskResult
        Invoke-CheckFailure -Check 'B' -StepName 'watchdog-killed-before-log' -Detail $detail -FileContext $TaskName
    } else {
        Write-Log -Message "Check B OK: no nightly-ingest log for today and no run today per Task Scheduler (day-of-week/laptop-off deferral is normal, see .SYNOPSIS)."
    }
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

# -- Check E: no call audio outlives retention.audio_max_age_days -------------
# Added 2026-10-04 (plan F.1, lesson L14). This check is also the purge's daily
# runner, rather than a fifth scheduled task: `whispr purge-audio` deletes overdue
# WAVs whose call has a transcript and exits 1 while anything overdue remains —
# an untranscribed call (never deleted automatically), a failed delete, or, under
# -DryRun (--check, deletes nothing), audio the purge would remove.
#
# Runs BEFORE Check D: colleagues' voices outliving the limit never self-heals
# and is the privacy-relevant condition; a parked file only delays one ingest.
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$purgeArgs = @('-m', 'whispr', 'purge-audio') + $(if ($DryRun) { @('--check') } else { @() })
$purgeOut = (& $python @purgeArgs 2>&1 | Out-String).Trim()
$purgeExit = $LASTEXITCODE
foreach ($line in ($purgeOut -split "`r?`n")) { if ($line) { Write-Log -Message "purge-audio: $line" } }
if ($purgeExit -eq 0) {
    Write-Log -Message "Check E OK: no audio older than retention.audio_max_age_days."
} else {
    $detail = "call audio is past retention.audio_max_age_days and was not deleted (purge-audio exit $purgeExit). Untranscribed calls are never deleted automatically — transcribe or delete each by hand. $($purgeOut -replace "`r?`n", ' | ')"
    Invoke-CheckFailure -Check 'E' -StepName 'audio-retention-overdue' -Detail $detail -FileContext (Join-Path $RepoRoot 'recordings')
}

# -- Check D: last night's run parked a file it could not resolve ------------
# Added 2026-09-23. Checks B and C both assume a broken pipeline announces itself
# by stopping. A parked file does not: nightly-ingest.ps1 reports it via
# Invoke-JobFailure -NonFatal and then carries on, so the run still logs
# "nightly-ingest SUCCESS" and Check B — which greps the tail for exactly that —
# reports OK. That is bit-for-bit the 2026-09-09..09-16 blind window, where eight
# consecutive "SUCCESS" runs made zero forward progress.
#
# The gap being closed is DELIVERY, not detection. The nightly job alerts at
# 01:00 into logs\, which nobody reads (there were 10 unread FAILED-*.txt files
# sitting there when this was written). This check runs twice daily at hours when
# a human is around, so it is the right place to surface the sentinel.
#
# Deliberately NOT a watermark-age check, and NOT a scan for today's
# NEEDS-ATTENTION-<date> sentinel either. The .SYNOPSIS forbids time heuristics
# and is right to — the watermark can legitimately sit days stale with the laptop
# off. A date-stamped sentinel glob looked state-based but smuggled the same
# assumption back in: under the schedule register-task.ps1 registers (ingest
# 22:00, this check 09:00 and 20:00) the park always lands AFTER both of that
# day's checks, and tomorrow's runs build a different date string, so it could
# never fire at all. Both conditions below read state that the condition itself
# maintains, so no schedule can hide them.
#
# The two predicates are scoped differently ON PURPOSE:
#   - A SUCCESSFUL park happens once, and the file then sits there indefinitely,
#     so this must be persistent: it nags every run until a human deals with the
#     file. That matches Check C, which also nags until fixed.
#   - A FAILED park regenerates its own evidence every single run (the watermark
#     stays frozen, the same file is retried, the same line is logged), so
#     scoping it to today's log is self-re-detecting and cannot go quiet while
#     the condition lasts.
# $todayLog is the path Check B already computed — not recomputed here.
#
# RUNS LAST, which inverts this file's "order by how badly the condition
# self-heals" rule, deliberately. Invoke-JobFailure ends in `exit 1`, so any
# firing check masks the ones below it — and a parked file is the only condition
# here where the pipeline is otherwise healthy and still making progress. Masking
# it for one 12-hour cycle costs nothing; masking C, A or B costs a dead pipeline.
$problems = @()

$parked = @(Get-ChildItem -LiteralPath $NeedsAttentionDir -File -ErrorAction SilentlyContinue)
if ($parked.Count -gt 0) {
    $names = ($parked | Select-Object -First 3 | ForEach-Object { $_.Name }) -join ', '
    $problems += "$($parked.Count) file(s) sit parked in '$NeedsAttentionDir' awaiting a human ($names). nightly-ingest skipped each one and will never ingest it until it is fixed and its LastWriteTimeUtc is bumped."
}

# The LATEST park attempt, not merely any. A daily log accumulates every run of
# the day, so "does this file contain a failure" would keep alarming after a
# later run had already parked the file successfully — a stale alarm that cannot
# clear until midnight. Whichever line came last is the current state, and a
# retry that succeeds silences it immediately.
if (Test-Path -LiteralPath $todayLog) {
    $parkAttempts = @(Select-String -LiteralPath $todayLog -Pattern 'Could NOT park |PARKED ' -ErrorAction SilentlyContinue)
    if ($parkAttempts.Count -gt 0 -and $parkAttempts[-1].Line -match 'Could NOT park ') {
        $problems += "the most recent park attempt today FAILED, so the watermark is FROZEN — every run from now on re-lists the whole backlog behind that file until it is moved or fixed by hand (the ingest ledger skips what is already ingested, so the re-listing no longer costs claude calls, but the frozen file itself is never ingested). This is the 2026-09-09 failure mode; see $todayLog."
    }
}

if ($problems.Count -eq 0) {
    Write-Log -Message "Check D OK: nothing parked in '$NeedsAttentionDir', and today's run parked everything it needed to."
} else {
    Invoke-CheckFailure -Check 'D' -StepName 'nightly-needs-attention' -Detail ($problems -join ' ALSO: ') -FileContext $NeedsAttentionDir
}

Write-Log -Message "=== check-nightly-freshness done ==="
