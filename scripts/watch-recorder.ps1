<#
.SYNOPSIS
  Liveness watchdog for the whispr recorder: relaunches it if it is not running.

  2026-08-23 outage (diagnosed and fixed 2026-08-26) — whispr was terminated
  (task LastResult 0x40010004,
  DBG_TERMINATE_PROCESS, i.e. an external kill, NOT a Python fault: both
  sys.excepthook and threading.excepthook are installed and logs/incidents.jsonl
  recorded nothing) and stayed dead for three days. Nothing noticed, because:
    - the recorder task's only trigger was -AtLogOn and the machine neither
      rebooted nor logged off in that window, so it never re-fired;
    - RestartCount does not cover a process exiting on its own (see
      register-task.ps1's .NOTES, 2026-07-17 finding — re-confirmed by this
      incident: the task carried RestartCount=3 and still did not recover);
    - check-nightly-freshness.ps1 watches whispr-nightly-ingest only, and
      logged "Check A OK / Check B OK" throughout the outage.
  This script closes that gap: the recorder now has a start path that repeats.

.DETECTION
  Reads whispr's OWN single-instance mutex (_MUTEX_NAME in whispr/__main__.py)
  rather than scanning for a process name. That is the authoritative signal —
  it is the exact thing whispr uses to decide whether another instance already
  owns the machine, so it cannot disagree with whispr about whether whispr is
  running. A process scan can be fooled: `pythonw.exe -m whispr` matches BOTH
  the venv launcher stub and the real app (two PIDs, parent/child), so a stub
  left behind by a half-dead child would read as alive.

.NOTES
  Launches via Win32_Process::Create, deliberately NOT Start-Process. Task
  Scheduler terminates a task's whole process TREE (job object) when it force-
  stops a run. An ordinary child would therefore inherit the watchdog task's
  lifetime and die with it — producing exactly the 0x40010004 signature this
  script exists to recover from. Win32_Process::Create is serviced by the WMI
  provider host, so the new process is parented outside this task's job object
  and survives independently.

  A SUCCESSFUL relaunch is logged only, never notified — see the comment at the
  $revived branch. A FAILED relaunch is reported by log + toast, but NOT via
  Invoke-JobFailure. That helper writes a FAILED sentinel, an Event Log entry
  and then `exit 1` — the right shape for a nightly job that gets one attempt a
  day, the wrong shape here: this script retries on its own every repetition
  interval, so a transient failure is self-healing and a persistent one already
  re-notifies every cycle.

.USAGE
  pwsh -NoProfile -File scripts\watch-recorder.ps1            # check, relaunch if dead
  pwsh -NoProfile -File scripts\watch-recorder.ps1 -DryRun    # report only, never launch
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    # How long to wait for a relaunched whispr to take the mutex before calling
    # the restart failed. whispr's heavy import chain (faster-whisper /
    # ctranslate2) runs BEFORE main() reaches _acquire_single_instance, so the
    # mutex does not appear instantly — measured ~9 s on this machine, and
    # markedly slower on a cold boot competing with other startup work.
    [int]$ConfirmTimeoutSeconds = 60,
    [int]$ConfirmPollSeconds    = 2
)

$ErrorActionPreference = 'Stop'

$RepoRoot     = Split-Path -Parent $PSScriptRoot                   # C:\github\whispr
$LogDir       = Join-Path $RepoRoot 'logs'
$MutexName    = 'Global\whispr_single_instance'                    # whispr/__main__.py::_MUTEX_NAME
$RecorderExe  = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$RecorderArgs = '-m whispr'
$JobName      = 'whispr-recorder'

