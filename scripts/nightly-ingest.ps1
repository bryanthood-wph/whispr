<#
.SYNOPSIS
  Nightly job: summarize newly completed whispr call transcripts and ingest
  them into the cohoodOBS vault. This is the ingest-only half of the pipeline —
  the vault hygiene pass (/lint + /compile) is a separate, weekly job
  (see weekly-lint-compile.ps1) because a full-vault /lint measured at ~19
  minutes and ~$8, which is too expensive/slow to run every weeknight.

.EGRESS DECISION (read this before touching the claude invocations below)
  Sends meeting transcript summaries to the Claude API via Claude Code CLI,
  authenticated under the Deloitte enterprise plan (zero-retention/no-training).
  This is a deliberate exception to whispr's local-only posture (see
  SUMMARY_AGENT.md), accepted because cohoodOBS already operates on the
  enterprise-governed API. Decision date 2026-07-13.

.USAGE
  .venv or system pwsh:
    pwsh -NoProfile -File scripts\nightly-ingest.ps1                    # real run
    pwsh -NoProfile -File scripts\nightly-ingest.ps1 -DryRun            # safe rehearsal
    pwsh -NoProfile -File scripts\nightly-ingest.ps1 -QuietMinutes 15 -TimeoutSeconds 600

.PARAMETERS
  -DryRun          Do all detection/ordering/frontmatter-assembly/logging, but never
                    call claude, never write into the vault sources dir, and never
                    advance the watermark. Logs exactly what would happen instead.
  -QuietMinutes    Skip transcripts modified more recently than this many minutes ago
                    (guards against racing whispr while it is still writing a file).
  -TimeoutSeconds  Hard external per-claude-call timeout (process is killed on expiry).
  -WatermarkOverrideUtc
                   Rehearsal only — accepted only together with -DryRun. Replaces the
                    stored watermark for this run (e.g. 1970-01-01 to rehearse a lost
                    watermark file) without reading or writing the real one.

.NOTES
  CLI surface verified against the actual installed binary (claude.exe 2.1.205, via
  `claude --help`) before this script was written — see the authoring report for the
  full flag audit. Key confirmed facts baked into the code below:
    - `-p`/`--print`, `--output-format json` (fields used: result, total_cost_usd),
      `--model <alias>`, `--allowedTools <list>`, `--add-dir <dirs>` all exist as-is.
    - `--permission-mode` choices are: acceptEdits, auto, bypassPermissions, manual,
      dontAsk, plan. `dontAsk` is the most restrictive mode that is still fully
      non-interactive (auto-denies anything not explicitly allowed via
      --allowedTools, never prompts) — used everywhere below instead of
      bypassPermissions, and instead of `acceptEdits`/`auto`/`manual`/`plan` which can
      still block waiting on an interactive prompt (unacceptable for an unattended
      scheduled task with nobody at the keyboard).
    - `claude` resolves (via `Get-Command claude`) to an npm shim (claude.ps1 /
      claude.cmd) that wraps a real native binary at
      `<npm-dir>\node_modules\@anthropic-ai\claude-code\bin\claude.exe`. We invoke
      that native binary directly via System.Diagnostics.Process so we get a real
      external timeout+kill and can pipe an arbitrarily large prompt over stdin
      (avoiding the ~32K character Windows command-line length limit — some
      transcripts are already tens of KB). `claude -p` reading its prompt from stdin
      when no positional prompt argument is given is a documented, supported mode.
    - `.claude/`/`CLAUDE.md` auto-discovery is keyed off the process's current
      working directory, hence the explicit `WorkingDirectory` passed to every
      claude invocation below instead of relying on the script's own cwd.

  Shared claude-invocation/logging/failure-handling/frontmatter-parsing
  plumbing lives in sync-common.ps1 (dot-sourced below) so it is not
  duplicated with weekly-lint-compile.ps1.
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$QuietMinutes = 10,
    [int]$TimeoutSeconds = 300,
    [string]$WatermarkOverrideUtc
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants (paths given by the task spec; not to be re-derived or invented)
# ---------------------------------------------------------------------------
# $PSScriptRoot-relative resolution for anything actually nested under this repo;
# the vault is a *sibling* repo so its root is necessarily a literal constant.
$RepoRoot            = Split-Path -Parent $PSScriptRoot                     # C:\github\whispr
$TranscriptsDir       = Join-Path $RepoRoot 'transcripts'
# $NeedsAttentionDir (where Move-ToNeedsAttention parks an unresolvable file) is
# defined in sync-common.ps1, dot-sourced below — check-nightly-freshness.ps1
# needs the same path, so it has one definition rather than two.
$LogDir               = Join-Path $RepoRoot 'logs'
$PromptTemplatePath   = Join-Path $PSScriptRoot 'summarize-prompt.txt'
$WatermarkFile        = Join-Path $LogDir 'nightly-ingest-last-run.txt'
$LedgerFile           = Join-Path $LogDir 'nightly-ingest-ledger.jsonl'

