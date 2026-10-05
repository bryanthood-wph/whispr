<#
.SYNOPSIS
  Ad hoc, single-file transcript summary — same summarize step as
  nightly-ingest.ps1, but fully independent of it.

.DESCRIPTION
  Takes one whispr transcript file, runs it through the same haiku
  summarize prompt nightly-ingest.ps1 uses, and writes the result to the
  user's Downloads folder. Does NOT touch the vault (cohoodOBS), does NOT
  run /ingest, does NOT read or advance nightly's watermark file, and never
  modifies the source transcript. This is a quick-use, interactive-only
  tool — no -DryRun flag, no batch mode, one file per invocation.

  Deliberately duplicates Get-FrontmatterAttendees / Get-FrontmatterOrDefault
  from nightly-ingest.ps1 (rather than sharing them via sync-common.ps1) so a
  future change to one script's frontmatter handling can't silently change
  the other's behavior.

.USAGE
  pwsh -NoProfile -File scripts\summarize-transcript.ps1 -File <path>

.PARAMETERS
  -File             Path to a whispr transcripts\*.md file (relative or absolute).
  -TimeoutSeconds   Hard external per-claude-call timeout (process is killed on expiry).
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [string]$File,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'

$RepoRoot           = Split-Path -Parent $PSScriptRoot   # C:\github\whispr
$PromptTemplatePath = Join-Path $PSScriptRoot 'summarize-prompt.txt'
$DownloadsDir       = Join-Path $env:USERPROFILE 'Downloads'

# Invoke-ClaudeStep (in sync-common.ps1) reads $DryRun from caller scope. This
# tool has no -DryRun flag but must still define the variable explicitly.
$DryRun = $false

# Shared, already-multi-consumer plumbing only (claude invocation, generic
# frontmatter parsing, logging). Does NOT pull in nightly-ingest.ps1's
# watermark/vault logic — this script never dot-sources that file.
. (Join-Path $PSScriptRoot 'sync-common.ps1')

$script:DailyLogPath = Join-Path $env:TEMP 'whispr-summarize-transcript.log'
if (-not (Test-Path -LiteralPath $script:DailyLogPath)) {
    [System.IO.File]::WriteAllText($script:DailyLogPath, '', $script:Utf8NoBom)
}

# ---------------------------------------------------------------------------
# Local copies of nightly-ingest.ps1's two nightly-only frontmatter
# conveniences — duplicated on purpose, not shared, per explicit decision:
# this script must not break (or be broken by) changes to nightly-ingest.ps1.
# ---------------------------------------------------------------------------
function Get-FrontmatterAttendees {
    param([Parameter(Mandatory)][string]$Frontmatter)
    $names = Get-FrontmatterListArray -Frontmatter $Frontmatter -Key 'attendees'
    if ($names.Count -eq 0) { return $null }
    return ($names -join '; ')
}

function Get-FrontmatterOrDefault {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return '(none)' }
    return $Value
}

# ---------------------------------------------------------------------------
# Resolve and validate the input file (fail fast, no silent fallback).
# ---------------------------------------------------------------------------
try {
    $fullPath = (Resolve-Path -LiteralPath $File -ErrorAction Stop).Path
} catch {
    Write-Log -Level ERROR -Message "File not found: '$File'"
    Write-Error "Transcript file not found: '$File'."
    exit 1
}

$fileInfo = Get-Item -LiteralPath $fullPath
if ($fileInfo.Extension -ne '.md' -or $fileInfo.Directory.Name -ne 'transcripts') {
    Write-Log -Level ERROR -Message "Not a transcripts\*.md-shaped file: '$fullPath'"
    Write-Error "Expected a whispr transcripts\*.md file, got: '$fullPath'."
    exit 1
}

if (-not (Test-Path -LiteralPath $PromptTemplatePath)) {
    Write-Error "Summarize prompt template not found at '$PromptTemplatePath' — is whispr present at '$RepoRoot'?"
    exit 1
}
$promptTemplate = [System.IO.File]::ReadAllText($PromptTemplatePath)
$raw = [System.IO.File]::ReadAllText($fullPath)

