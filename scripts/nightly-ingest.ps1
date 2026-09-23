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
    [int]$TimeoutSeconds = 300
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

function Get-Watermark {
    if (Test-Path -LiteralPath $WatermarkFile) {
        $raw = ''
        try {
            $raw = [System.IO.File]::ReadAllText($WatermarkFile).Trim()
            return ([datetime]::Parse($raw, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)).ToUniversalTime()
        } catch {
            Write-Log -Level WARN -Message "Watermark file unreadable/corrupt ('$raw'); treating as epoch (process everything). Error: $($_.Exception.Message)"
        }
    }
    return [datetime]::Parse('1970-01-01T00:00:00Z', [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
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
        Write-Log -Level ERROR -Message "Could NOT park '$($File.Name)' ($($_.Exception.Message)) — freezing the watermark instead, so it is retried rather than lost. The backlog will grow and cost will recur every night until this file is dealt with."
        Invoke-JobFailure -NonFatal -StepName 'needs-attention-park-failed' -FileContext $File.Name `
            -Detail "Could not move '$($File.FullName)' to '$NeedsAttentionDir': $($_.Exception.Message). Watermark FROZEN as a fallback — every later run will re-process the whole backlog until this file is moved or fixed by hand."
        return
    }

    # $dest is still in scope: try/catch does not create one in PowerShell, and
    # these lines are reachable only when the catch above did not return.
    Write-Log -Level WARN -Message "PARKED '$($File.Name)' -> '$dest' ($Reason). Watermark NOT frozen. $fixHint"
    Invoke-JobFailure -NonFatal -StepName 'needs-attention' -FileContext $File.Name `
        -Detail "Parked to '$dest' ($Reason). Later files in this run continue normally. $fixHint"
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

    # --- Step 2: watermark + enumerate candidates ---------------------------
    $watermark = Get-Watermark
    Write-Log -Level INFO -Message "Watermark (last fully-good file's LastWriteTimeUtc): $($watermark.ToString('o'))"

    # Quiet-period guard: skip anything whispr might still be actively writing.
    $cutoffUtc = (Get-Date).ToUniversalTime().AddMinutes(-1 * $QuietMinutes)

    if (-not (Test-Path -LiteralPath $TranscriptsDir)) {
        Invoke-JobFailure -StepName 'startup' -Detail "Transcripts directory not found at '$TranscriptsDir'."
    }
    $candidates = Get-ChildItem -LiteralPath $TranscriptsDir -Filter '*.md' -File |
        Where-Object { $_.LastWriteTimeUtc -gt $watermark -and $_.LastWriteTimeUtc -lt $cutoffUtc } |
        Sort-Object LastWriteTimeUtc

    Write-Log -Level INFO -Message "Found $($candidates.Count) transcript(s) to consider: $(($candidates | ForEach-Object { $_.Name }) -join ', ')"

    $ingestedCount = 0
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
        $frontmatter = $parsed.Frontmatter
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
        $totalCost += $summarizeResult.CostUsd
        $summarizedBody = $summarizeResult.Result

        # (f) Assemble the vault source file content deterministically — the
        # frontmatter block is carried over byte-for-byte (already vault-
        # compliant), we never let claude touch it.
        if ($DryRun) {
            $summarizedBody = "[DRYRUN placeholder — claude summarize call was skipped]`n`n" + $body
        }
        $outputContent = "---`n$frontmatter`n---`n`n$($summarizedBody.Trim())`n"
        $targetPath = Join-Path $VaultSourcesDir "$basename.md"

        if ($DryRun) {
            $preview = ($outputContent -split "`n" | Select-Object -First 20) -join "`n"
            Write-Log -Level INFO -Message "[DRYRUN] Would write vault source file: '$targetPath'"
            Write-Log -Level INFO -Message "[DRYRUN] First 20 lines of would-be content:`n$preview"
        } else {
            if (-not (Test-Path -LiteralPath $VaultSourcesDir)) {
                Invoke-JobFailure -StepName "write-vault-source:$($file.Name)" -FileContext $file.Name `
                    -Detail "Vault sources directory not found at '$VaultSourcesDir'."
            }
            try {
                Write-Utf8File -Path $targetPath -Content $outputContent
            } catch {
                Invoke-JobFailure -StepName "write-vault-source:$($file.Name)" -FileContext $file.Name `
                    -Detail "Failed writing vault source file: $($_.Exception.Message)"
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

        # (h) ONLY NOW advance the watermark — the data-loss guard: anything
        # that fails above leaves the watermark here, so next run retries it.
        Set-Watermark -UtcTimestamp $file.LastWriteTimeUtc

        $ingestedCount += 1

        $tag = if ($DryRun) { '[DRYRUN] ' } else { '' }
        Write-Log -Level INFO -Message "${tag}SUCCESS '$($file.Name)' — summarize cost `$$($summarizeResult.CostUsd), ingest cost `$$($ingestResult.CostUsd)."
    }

    Write-Log -Level INFO -Message "=== nightly-ingest SUCCESS — files processed: $ingestedCount, total cost: `$$totalCost ==="
    Write-Log -Level INFO -Message "Lint/compile hygiene pass is NOT run here — see weekly-lint-compile.ps1 (runs weekly, separately, due to cost/latency)."
    exit 0

} catch {
    Invoke-JobFailure -StepName 'unhandled-exception' -Detail $_.Exception.Message -StdErr ($_.ScriptStackTrace)
}