$VaultRoot            = 'C:\github\cohoodOBS'                               # sibling repo — not under $RepoRoot
$VaultSourcesDir      = Join-Path $VaultRoot 'sources'

$EventLogSource        = 'whispr-nightly'
$EventLogName          = 'Application'
$EventId               = 1001   # arbitrary but stable id for this source's entries
$JobName               = 'whispr-nightly-ingest'   # used by the shared failure-toast label

# Shared helpers (claude invocation, logging, failure handling). Dot-sourcing
# runs in this script's own scope, so the constants above (and $DryRun) are
# already in scope for every function it defines.
. (Join-Path $PSScriptRoot 'sync-common.ps1')

# P2b: this job's claude calls load the whispr plugin through your settings, so its
# SessionStart hook would add the graph-first note to their context. Keep it off, so
# the job behaves as before (config/defaults.yaml graph_first_note.switch_env and
# off_value; tests/test_eval_graph_first.py checks the two agree). The child process
# inherits it from this process only.
$env:WHISPR_GRAPH_FIRST_NOTE = 'off'

# ---------------------------------------------------------------------------
# Watermark (premortem: only ever advance it AFTER a file's /ingest call has
# fully succeeded — see step (h) in the per-file loop below. A crash or claude
# failure anywhere before that point leaves the watermark at the last
# fully-good file, so the next run retries the failed one instead of silently
# losing it.)
# ---------------------------------------------------------------------------
# A file we can't safely resolve (malformed frontmatter or partial:true) is
# PARKED — moved out of the scanned directory by Move-ToNeedsAttention — and the
# watermark keeps advancing. Freezing is the FALLBACK, used only when the move
# itself fails.
#
# It used to be the other way round, and that cost 14 days (2026-09-09 -> 09-23).
# Freezing assumed a human would resolve the file promptly; nothing told the
# human. One pasted non-transcript froze the watermark at 2026-09-08 and every
# subsequent run re-summarized and re-ingested the same growing backlog — one
# file was processed 10 times, ~$14/night, $69.41 over the final five runs. Once
# the backlog exceeded the task's 1h ExecutionTimeLimit (from 09-17) the
# scheduler killed each run mid-flight. Worse, the runs from 09-09 to 09-16 still
# logged "nightly-ingest SUCCESS" while making zero forward progress, so
# check-nightly-freshness.ps1's Check B reported OK for eight days.
#
# Parking removes the file from discovery BY CONSTRUCTION, so the retry loop
# cannot recur. The invariant the freeze protected — an unresolved file must
# never be silently skipped — still holds, by different means: the file is
# physically moved somewhere a human can see it, named in the log, and reported
# through Invoke-JobFailure -NonFatal (durable sentinel + Event Log).
#
# The freeze survives for the one case parking cannot cover: if Move-Item throws
# (the file is locked by whoever put it there), an advancing watermark would
# skip the file PERMANENTLY. That is strictly worse than the stall this replaced,
# so the catch block freezes exactly as before — but now it also alerts.
$script:WatermarkFrozen = $false

