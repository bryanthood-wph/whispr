<#
.SYNOPSIS
  Shared run-status primitive for the nightly-drafting pipeline jobs
  (nightly-triage.ps1 / nightly-draft.ps1, built in later phases).

.DESCRIPTION
  sync-common.ps1's Invoke-JobFailure already writes a durable trio (sentinel
  .txt + Windows Event Log + toast) but ONLY on failure. The nightly-drafting
  design (_design-nightly-drafting.md §9/§18 R5) additionally needs a
  machine-readable status for EVERY run — success included — so:
    - the morning Brief stage can open logs/run-status-<date>.json and lead
      with "run did not complete" if it's missing/stale/still 'started',
      instead of just going silent;
    - the NEXT scheduled run can see the prior run never reached 'completed'
      and know to catch up rather than assume yesterday was clean.

  This is deliberately separate from sync-common.ps1 (owned by the existing
  whispr<->cohoodOBS ingest/lint pipeline; this file is additive, not a
  modification of that module) but dot-sources it for Write-Log/Utf8NoBom so
  logging stays in one convention across both pipelines.

.USAGE
  . (Join-Path $PSScriptRoot 'sync-common.ps1')       # must be dot-sourced FIRST
  . (Join-Path $PSScriptRoot 'draft-run-status.ps1')
  $status = Start-DraftRun -JobName 'nightly-triage' -LogDir $LogDir
  try {
      ... do the run ...
      Complete-DraftRun -Status $status
  } catch {
      Complete-DraftRun -Status $status -Failed -FailureReason @{ category = 'logic'; message = $_.Exception.Message }
      throw
  }
#>

function Get-RunStatusPath {
    # JobName is part of the filename so that two jobs sharing a local date
    # (e.g. the 20:30 triage and the 22:15 late-sweep) do NOT overwrite each
    # other's terminal status — a code-review finding (2026-07-16). A sanitized
    # JobName keeps the filename filesystem-safe.
    param(
        [Parameter(Mandatory)][string]$LogDir,
        [Parameter(Mandatory)][string]$JobName,
        [string]$Date = (Get-Date -Format 'yyyy-MM-dd')
    )
    $safeJob = ($JobName -replace '[^A-Za-z0-9._-]', '_')
    return Join-Path $LogDir "run-status-$safeJob-$Date.json"
}

function Start-DraftRun {
    # Begins a run-status record. Call once at the very top of the job, before
    # any work that could throw, so a crash after this point is still visible.
    param(
        [Parameter(Mandatory)][string]$JobName,
        [Parameter(Mandatory)][string]$LogDir
    )
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    $path = Get-RunStatusPath -LogDir $LogDir -JobName $JobName
    $runId = [guid]::NewGuid().ToString('N').Substring(0, 12)
    $record = [PSCustomObject]@{
        jobName       = $JobName
        runId         = $runId
        status        = 'started'
        startedAt     = (Get-Date).ToUniversalTime().ToString('o')
        completedAt   = $null
        isError       = $false
        failureReason = $null
    }
    try {
        Write-Utf8File -Path $path -Content ($record | ConvertTo-Json -Depth 5)
    } catch {
        # Never let status-writing itself take down the job; log and continue.
        Write-Log -Level WARN -Message "Could not write run-status start record: $($_.Exception.Message)"
    }
    return [PSCustomObject]@{ Path = $path; JobName = $JobName; RunId = $runId }
}

function Complete-DraftRun {
    # Call exactly once at the end of the job — in the success path AND in the
    # catch block (mirrors sync-common.ps1's try/finally-around-Invoke-JobFailure
    # pattern). Overwrites the record Start-DraftRun created with a terminal state.
    param(
        [Parameter(Mandatory)][PSCustomObject]$Status,
        [switch]$Failed,
        [hashtable]$FailureReason
    )
    $record = [PSCustomObject]@{
        jobName       = $Status.JobName
        runId         = $Status.RunId
        status        = if ($Failed) { 'failed' } else { 'completed' }
        startedAt     = $null
        completedAt   = (Get-Date).ToUniversalTime().ToString('o')
        isError       = [bool]$Failed
        failureReason = if ($Failed -and $FailureReason) {
            [PSCustomObject]@{ category = $FailureReason.category; message = $FailureReason.message }
        } else { $null }
    }
    # Preserve the original startedAt from disk if present, rather than losing it.
    try {
        if (Test-Path -LiteralPath $Status.Path) {
            $prior = Get-Content -LiteralPath $Status.Path -Raw | ConvertFrom-Json
            if ($prior.startedAt) { $record.startedAt = $prior.startedAt }
        }
    } catch {}

    try {
        Write-Utf8File -Path $Status.Path -Content ($record | ConvertTo-Json -Depth 5)
    } catch {
        Write-Log -Level WARN -Message "Could not write run-status completion record: $($_.Exception.Message)"
    }
}

function Test-DraftRunHealthy {
    # Read-only check for a consumer (Brief stage, or the next scheduled run)
    # to ask "did last night's run actually finish?" Returns a simple verdict
    # object rather than throwing, so a missing/corrupt file is reportable,
    # not a crash.
    param(
        [Parameter(Mandatory)][string]$LogDir,
        [Parameter(Mandatory)][string]$JobName,
        [string]$Date = (Get-Date -Format 'yyyy-MM-dd')
    )
    $path = Get-RunStatusPath -LogDir $LogDir -JobName $JobName -Date $Date
    if (-not (Test-Path -LiteralPath $path)) {
        return [PSCustomObject]@{ Healthy = $false; Reason = "no run-status file for job '$JobName' on $Date at '$path'"; Record = $null }
    }
    try {
        $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    } catch {
        return [PSCustomObject]@{ Healthy = $false; Reason = "run-status file unreadable/corrupt: $($_.Exception.Message)"; Record = $null }
    }
    if ($JobName -and $record.jobName -ne $JobName) {
        return [PSCustomObject]@{ Healthy = $false; Reason = "run-status file is for job '$($record.jobName)', expected '$JobName'"; Record = $record }
    }
    if ($record.status -ne 'completed') {
        return [PSCustomObject]@{ Healthy = $false; Reason = "run status is '$($record.status)', not 'completed'"; Record = $record }
    }
    return [PSCustomObject]@{ Healthy = $true; Reason = 'ok'; Record = $record }
}
