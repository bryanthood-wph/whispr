# One-shot P2b run (python -m eval graph-first, eval/graph_first.py): both arms, 20 live
# sessions. Runs only under Task Scheduler, so the claude CLI gets a clean environment
# (no CLAUDECODE / ANTHROPIC_BASE_URL from a parent Claude session). The task is
# registered by hand, once, with the runbook in docs/plan/task-intake-and-worker.md §7
# (P2b), run hidden by pwsh -WindowStyle Hidden, and started with
# Start-ScheduledTask whispr-eval-graph-first.
#
# -Repo is the checkout to run (the rebuild worktree, with P2b merged: the live plugin,
# CLAUDE_CODE_PLUGIN_DIRS in your settings, and the plugin's whispr_root option all name
# it). -Python is whispr's interpreter. -Log is appended to.
#
# While it runs it asks Windows not to idle-sleep (SetThreadExecutionState, released when
# the process exits), as the dev eval's runner did: a laptop that slept mid-call hung the
# call (2026-10-05). Closing the lid still sleeps.
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$Python,
  [Parameter(Mandatory = $true)][string]$Log
)
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
Add-Type -Namespace Whispr -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
[void][Whispr.Power]::SetThreadExecutionState([uint32]'0x80000001')   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
Set-Location -LiteralPath $Repo
$env:PYTHONUTF8 = '1'                 # model text is not ASCII: never let an encoding stop the run
"=== start $(Get-Date -Format o) HEAD $(git rev-parse --short HEAD)" | Out-File -LiteralPath $Log -Append -Encoding utf8
& $Python -m eval graph-first 2>&1 | Out-File -LiteralPath $Log -Append -Encoding utf8
$code = $LASTEXITCODE
"=== exit $code (0 PASS, 1 FAIL, 2 refused or stopped) $(Get-Date -Format o)" | Out-File -LiteralPath $Log -Append -Encoding utf8
[void][Whispr.Power]::SetThreadExecutionState([uint32]'0x80000000')   # ES_CONTINUOUS: release
exit $code