function ConvertTo-UtcTimestamp {
    # One parser for every watermark value: the stored file (written as 'o', so
    # it carries its Z), the epoch fallback, and -WatermarkOverrideUtc. A value
    # with no offset is read as UTC, never as local time.
    param([Parameter(Mandatory)][string]$Text)
    return [datetime]::Parse($Text, [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
}

function Get-Watermark {
    if (Test-Path -LiteralPath $WatermarkFile) {
        $raw = ''
        try {
            $raw = [System.IO.File]::ReadAllText($WatermarkFile).Trim()
            return (ConvertTo-UtcTimestamp -Text $raw)
        } catch {
            Write-Log -Level WARN -Message "Watermark file unreadable/corrupt ('$raw'); treating as epoch (process everything). Error: $($_.Exception.Message)"
        }
    }
    return (ConvertTo-UtcTimestamp -Text '1970-01-01T00:00:00Z')
}

function Set-Watermark {
    param([Parameter(Mandatory)][datetime]$UtcTimestamp)
    if ($script:WatermarkFrozen) {
        Write-Log -Level WARN -Message "Watermark advance suppressed (frozen earlier this run by an unresolved file) — would otherwise have advanced to $($UtcTimestamp.ToUniversalTime().ToString('o'))."
        return
    }
    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Would advance watermark to $($UtcTimestamp.ToUniversalTime().ToString('o')) (not written)."
        return
    }
    Write-Utf8File -Path $WatermarkFile -Content $UtcTimestamp.ToUniversalTime().ToString('o')
}

function Move-ToNeedsAttention {
    <#
      Park a file this job cannot resolve, INSTEAD of freezing the watermark.
      See the $script:WatermarkFrozen comment above for why this replaced
      freezing as the primary path.

      Local rather than in sync-common.ps1 for the same reason
      check-nightly-freshness.ps1 keeps Invoke-CheckFailure local: it depends on
      $NeedsAttentionDir/$TranscriptsDir/$script:WatermarkFrozen, all
      nightly-only concepts. weekly-lint-compile.ps1 never walks transcripts.
    #>
    param(
        [Parameter(Mandatory)][System.IO.FileInfo]$File,
        [Parameter(Mandatory)][string]$Reason
    )

    # Mirrors Set-Watermark's DryRun arm: report, change nothing, freeze nothing.
    if ($DryRun) {
        Write-Log -Level WARN -Message "[DRYRUN] Would park '$($File.Name)' ($Reason) into '$NeedsAttentionDir'; watermark would NOT be frozen."
        return
    }

    # Stated everywhere this file is mentioned, because parking silently breaks
    # the obvious repair: a same-volume move preserves LastWriteTimeUtc, so once
    # the watermark has passed that mtime, moving the fixed file back is a no-op.
    # Do NOT "solve" that by scanning _needs-attention\ — that reintroduces the
    # unbounded retry through the back door.
    $fixHint = "To re-process after fixing: set its LastWriteTimeUtc to now " +
               "((Get-Item <path>).LastWriteTimeUtc = (Get-Date).ToUniversalTime()) then move it back to " +
               "'$TranscriptsDir'. Moving it back UNCHANGED does nothing — the watermark has passed its mtime."

    Write-Log -Level WARN -Message "Parking '$($File.Name)' ($Reason) -> '$NeedsAttentionDir' ..."

    # Only the MOVE is guarded. Reporting happens after the try on purpose: with
    # $ErrorActionPreference = 'Stop', an unrelated throw from Write-Log (its
    # AppendAllText is unguarded, so a transient sharing violation on the daily
    # log is enough) would otherwise land in the catch below and freeze the
    # watermark while alerting "Could NOT park" for a file that HAD been parked.
    # That single mis-attribution would restore the full-backlog re-ingest at
    # full claude cost — the exact failure this function exists to end.
    try {
        if (-not (Test-Path -LiteralPath $NeedsAttentionDir)) {
            New-Item -ItemType Directory -Path $NeedsAttentionDir -Force | Out-Null
        }
        # Never -Force: an already-parked copy of the same name is the artifact
        # we moved it here to preserve, so overwriting would destroy the
        # evidence. Suffix instead, matching whispr/output.py::_resolve_unique_path.
        # The Test-Path race is benign — single writer, once a night — and
        # Move-Item without -Force throws on a genuine collision rather than
        # silently overwriting.
        $dest = Join-Path $NeedsAttentionDir $File.Name
        $n = 2
        while (Test-Path -LiteralPath $dest) {
            $dest = Join-Path $NeedsAttentionDir ('{0}-{1}{2}' -f $File.BaseName, $n, $File.Extension)
            $n++
        }
        Move-Item -LiteralPath $File.FullName -Destination $dest
    } catch {
        # Could not park it (locked by an editor, ACL, AV handle). Fall back to
        # the old freeze so the file cannot be silently skipped. This is now the
        # ONLY remaining path to the 2026-09 stall, and unlike then it alerts.
        $script:WatermarkFrozen = $true
        Write-Log -Level ERROR -Message "Could NOT park '$($File.Name)' ($($_.Exception.Message)) — freezing the watermark instead, so it is retried rather than lost. Every later run re-lists the backlog behind it until this file is dealt with (the ingest ledger skips what is already ingested, so the re-listing costs no claude calls)."
        Invoke-JobFailure -NonFatal -StepName 'needs-attention-park-failed' -FileContext $File.Name `
            -Detail "Could not move '$($File.FullName)' to '$NeedsAttentionDir': $($_.Exception.Message). Watermark FROZEN as a fallback — every later run re-lists the backlog behind it until this file is moved or fixed by hand."
        return
    }

    # $dest is still in scope: try/catch does not create one in PowerShell, and
    # these lines are reachable only when the catch above did not return.
    Write-Log -Level WARN -Message "PARKED '$($File.Name)' -> '$dest' ($Reason). Watermark NOT frozen. $fixHint"
    Invoke-JobFailure -NonFatal -StepName 'needs-attention' -FileContext $File.Name `
        -Detail "Parked to '$dest' ($Reason). Later files in this run continue normally. $fixHint"
}

# ---------------------------------------------------------------------------
# Idempotency — the ingest ledger and the per-file gate (added 2026-09-30).
#
# The watermark decides which transcripts are LISTED; it was never able to say
# whether one had already been ingested. So anything that re-listed a
# transcript — the 2026-09-09..09-23 freeze, a hand-rewound watermark, an
# unreadable watermark file (epoch: every transcript) — re-summarized it and
# rewrote sources\<name>.md over the existing note. The transcript's own
# frontmatter always reads status raw / topic placeholder, so each rewrite reset
# an already-integrated note to raw with a new, differently-worded body — the
# vault's one hard rule (a note's body is immutable once it leaves raw). 155 of
# 278 /ingest calls in that window were repeats of files already ingested.
#
# Two layers, checked before any claude call:
#   1. The ledger (append-only JSONL, one line per ingested transcript: name +
#      sha256). A listed name already in the ledger is skipped at zero cost —
#      this is what makes a rewound or lost watermark harmless, and it still
#      remembers notes that were deliberately deleted from the vault afterwards.
#   2. The vault note itself. A transcript missing from the ledger whose note
#      already exists is never rewritten. The note is compared with the
#      transcript's own frontmatter (the state whispr hands over), not with any
#      literal status or topic value: if the note has moved on, /ingest already
#      ran; if it has not, an earlier run wrote it and /ingest never finished, so
#      only /ingest is re-run.
# ---------------------------------------------------------------------------
$script:IngestLedger = @{}   # transcript file name -> sha256; case-insensitive like the filesystem

function Get-TranscriptSha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-IngestLedgerLine {
    # Records the entry in memory and returns its JSONL line for the caller to
    # persist. Origin: ingest = this run's /ingest succeeded; backfill-vault =
    # the note had already been ingested by an earlier path; backfill-log =
    # seeded from a past run's SUCCESS line when the ledger was first created.
    param(
        [Parameter(Mandatory)][string]$TranscriptName,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][ValidateSet('ingest', 'backfill-vault', 'backfill-log')][string]$Origin
    )
    $script:IngestLedger[$TranscriptName] = $Sha256
    return ([PSCustomObject]@{
        transcript = $TranscriptName
        sha256     = $Sha256
        origin     = $Origin
        recordedAt = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Compress)
}

function Add-IngestLedgerEntry {
    param(
        [Parameter(Mandatory)][string]$TranscriptName,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][string]$Origin
    )
    $line = New-IngestLedgerLine -TranscriptName $TranscriptName -Sha256 $Sha256 -Origin $Origin
    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Would append ingest-ledger entry ($Origin): $TranscriptName"
        return
    }
    Add-Utf8Line -Path $LedgerFile -Line $line
}