# (a) Split frontmatter/body.
$parsed = Split-Frontmatter -Content $raw
if (-not $parsed.Valid) {
    Write-Log -Level ERROR -Message "Malformed/missing frontmatter in '$fullPath'"
    Write-Error "'$($fileInfo.Name)' has no valid '---'-delimited frontmatter block — refusing to proceed."
    exit 1
}
# Same egress rule as nightly: the prompt goes to claude, so the join
# link/passcode keys are dropped before anything reads the frontmatter.
$frontmatter = Remove-FrontmatterKeys -Frontmatter $parsed.Frontmatter -Keys $EgressExcludedFrontmatterKeys
$body = $parsed.Body

# (b) partial: flag — hard stop (this is a single-file tool, no batch to
# skip-and-continue within).
$partialRaw = Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'partial'
$isPartial = ($null -ne $partialRaw) -and ($partialRaw.Trim().ToLowerInvariant() -eq 'true')
if ($isPartial) {
    Write-Log -Level ERROR -Message "'$($fileInfo.Name)' is flagged partial: true"
    Write-Error "'$($fileInfo.Name)' is flagged partial: true (crash-truncated capture) — not summarizing."
    exit 1
}

# (c) Frontmatter scalars — same fields/defaults as nightly-ingest.ps1's per-file loop.
$callTitle   = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'call_title')
$date        = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'date')
$callType    = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'call_type')
$organizer   = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'organizer')
$attendees   = Get-FrontmatterOrDefault (Get-FrontmatterAttendees -Frontmatter $frontmatter)
$inviteNotes = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'invite_notes')

# (d) Token-substitute the same shared prompt template nightly-ingest.ps1 uses.
$summarizePrompt = $promptTemplate.
    Replace('{{CALL_TITLE}}', $callTitle).
    Replace('{{DATE}}', $date).
    Replace('{{CALL_TYPE}}', $callType).
    Replace('{{ORGANIZER}}', $organizer).
    Replace('{{ATTENDEES}}', $attendees).
    Replace('{{INVITE_NOTES}}', $inviteNotes).
    Replace('{{TRANSCRIPT_BODY}}', $body)

# (e) Spawn the real haiku subprocess. WorkingDirectory is pinned to $RepoRoot
# explicitly — required (not optional) because this script can be invoked via
# a user-level skill from any cwd, and claude's CLAUDE.md/.claude auto-
# discovery is keyed off the subprocess's working directory.
Write-Log -Level INFO -Message "Summarizing '$($fileInfo.Name)'..."
$summarizeResult = Invoke-ClaudeStep -StepName "summarize-transcript:$($fileInfo.Name)" `
    -PromptText $summarizePrompt `
    -ExtraArgs @('--model', 'haiku', '--permission-mode', 'dontAsk') `
    -WorkingDirectory $RepoRoot -TimeoutSeconds $TimeoutSeconds

if (-not $summarizeResult.Success) {
    $why = if ($summarizeResult.TimedOut) { "timed out after ${TimeoutSeconds}s" } else { 'failed or returned an empty result' }
    Write-Log -Level ERROR -Message "Claude summarize step $why for '$($fileInfo.Name)'. ExitCode=$($summarizeResult.ExitCode)"
    Write-Error "Claude summarization $why for '$($fileInfo.Name)'.`nExit code: $($summarizeResult.ExitCode)`nStderr: $($summarizeResult.StdErr)"
    exit 1
}

# (f) Assemble deterministically — same recipe as nightly's step (f) — and
# write to Downloads under the same basename as the source transcript.
$outputContent = "---`n$frontmatter`n---`n`n$($summarizeResult.Result.Trim())`n"

if (-not (Test-Path -LiteralPath $DownloadsDir)) {
    Write-Error "Downloads folder not found at '$DownloadsDir'."
    exit 1
}
$targetPath = Join-Path $DownloadsDir $fileInfo.Name
if (Test-Path -LiteralPath $targetPath) {
    Write-Log -Level WARN -Message "Overwriting existing file at '$targetPath'."
}
try {
    Write-Utf8File -Path $targetPath -Content $outputContent
} catch {
    Write-Error "Failed writing summary to '$targetPath': $($_.Exception.Message)"
    exit 1
}

Write-Log -Level INFO -Message "SUCCESS — wrote '$targetPath' (cost `$$($summarizeResult.CostUsd))."
Write-Output $targetPath
exit 0