$script:DailyLogPath = Join-Path $LogDir ('watch-recorder-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
if (-not (Test-Path -LiteralPath $script:DailyLogPath)) {
    New-Item -ItemType File -Path $script:DailyLogPath -Force | Out-Null
}

. (Join-Path $PSScriptRoot 'sync-common.ps1')

function Test-WhisprAlive {
    <#
      $true if whispr holds its single-instance mutex.

      Deliberately kept local rather than added to sync-common.ps1: this is its
      only caller, and putting it there would hand nightly-ingest and
      weekly-lint-compile a function neither of them uses.

      An UNEXPECTED failure is reported as DEAD, on purpose. The two wrong
      answers are not symmetric: a false "dead" costs one redundant launch that
      whispr's own mutex kills within seconds, while a false "alive" turns this
      watchdog into a silent no-op — the precise failure this script exists to
      prevent. Erring toward a harmless extra launch is the safe direction, and
      the WARN below makes the condition greppable if it ever becomes chronic.
    #>
    try {
        $mutex = [System.Threading.Mutex]::OpenExisting($MutexName)
        $mutex.Dispose()
        return $true
    } catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } catch {
        Write-Log -Level WARN -Message "Mutex probe failed unexpectedly ($($_.Exception.GetType().Name): $($_.Exception.Message)); treating whispr as DEAD."
        return $false
    }
}

Write-Log -Message "=== watch-recorder starting (DryRun=$DryRun) ==="

if (Test-WhisprAlive) {
    Write-Log -Message "OK: whispr is running (holds '$MutexName')."
    Write-Log -Message '=== watch-recorder done ==='
    return
}

Write-Log -Level WARN -Message "whispr is NOT running (no holder of '$MutexName')."

if ($DryRun) {
    Write-Log -Message "DryRun: would launch '$RecorderExe $RecorderArgs' (workdir '$RepoRoot'). Nothing launched."
    Write-Log -Message '=== watch-recorder done ==='
    return
}

if (-not (Test-Path -LiteralPath $RecorderExe)) {
    # Not recoverable by retrying, so say so plainly rather than re-toasting the
    # same broken path every cycle with a message that implies a transient fault.
    $detail = "Recorder executable not found at '$RecorderExe' — cannot relaunch."
    Write-Log -Level ERROR -Message $detail
    Send-BestEffortToast -Title 'whispr NOT restarted' -Message $detail -NotifierId $JobName
    exit 1
}

$commandLine = '"{0}" {1}' -f $RecorderExe, $RecorderArgs
Write-Log -Message "Launching: $commandLine (workdir '$RepoRoot')"

# Guarded, because $ErrorActionPreference = 'Stop' (line 61) turns ANY CIM-layer
# failure into a TERMINATING error — which would kill the script right here, before
# the ERROR log line and the notification below ever run. The daily log would then
# end on "Launching: ..." with no error and no alert, reading to anyone grepping it
# as though the launch had succeeded. That is a silent failure: precisely the shape
# this entire script exists to eliminate, reintroduced inside the fix.
#
# Reachable whenever the WMI layer itself is unusable rather than merely refusing
# the request: Winmgmt stopped or disabled by policy, WMI repository corruption,
# "RPC server is unavailable" (0x800706BA), or an EDR agent blocking WmiPrvSE from
# spawning processes. None of those are exotic on a managed enterprise laptop, and
# this machine has already produced one unexplained external process kill.
#
# A non-zero ReturnValue is the OTHER failure mode — the call returns normally and
# does not throw — and is handled separately immediately below. Both paths must
# alert, or the watchdog can fail in silence exactly like the thing it watches.
try {
    $create = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = $commandLine
        CurrentDirectory = $RepoRoot
    }
} catch {
    $detail = "Win32_Process::Create threw ($($_.Exception.GetType().Name): $($_.Exception.Message)) — the WMI layer may be unavailable."
    Write-Log -Level ERROR -Message $detail
    Send-BestEffortToast -Title 'whispr restart FAILED' -Message $detail -NotifierId $JobName
    exit 1
}