function Initialize-IngestLedger {
    <#
      Load the ledger into $script:IngestLedger. A partial last line (a crash
      mid-append) or any other unparseable line is skipped, not fatal — the
      vault-note check behind the ledger still stops that file being rewritten.

      First run (no ledger file): seed it from every past run's
      "SUCCESS '<file>'" log line whose transcript still exists, so transcripts
      ingested before the ledger existed — including ones whose vault note was
      later deleted on purpose — are never re-ingested.
    #>
    if (Test-Path -LiteralPath $LedgerFile) {
        $bad = 0
        foreach ($line in [System.IO.File]::ReadAllLines($LedgerFile, $script:Utf8NoBom)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try {
                $entry = $line | ConvertFrom-Json -ErrorAction Stop
                if ($entry.transcript -and $entry.sha256) { $script:IngestLedger[[string]$entry.transcript] = [string]$entry.sha256 }
                else { $bad++ }
            } catch { $bad++ }
        }
        $badNote = if ($bad -gt 0) { " ($bad unparseable line(s) ignored)" } else { '' }
        Write-Log -Level INFO -Message "Ingest ledger loaded: $($script:IngestLedger.Count) transcript(s) from '$LedgerFile'$badNote."
        return
    }

    Write-Log -Level INFO -Message "Ingest ledger not found at '$LedgerFile' — seeding it from past nightly-ingest logs."
    $lines = [System.Collections.Generic.List[string]]::new()
    $successRe = '^\[[^\]]+\] \[INFO\] SUCCESS ''(.+?)'' — summarize cost '
    foreach ($log in (Get-ChildItem -LiteralPath $LogDir -Filter 'nightly-ingest-*.log' -File | Sort-Object Name)) {
        foreach ($m in (Select-String -LiteralPath $log.FullName -Pattern $successRe)) {
            $name = $m.Matches[0].Groups[1].Value
            if ($script:IngestLedger.ContainsKey($name)) { continue }
            $transcriptPath = Join-Path $TranscriptsDir $name
            if (-not (Test-Path -LiteralPath $transcriptPath)) { continue }
            $lines.Add((New-IngestLedgerLine -TranscriptName $name -Sha256 (Get-TranscriptSha256 -Path $transcriptPath) -Origin 'backfill-log'))
        }
    }
    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Would create the ingest ledger seeded with $($lines.Count) transcript(s) from past SUCCESS log lines (held in memory for this rehearsal only)."
        return
    }
    Write-Utf8File -Path $LedgerFile -Content ((@($lines) | ForEach-Object { "$_`n" }) -join '')
    Write-Log -Level INFO -Message "Ingest ledger created at '$LedgerFile', seeded with $($lines.Count) transcript(s) from past SUCCESS log lines."
}

function New-IngestDecision {
    # The gate's result. -Backfill: a skip should also record the transcript in
    # the ledger. -Alert: a skip must be reported durably, not just logged.
    param(
        [Parameter(Mandatory)][ValidateSet('skip', 'ingest-only', 'full')][string]$Action,
        [Parameter(Mandatory)][string]$Reason,
        [ValidateSet('INFO', 'WARN')][string]$Level = 'INFO',
        [switch]$Backfill,
        [switch]$Alert
    )
    return [PSCustomObject]@{ Action = $Action; Reason = $Reason; Level = $Level; Backfill = [bool]$Backfill; Alert = [bool]$Alert }
}

function Get-IngestDecision {
    # Decide what to do with one listed transcript BEFORE any claude call.
    param(
        [Parameter(Mandatory)][string]$TranscriptName,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][string]$TranscriptFrontmatter,
        [Parameter(Mandatory)][string]$NotePath
    )
    if ($script:IngestLedger.ContainsKey($TranscriptName)) {
        $ledgerSha = $script:IngestLedger[$TranscriptName]
        if ($ledgerSha -eq $Sha256) {
            return (New-IngestDecision -Action skip -Reason 'already ingested (ledger)')
        }
        # Deliberately a log line, not an alert (decision 2026-09-30): the note
        # stays exactly as it was ingested; nothing is re-summarized. Prefixes
        # are length-guarded because the ledger is a hand-editable text file.
        $short = { param($h) if ($h.Length -gt 12) { $h.Substring(0, 12) + '…' } else { $h } }
        return (New-IngestDecision -Action skip -Level WARN `
            -Reason "transcript changed since it was ingested (ledger sha256 $(& $short $ledgerSha), now $(& $short $Sha256)) — vault note left as ingested")
    }

    if (-not (Test-Path -LiteralPath $NotePath)) {
        return (New-IngestDecision -Action full -Reason 'new transcript')
    }

    $note = Split-Frontmatter -Content ([System.IO.File]::ReadAllText($NotePath))
    if (-not $note.Valid) {
        return (New-IngestDecision -Action skip -Level WARN -Alert `
            -Reason "vault note '$NotePath' exists but its frontmatter does not parse — left untouched")
    }
    $noteStatus = Get-FrontmatterScalar -Frontmatter $note.Frontmatter -Key 'status'
    $noteTopic  = Get-FrontmatterScalar -Frontmatter $note.Frontmatter -Key 'topic'
    $handedStatus = Get-FrontmatterScalar -Frontmatter $TranscriptFrontmatter -Key 'status'
    $handedTopic  = Get-FrontmatterScalar -Frontmatter $TranscriptFrontmatter -Key 'topic'

    if ($noteStatus -ne $handedStatus) {
        return (New-IngestDecision -Action skip -Backfill -Reason "vault note already moved on (status '$noteStatus')")
    }
    if ($noteTopic -ne $handedTopic) {
        return (New-IngestDecision -Action skip -Backfill -Reason "vault note already ingested (topic $noteTopic)")
    }
    return (New-IngestDecision -Action ingest-only -Reason 'vault note written by an earlier run whose /ingest never finished — re-running /ingest only, no re-summarize, no rewrite')
}