if ($create.ReturnValue -ne 0) {
    $detail = "Win32_Process::Create refused the launch (ReturnValue=$($create.ReturnValue))."
    Write-Log -Level ERROR -Message $detail
    Send-BestEffortToast -Title 'whispr restart FAILED' -Message $detail -NotifierId $JobName
    exit 1
}

# Bind to the process OBJECT now, while the PID is unambiguously ours. The reap
# below can run up to $ConfirmTimeoutSeconds later, and Windows recycles PIDs:
# `Stop-Process -Id` re-resolves the number at kill time, so after a 60 s wait it
# can terminate whatever inherited it. Verified 2026-08-27 on a PID whose process
# had exited: `-Id` threw (it had gone back to the pool and was being looked up
# afresh), while piping the captured object killed without any lookup at all.
#
# Touching .Handle is load-bearing, not defensive noise: Process.Kill() opens its
# native handle LAZILY BY PID if one is not already held, which would reintroduce
# the same lookup at the same late moment. Forcing it open here both pins the
# identity and keeps Windows from reissuing the PID while we hold it.
#
# SilentlyContinue because a process that died during import is the expected
# case, not an error — the reap guards for $null.
$launched = Get-Process -Id $create.ProcessId -ErrorAction SilentlyContinue
if ($launched) { $null = $launched.Handle }

Write-Log -Message "Launched PID $($create.ProcessId); waiting up to $ConfirmTimeoutSeconds s for it to take the mutex."

# Confirm the process actually came UP, don't just report that it was spawned.
# whispr can start and still die during import; without this the toast below
# would claim a recovery that never happened.
$deadline = (Get-Date).AddSeconds($ConfirmTimeoutSeconds)
$revived  = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $ConfirmPollSeconds
    if (Test-WhisprAlive) { $revived = $true; break }
}

if ($revived) {
    # Log only, no notification: a successful self-heal is this script working as
    # designed, and Send-BestEffortToast resolves to a MODAL msg.exe dialog under
    # pwsh 7 (WinRT toast types are unavailable there — verified 2026-08-26), so
    # notifying on success would interrupt with a dialog to dismiss every time
    # whispr is revived. Failures below still notify; those are worth the
    # interruption. Grep this log for 'Restarted whispr successfully' to see how
    # often the recorder is actually dying.
    Write-Log -Message "Restarted whispr successfully (PID $($create.ProcessId))."
    Write-Log -Message '=== watch-recorder done ==='
    return
}

$detail = "Launched PID $($create.ProcessId) but it never took the mutex within $ConfirmTimeoutSeconds s — whispr is still down."
Write-Log -Level ERROR -Message $detail

# Reap the launch that never came up. A whispr that hangs BEFORE it reaches
# _acquire_single_instance holds no mutex, so the next cycle sees "not running"
# and launches another one — and with a 15-minute repetition that accumulates a
# live pythonw.exe every cycle, indefinitely, until someone notices the machine
# is full of them. Killing the one we just launched keeps this script's own
# retries bounded.
#
# Best-effort by design, and two limits are worth stating plainly rather than
# being discovered later: the common case is that the process already exited on
# its own (hence the catch), and this stops the process we launched — the venv
# launcher stub — which does not guarantee a hung grandchild goes with it.
#
# Stops $launched, the handle captured at launch, NOT a bare -Id (fixed
# 2026-08-27): by this point up to $ConfirmTimeoutSeconds have passed and the PID
# may belong to something else entirely.
if (-not $launched) {
    Write-Log -Message "Nothing to reap — PID $($create.ProcessId) exited before it could be bound."
} else {
    try {
        $launched | Stop-Process -Force -ErrorAction Stop
        Write-Log -Message "Reaped unresponsive PID $($create.ProcessId)."
    } catch {
        Write-Log -Message "Nothing to reap — PID $($create.ProcessId) had already exited."
    }
}

Send-BestEffortToast -Title 'whispr restart FAILED' -Message $detail -NotifierId $JobName
exit 1