# ---------------------------------------------------------------------------
# Frontmatter parsing — Split-Frontmatter / ConvertFrom-YamlScalar /
# Get-FrontmatterScalar / Get-FrontmatterListArray now live in sync-common.ps1
# (shared with weekly-lint-compile.ps1's active-workstream derivation).
# Get-FrontmatterAttendees and Get-FrontmatterOrDefault stay here — nightly-
# only conveniences, the former just a thin join wrapper over the shared list
# parser.
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
# Main
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$script:DailyLogPath = Join-Path $LogDir ('nightly-ingest-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
if (-not (Test-Path -LiteralPath $script:DailyLogPath)) { [System.IO.File]::WriteAllText($script:DailyLogPath, '', $script:Utf8NoBom) }

try {
    Write-Log -Level INFO -Message "=== nightly-ingest starting (DryRun=$DryRun QuietMinutes=$QuietMinutes TimeoutSeconds=$TimeoutSeconds) ==="

    # Same exposure as weekly-lint-compile, which is where this bit: a 01:00
    # trigger on a laptop, then per-file summarize+ingest calls that each run
    # for minutes. This job has not been caught by standby yet; it is one line
    # to make sure it is not, rather than waiting for the night it is.
    Set-SystemAwake | Out-Null

    if (-not (Test-Path -LiteralPath $PromptTemplatePath)) {
        Invoke-JobFailure -StepName 'startup' -Detail "Summarize prompt template not found at '$PromptTemplatePath'."
    }
    $promptTemplate = [System.IO.File]::ReadAllText($PromptTemplatePath)

    # --- Step 2: watermark + ledger + enumerate candidates -------------------
    if ($WatermarkOverrideUtc) {
        if (-not $DryRun) {
            Invoke-JobFailure -StepName 'startup' -Detail "-WatermarkOverrideUtc is a rehearsal switch and is only accepted together with -DryRun."
        }
        try {
            $watermark = ConvertTo-UtcTimestamp -Text $WatermarkOverrideUtc
        } catch {
            Invoke-JobFailure -StepName 'startup' -Detail "-WatermarkOverrideUtc '$WatermarkOverrideUtc' is not a parseable date/time: $($_.Exception.Message)"
        }
        Write-Log -Level INFO -Message "[DRYRUN] Watermark OVERRIDDEN for this rehearsal: $($watermark.ToString('o')) (the watermark file is neither read nor written)."
    } else {
        $watermark = Get-Watermark
        Write-Log -Level INFO -Message "Watermark (last fully-good file's LastWriteTimeUtc): $($watermark.ToString('o'))"
    }

    # Quiet-period guard: skip anything whispr might still be actively writing.
    $cutoffUtc = (Get-Date).ToUniversalTime().AddMinutes(-1 * $QuietMinutes)

    if (-not (Test-Path -LiteralPath $TranscriptsDir)) {
        Invoke-JobFailure -StepName 'startup' -Detail "Transcripts directory not found at '$TranscriptsDir'."
    }
    if (-not (Test-Path -LiteralPath $VaultSourcesDir)) {
        Invoke-JobFailure -StepName 'startup' -Detail "Vault sources directory not found at '$VaultSourcesDir'."
    }

    # Only after both directories are confirmed: a first-run seed checks each
    # past SUCCESS line against $TranscriptsDir, so seeding while that directory
    # is missing would persist an empty ledger.
    Initialize-IngestLedger

    $candidates = Get-ChildItem -LiteralPath $TranscriptsDir -Filter '*.md' -File |
        Where-Object { $_.LastWriteTimeUtc -gt $watermark -and $_.LastWriteTimeUtc -lt $cutoffUtc } |
        Sort-Object LastWriteTimeUtc

    Write-Log -Level INFO -Message "Found $($candidates.Count) transcript(s) to consider: $(($candidates | ForEach-Object { $_.Name }) -join ', ')"

    $ingestedCount = 0
    $skippedCount = 0
    $totalCost = 0.0

    # --- Step 3: per-file loop ------------------------------------------------
    foreach ($file in $candidates) {
        $basename = $file.BaseName
        Write-Log -Level INFO -Message "--- Processing '$($file.Name)' (LastWriteTimeUtc=$($file.LastWriteTimeUtc.ToString('o'))) ---"

        $raw = [System.IO.File]::ReadAllText($file.FullName)

        # (a) Split frontmatter/body.
        $parsed = Split-Frontmatter -Content $raw
        if (-not $parsed.Valid) {
            Move-ToNeedsAttention -File $file -Reason 'no valid frontmatter block — not a transcript'
            continue
        }
        # Everything below either goes to claude or into the vault, so the
        # egress-excluded keys (join link/passcode) are dropped up front.
        $frontmatter = Remove-FrontmatterKeys -Frontmatter $parsed.Frontmatter -Keys $EgressExcludedFrontmatterKeys
        $body = $parsed.Body

        # (b) partial: / context fields.
        $partialRaw = Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'partial'
        $isPartial = ($null -ne $partialRaw) -and ($partialRaw.Trim().ToLowerInvariant() -eq 'true')
        if ($isPartial) {
            # Parked, not retried: whispr writes each transcript exactly once and
            # atomically (output.py::atomic_write_text) and never revisits it, so
            # a partial:true file will NEVER become non-partial. Retrying it every
            # night waits on an event that cannot occur. Parked rather than
            # ingested because whether a cut-off recording belongs in the vault is
            # a separate decision — its frontmatter already self-labels, so
            # ingesting partials outright is a reasonable future change.
            Move-ToNeedsAttention -File $file -Reason 'partial: true — recording was cut off'
            continue
        }

        # (b2) Idempotency gate — before any claude call. See the ledger comment
        # block above for why this exists and what each outcome means.
        $sha256 = Get-TranscriptSha256 -Path $file.FullName
        $targetPath = Join-Path $VaultSourcesDir "$basename.md"
        $decision = Get-IngestDecision -TranscriptName $file.Name -Sha256 $sha256 -TranscriptFrontmatter $frontmatter -NotePath $targetPath

        if ($decision.Action -eq 'skip') {
            Write-Log -Level $decision.Level -Message "SKIP '$($file.Name)' — $($decision.Reason). No claude call."
            if ($decision.Alert) {
                # The watermark still advances past this file, so without an
                # alert nothing would ever surface it again.
                $alertDetail = "$($decision.Reason). The transcript was not ingested and will not be listed again; fix the note's frontmatter by hand."
                if ($DryRun) {
                    Write-Log -Level WARN -Message "[DRYRUN] Would raise NEEDS ATTENTION (gate-note-unparseable) for '$($file.Name)': $alertDetail"
                } else {
                    Invoke-JobFailure -NonFatal -StepName 'gate-note-unparseable' -FileContext $file.Name -Detail $alertDetail
                }
            }
            if ($decision.Backfill) {
                Add-IngestLedgerEntry -TranscriptName $file.Name -Sha256 $sha256 -Origin 'backfill-vault'
            }
            Set-Watermark -UtcTimestamp $file.LastWriteTimeUtc
            $skippedCount += 1
            continue
        }

        $summarizeCost = 0.0
        if ($decision.Action -eq 'ingest-only') {
            Write-Log -Level INFO -Message "INGEST-ONLY '$($file.Name)' — $($decision.Reason)."
        } else {
            $callTitle   = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'call_title')
            $date        = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'date')
            $callType    = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'call_type')
            $organizer   = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'organizer')
            $attendees   = Get-FrontmatterOrDefault (Get-FrontmatterAttendees -Frontmatter $frontmatter)
            $inviteNotes = Get-FrontmatterOrDefault (Get-FrontmatterScalar -Frontmatter $frontmatter -Key 'invite_notes')

            # (c) Build the summarization prompt.
            $summarizePrompt = $promptTemplate.
                Replace('{{CALL_TITLE}}', $callTitle).
                Replace('{{DATE}}', $date).
                Replace('{{CALL_TYPE}}', $callType).
                Replace('{{ORGANIZER}}', $organizer).
                Replace('{{ATTENDEES}}', $attendees).
                Replace('{{INVITE_NOTES}}', $inviteNotes).
                Replace('{{TRANSCRIPT_BODY}}', $body)

            # (d)/(e) Summarize. haiku by default — summarization/paraphrase of an
            # already-structured transcript is a straightforward task, not one that
            # needs sonnet's extra reasoning; use sonnet below only where the task
            # (writing into the vault, following /ingest's multi-step rules)
            # actually benefits from it. No --allowedTools here: this call needs
            # zero file/tool access (prompt text is fully self-contained), so under
            # --permission-mode dontAsk every tool is auto-denied — strictly more
            # restrictive than granting Read/Edit/Write it doesn't need.
            $summarizeResult = Invoke-ClaudeStep -StepName "summarize:$($file.Name)" -PromptText $summarizePrompt `
                -ExtraArgs @('--model', 'haiku', '--permission-mode', 'dontAsk') `
                -WorkingDirectory $RepoRoot -TimeoutSeconds $TimeoutSeconds

            if (-not $summarizeResult.Success) {
                $why = if ($summarizeResult.TimedOut) { 'timed out' } else { 'failed or returned an empty result' }
                Invoke-JobFailure -StepName "summarize:$($file.Name)" -FileContext $file.Name `
                    -Detail "Summarize call $why." -ExitCode $summarizeResult.ExitCode -StdErr $summarizeResult.StdErr
            }
            $summarizeCost = $summarizeResult.CostUsd
            $totalCost += $summarizeCost
            $summarizedBody = $summarizeResult.Result

            # (f) Assemble the vault source file content deterministically — the
            # frontmatter block is carried over byte-for-byte (already vault-
            # compliant), we never let claude touch it. Written with -CreateNew:
            # the gate has just established there is no note at this path, so an
            # existing one here means something raced us — fail rather than
            # overwrite it.
            if ($DryRun) {
                $summarizedBody = "[DRYRUN placeholder — claude summarize call was skipped]`n`n" + $body
            }
            $outputContent = "---`n$frontmatter`n---`n`n$($summarizedBody.Trim())`n"

            if ($DryRun) {
                $preview = ($outputContent -split "`n" | Select-Object -First 20) -join "`n"
                Write-Log -Level INFO -Message "[DRYRUN] Would write vault source file: '$targetPath'"
                Write-Log -Level INFO -Message "[DRYRUN] First 20 lines of would-be content:`n$preview"
            } else {
                try {
                    Write-Utf8File -Path $targetPath -Content $outputContent -CreateNew
                } catch {
                    Invoke-JobFailure -StepName "write-vault-source:$($file.Name)" -FileContext $file.Name `
                        -Detail "Failed writing vault source file (an existing note is never overwritten): $($_.Exception.Message)"
                }
            }
        }

        # (g) Run /ingest on it from the vault root. sonnet here (not haiku):
        # /ingest has to correctly apply several conditional rules (preserve
        # pre-existing frontmatter verbatim, only replace a placeholder topic,
        # slug/collision handling, wiki-link casing) — worth the stronger model.
        $ingestArgs = @('--model', 'sonnet', '--permission-mode', 'dontAsk', '--allowedTools', 'Read,Edit,Write,Glob,Grep')
        $ingestPrompt = "/ingest $basename.md"
        $ingestResult = Invoke-ClaudeStep -StepName "ingest:$($file.Name)" -PromptText $ingestPrompt `
            -ExtraArgs $ingestArgs -WorkingDirectory $VaultRoot -TimeoutSeconds $TimeoutSeconds

        if (-not $ingestResult.Success) {
            $why = if ($ingestResult.TimedOut) { 'timed out' } else { 'failed or returned an empty result' }
            Invoke-JobFailure -StepName "ingest:$($file.Name)" -FileContext $file.Name `
                -Detail "/ingest call $why." -ExitCode $ingestResult.ExitCode -StdErr $ingestResult.StdErr
        }
        $totalCost += $ingestResult.CostUsd

        # (h) ONLY NOW record it in the ledger and advance the watermark — the
        # data-loss guard: anything that fails above leaves both untouched, so
        # next run retries it. Ledger first: a crash between the two leaves the
        # file listed again next run, where the ledger skips it for free.
        Add-IngestLedgerEntry -TranscriptName $file.Name -Sha256 $sha256 -Origin 'ingest'
        Set-Watermark -UtcTimestamp $file.LastWriteTimeUtc

        $ingestedCount += 1

        $tag = if ($DryRun) { '[DRYRUN] ' } else { '' }
        $modeNote = if ($decision.Action -eq 'ingest-only') { ' (ingest-only: existing note not re-summarized or rewritten)' } else { '' }
        Write-Log -Level INFO -Message "${tag}SUCCESS '$($file.Name)' — summarize cost `$$summarizeCost, ingest cost `$$($ingestResult.CostUsd).$modeNote"
    }

    Write-Log -Level INFO -Message "=== nightly-ingest SUCCESS — files processed: $ingestedCount, skipped (already ingested): $skippedCount, total cost: `$$totalCost ==="
    Write-Log -Level INFO -Message "Lint/compile hygiene pass is NOT run here — see weekly-lint-compile.ps1 (runs weekly, separately, due to cost/latency)."
    exit 0

} catch {
    Invoke-JobFailure -StepName 'unhandled-exception' -Detail $_.Exception.Message -StdErr ($_.ScriptStackTrace)
}
