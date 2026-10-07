<#
.SYNOPSIS
  Weekly vault-hygiene job: runs a full-vault /lint over cohoodOBS, derives
  Colby's currently active workstreams from recent meeting notes, extracts
  compile candidates from the lint report (split into an uncapped priority
  "workstream" tier and a capped "generic" tier), and compiles them via
  /compile. Generic candidates that don't fit under -MaxCompiles in a given
  run persist in an on-disk backlog and are drained first next time.

.WHY SEPARATE FROM nightly-ingest.ps1
  A full-vault /lint measured at ~19 minutes and ~$8 in practice. That's too
  slow and too expensive to run every weeknight as part of the cheap/fast
  per-file summarize+ingest job, so the hygiene pass (lint + compile) is its
  own job that runs once a week (Sunday night) instead.

.WORKSTREAM-FIRST SELECTION
  Compile-target selection is workstream-first, not just priority-ranked:
    - "Active workstreams" are derived deterministically (no API call) from
      cohoodOBS/sources/*.md meeting notes whose date: is within
      -WorkstreamDays of today — their topic: tags + call_title form the
      active-workstream signal.
    - The extraction step (haiku) is given both the lint report AND that
      signal, and splits candidates into two tiers:
        - workstream[]  — candidates that clearly match an active workstream.
          Compiled UNCAPPED (every one, one at a time) — this is what "the
          things Colby is actually working on right now" being unblocked from
          an arbitrary cap buys us. -MaxWorkstreamCompiles is a catastrophic
          BACKSTOP only (should never trigger in normal use).
        - other[]       — the remaining genuine compile candidates, ranked.
          Compiled up to -MaxCompiles, backlog-first (persistent backlog file
          drained ahead of this week's new candidates). Anything left over
          stays in the backlog for next run — nothing is silently dropped.
  -BacklogOnly skips lint/extraction/workstream-derivation entirely and just
  drains the generic backlog — a cheap on-demand catch-up path that doesn't
  pay for another ~$8 /lint.

.EGRESS DECISION
  Same accepted exception as nightly-ingest.ps1 — see that script's header and
  README-nightly-sync.md. This job talks to the same enterprise-governed
  Claude Code CLI, against the cohoodOBS vault only (no whispr transcript
  content is sent from this script — that already happened in nightly-ingest).

.USAGE
    pwsh -NoProfile -File scripts\weekly-lint-compile.ps1                    # real run
    pwsh -NoProfile -File scripts\weekly-lint-compile.ps1 -DryRun            # safe rehearsal
    pwsh -NoProfile -File scripts\weekly-lint-compile.ps1 -MaxCompiles 3
    pwsh -NoProfile -File scripts\weekly-lint-compile.ps1 -BacklogOnly       # cheap on-demand drain
    pwsh -NoProfile -File scripts\weekly-lint-compile.ps1 -BacklogOnly -DryRun

.PARAMETERS
  -DryRun                  Derive active workstreams for real (local file reads only, no
                            API call), log them and the full plan, but never call claude
                            and never write the backlog file. Logs what WOULD be compiled.
  -LintTimeoutSeconds       Hard external timeout for the /lint call. Default 1800 (30 min)
                            — comfortably above the ~19 min measured cost.
  -CompileTimeoutSeconds    Hard external timeout for each /compile call. Default 900 (15 min).
  -MaxCompiles              Upper bound on how many GENERIC (non-workstream) compile
                            candidates are compiled in a single run, backlog-first,
                            highest-priority-next. Default 5. Anything left over persists
                            in the on-disk backlog for the next run — never silently lost.
  -WorkstreamDays           A meeting-note call date within this many days of today makes
                            its topics/call_title part of the "active workstream" signal.
                            Default 21.
  -MaxWorkstreamCompiles    Catastrophic backstop only — normal runs stay well under this.
                            If the workstream tier somehow exceeds this count, compile only
                            the first N, WARN loudly, and push the rest to the backlog
                            instead of compiling an unbounded number in one run. Default 25.
  -BacklogOnly              Skip /lint, workstream derivation, and extraction entirely.
                            Just drain up to -MaxCompiles items from the persistent generic
                            backlog. Cheap, on-demand, no ~$8 /lint cost.

.NOTES
  Shares its claude-invocation/logging/failure-handling/frontmatter-parsing
  plumbing with nightly-ingest.ps1 via sync-common.ps1 (dot-sourced below) —
  no duplicated functions between the two scripts.
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$LintTimeoutSeconds = 1800,
    [int]$CompileTimeoutSeconds = 900,
    [int]$MaxCompiles = 5,
    [int]$WorkstreamDays = 21,
    [int]$MaxWorkstreamCompiles = 25,
    [switch]$BacklogOnly
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$RepoRoot      = Split-Path -Parent $PSScriptRoot                     # C:\github\whispr
$LogDir        = Join-Path $RepoRoot 'logs'
$VaultRoot     = 'C:\github\cohoodOBS'                                # sibling repo — not under $RepoRoot
$VaultSourcesDir = Join-Path $VaultRoot 'sources'
$VaultLogPath    = Join-Path $VaultRoot 'log.md'                      # the vault's own activity record

$EventLogSource = 'whispr-weekly'
$EventLogName   = 'Application'
$EventId        = 1002   # arbitrary but stable id for this source's entries
$JobName        = 'whispr-weekly-lint-compile'   # used by the shared failure-toast label

# Shared helpers (claude invocation, logging, failure handling, frontmatter
# parsing). Dot-sourcing runs in this script's own scope, so the constants
# above (and $DryRun) are already in scope for every function it defines.
. (Join-Path $PSScriptRoot 'sync-common.ps1')

# P2b: this job's claude calls load the whispr plugin through your settings, so its
# SessionStart hook would add the graph-first note to their context. Keep it off, so
# the job behaves as before (config/defaults.yaml graph_first_note.switch_env and
# off_value; tests/test_eval_graph_first.py checks the two agree). The child process
# inherits it from this process only.
$env:WHISPR_GRAPH_FIRST_NOTE = 'off'

if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$script:DailyLogPath = Join-Path $LogDir ('weekly-lint-compile-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
if (-not (Test-Path -LiteralPath $script:DailyLogPath)) { [System.IO.File]::WriteAllText($script:DailyLogPath, '', $script:Utf8NoBom) }

$AllowedTools = 'Read,Edit,Write,Glob,Grep'
$BacklogFile  = Join-Path $LogDir 'compile-backlog.txt'   # one concept name per line, generic-tier only

# ---------------------------------------------------------------------------
# Backlog persistence (generic tier only — the workstream tier is never
# backlog-driven except as the catastrophic overflow backstop below).
# ---------------------------------------------------------------------------
function Get-Backlog {
    # NOTE on the leading commas below: PowerShell unrolls an array returned
    # from a function onto the pipeline element-by-element, so a genuinely
    # EMPTY array collapses to $null on the caller's assignment (`$x = Get-
    # Backlog ...` would become $null, not @()) — which then hard-fails a
    # Mandatory [AllowEmptyCollection()][string[]] parameter downstream. The
    # unary comma operator (`,$array`) wraps the array as a single pipeline
    # object so it survives the return/capture round-trip as a real (possibly
    # empty) array, never $null. Every other array-returning helper below
    # follows the same convention.
    param([Parameter(Mandatory)][string]$BacklogFile)
    if (-not (Test-Path -LiteralPath $BacklogFile)) { return ,@() }
    $lines = Get-Content -LiteralPath $BacklogFile -ErrorAction SilentlyContinue
    return ,@($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_.Trim() })
}

function Set-Backlog {
    # Rewrites the backlog file to contain exactly $Items (one per line). The
    # caller is responsible for never invoking this under -DryRun. $Items is
    # deliberately NOT Mandatory (belt-and-suspenders alongside the comma-
    # operator fixes above): a Mandatory array parameter hard-fails
    # ("Cannot bind argument... because it is null") if a caller ever passes
    # a bare $null, whereas a non-Mandatory array param with an @() default
    # just treats $null/omitted the same as empty — never a crash.
    param([Parameter(Mandatory)][string]$BacklogFile, [AllowEmptyCollection()][string[]]$Items = @())
    $content = if ($Items.Count -gt 0) { ($Items -join "`n") + "`n" } else { '' }
    Write-Utf8File -Path $BacklogFile -Content $content
}

function Get-DedupedOrderedList {
    # Case-insensitive dedup that preserves first-seen order and casing. Used
    # both for the active-workstream signal (topics + call titles across many
    # notes) and for building the backlog-first generic compile queue — one
    # parameterized helper instead of two copies of the same seen-hashtable
    # pattern. $Items not Mandatory — see Set-Backlog's comment above.
    param([AllowEmptyCollection()][string[]]$Items = @())
    $seen = @{}
    $result = [System.Collections.Generic.List[string]]::new()
    foreach ($item in $Items) {
        if ([string]::IsNullOrWhiteSpace($item)) { continue }
        $key = $item.Trim().ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $result.Add($item.Trim())
        }
    }
    return ,@($result)
}

# ---------------------------------------------------------------------------
# Active-workstream derivation (deterministic PowerShell, no API call).
# ---------------------------------------------------------------------------
function Get-ActiveWorkstreamSignal {
    param(
        [Parameter(Mandatory)][string]$SourcesDir,
        [Parameter(Mandatory)][int]$WorkstreamDays
    )
    if (-not (Test-Path -LiteralPath $SourcesDir)) {
        Write-Log -Level WARN -Message "Vault sources directory not found at '$SourcesDir' — active-workstream signal will be empty."
        return ,@()
    }

    $today = (Get-Date).Date
    $rawTerms = [System.Collections.Generic.List[string]]::new()
    $includedNotes = 0

    $files = Get-ChildItem -LiteralPath $SourcesDir -Filter '*.md' -File
    foreach ($file in $files) {
        $raw = [System.IO.File]::ReadAllText($file.FullName)
        $parsed = Split-Frontmatter -Content $raw
        if (-not $parsed.Valid) { continue }
        $fm = $parsed.Frontmatter

        $source = Get-FrontmatterScalar -Frontmatter $fm -Key 'source'
        if ($source -ne 'meeting') { continue }

        $dateRaw = Get-FrontmatterScalar -Frontmatter $fm -Key 'date'
        $noteDate = $null
        if ($dateRaw) {
            try {
                $noteDate = [datetime]::Parse($dateRaw, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None)
            } catch {
                $noteDate = $null
            }
        }
        if (-not $noteDate) { $noteDate = $file.LastWriteTime }

        $diffDays = [math]::Abs(($today - $noteDate.Date).TotalDays)
        if ($diffDays -gt $WorkstreamDays) { continue }

        $includedNotes += 1
        $topics = Get-FrontmatterListArray -Frontmatter $fm -Key 'topic'
        $callTitle = Get-FrontmatterScalar -Frontmatter $fm -Key 'call_title'
        foreach ($t in @($topics)) { $rawTerms.Add($t) }
        if ($callTitle) { $rawTerms.Add($callTitle) }
    }

    Write-Log -Level INFO -Message "Active-workstream derivation: $includedNotes meeting note(s) within $WorkstreamDays day(s) of today out of $($files.Count) source file(s) scanned."
    # Plain pass-through — NOT `,@(...)`. Get-DedupedOrderedList already
    # returns a comma-wrapped (collapse-proof) array; wrapping its call again
    # here would double-nest it (the whole point of the comma trick is that
    # exactly one pipeline object — the array itself — flows through a plain
    # `return`/assignment chain unchanged, all the way to the final caller).
    return Get-DedupedOrderedList -Items @($rawTerms)
}

# ---------------------------------------------------------------------------
# Lint-report parsing (deterministic — the stale-pages list drives the
# "already compiled, but don't skip it" exception below).
# ---------------------------------------------------------------------------
function Get-LintStalePages {
    # Parses the "### 4. Stale wiki pages" section of the lint report for
    # `- **<Name>.md**` bullets. Deliberately simple/regex-based, matching the
    # report's consistent markdown shape (this section always bolds bare
    # `Name.md`, distinct from the `**[[Name]]**` wiki-link bolding used for
    # dangling-link compile candidates elsewhere in the same report).
    param([AllowNull()][string]$LintReportText)
    if ([string]::IsNullOrWhiteSpace($LintReportText)) { return ,@() }
    if ($LintReportText -notmatch '(?ms)^###\s*4\..*?Stale wiki pages.*?\r?\n(.*?)(?=^###\s*5\.|\z)') { return ,@() }
    $section = $Matches[1]
    $names = [regex]::Matches($section, '(?m)^-\s+\*\*([^*]+?)\.md\*\*') | ForEach-Object { $_.Groups[1].Value.Trim() }
    return ,@($names | Select-Object -Unique)
}

function Get-LintFindingCounts {
    # Pulls the per-section "(N)" counts out of the lint report's six section
    # headers (`### 1. Orphan notes (12)`, `### 2. Dangling links (450)`, ...).
    # Deliberately does NOT default a missing section to 0: writing "0 orphans"
    # because the parse failed would put a false number into the vault's
    # permanent record, which is worse than recording that counts weren't
    # available. Complete is true only when all six parsed.
    param([AllowNull()][string]$LintReportText)
    $counts = @{}
    if (-not [string]::IsNullOrWhiteSpace($LintReportText)) {
        foreach ($m in [regex]::Matches($LintReportText, '(?m)^###\s*([1-6])\.[^\r\n(]*\((\d+)\)')) {
            $counts[[int]$m.Groups[1].Value] = [int]$m.Groups[2].Value
        }
    }
    $missing = @(1..6 | Where-Object { -not $counts.ContainsKey($_) })
    return [PSCustomObject]@{ Counts = $counts; Complete = ($missing.Count -eq 0); Missing = $missing }
}

function Write-VaultLintLogEntry {
    <#
      THE BACKSTOP for the vault's lint record.

      /lint's command file tells Claude to append a `## [date] lint | N findings`
      line to cohoodOBS/log.md — but that instruction sat AFTER an interactive
      "which should I fix?" question, and this job calls `claude -p`, where there
      is no second turn and nobody ever answers. The model emitted its report,
      reached the question, and the process exited before appending anything. So
      between 2026-08-17 and 2026-09-13 the vault recorded four successful weekly
      lint runs as: nothing at all. The report is held in memory only, so those
      four weeks of findings are unrecoverable.

      lint.md is fixed too (the append is now a work step ahead of the question),
      but a prompt instruction is precisely what failed here, so this writes the
      line deterministically whenever the model didn't.

      Idempotent — if today's entry is already present the prompt fix worked and
      this does nothing. Never throws: a record-keeping backstop must not be able
      to fail the job it exists to record. Also appends at the true end of file,
      which the model-written entries have not reliably done.
    #>
    param(
        [Parameter(Mandatory)][string]$VaultLogPath,
        [AllowNull()][string]$LintReportText
    )
    try {
        if (-not (Test-Path -LiteralPath $VaultLogPath)) {
            Write-Log -Level WARN -Message "Vault log not found at '$VaultLogPath' — this lint run will go unrecorded."
            return
        }

        $today = Get-Date -Format 'yyyy-MM-dd'
        $existing = [System.IO.File]::ReadAllText($VaultLogPath)
        if ($existing -match ('(?m)^##\s*\[' + [regex]::Escape($today) + '\]\s*lint\s*\|')) {
            Write-Log -Level INFO -Message "Vault log already carries a lint entry for $today (/lint wrote its own) — backstop not needed."
            return
        }

        $parsed = Get-LintFindingCounts -LintReportText $LintReportText
        if ($parsed.Complete) {
            $c = $parsed.Counts
            $total = $c[1] + $c[2] + $c[3] + $c[4] + $c[5] + $c[6]
            $entry = "## [$today] lint | $total findings — $($c[1]) orphans, $($c[2]) dangling links ($($c[6]) compile candidates), $($c[3]) contradictions, $($c[4]) stale pages, $($c[5]) stale claims <!-- backstop -->"
        } else {
            Write-Log -Level WARN -Message "Lint report section counts not fully parseable (missing section(s): $($parsed.Missing -join ', ')) — recording the run without inventing numbers."
            $entry = "## [$today] lint | ran (weekly job) — finding counts unparseable; see whispr logs\weekly-lint-compile-$today.log <!-- backstop -->"
        }

        # Defensive: many agents mutate this file, and a missing trailing newline
        # would otherwise glue this entry onto the last existing line.
        if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { $entry = "`n" + $entry }

        Add-Utf8Line -Path $VaultLogPath -Line $entry
        Write-Log -Level WARN -Message "BACKSTOP FIRED — /lint did not write its own log.md entry; wrote it from PowerShell instead: $entry"
    } catch {
        Write-Log -Level WARN -Message "Could not write the vault lint log entry (non-fatal — the lint itself succeeded): $($_.Exception.Message)"
    }
}

function Test-VaultPageExists {
    param([Parameter(Mandatory)][string]$Concept)
    $path = Join-Path $VaultRoot ("$Concept.md")
    return Test-Path -LiteralPath $path
}

function ConvertFrom-WorkstreamSplit {
    # Tolerant parse of the extraction step's { "workstream": [...], "other":
    # [...] } JSON object. Built on the shared ConvertFrom-ClaudeJsonLenient
    # fence-stripper (sync-common.ps1) rather than duplicating it.
    param([AllowNull()][string]$RawResult)
    # NOTE: these are plain (non-`return`) assignments of array literals/
    # expressions, which never collapse to $null in PowerShell — the comma-
    # operator wrap is only needed at `return` boundaries and captured
    # function-call assignments (see Get-Backlog's comment above), so it's
    # deliberately NOT used here.
    $parsed = ConvertFrom-ClaudeJsonLenient -RawResult $RawResult
    $workstream = @()
    $other = @()
    if ($parsed) {
        if (($parsed.PSObject.Properties.Name -contains 'workstream') -and $parsed.workstream) { $workstream = @($parsed.workstream) }
        if (($parsed.PSObject.Properties.Name -contains 'other') -and $parsed.other) { $other = @($parsed.other) }
    }
    return [PSCustomObject]@{ Workstream = $workstream; Other = $other }
}

# ---------------------------------------------------------------------------
# Compile mechanics — a single choke point used by both the uncapped
# workstream tier and the capped generic/backlog tier, so the skip-if-
# already-compiled check, DryRun handling, the /compile call + JobFailure,
# cost accounting, and logging exist in exactly one place.
# ---------------------------------------------------------------------------
function Invoke-ConceptCompile {
    param(
        [Parameter(Mandatory)][string]$Concept,
        [Parameter(Mandatory)][string]$Tier,             # label for logging only, e.g. 'workstream' / 'generic'
        [AllowEmptyCollection()][string[]]$StalePages = @(),
        [Parameter(Mandatory)][string[]]$CompileArgs,
        [Parameter(Mandatory)][int]$CompileTimeoutSeconds
    )
    if (-not ($StalePages -contains $Concept) -and (Test-VaultPageExists -Concept $Concept)) {
        Write-Log -Level INFO -Message "SKIPPED (already compiled — page exists, not in lint's stale-pages list) [$Tier tier]: $Concept"
        return [PSCustomObject]@{ Outcome = 'SkippedAlreadyDone'; CostUsd = 0.0 }
    }

    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Would compile '$Concept' [$Tier tier]."
        return [PSCustomObject]@{ Outcome = 'WouldCompile'; CostUsd = 0.0 }
    }

    $compileResult = Invoke-ClaudeStep -StepName "compile:$Concept" -PromptText ('/compile "' + $Concept + '"') `
        -ExtraArgs $CompileArgs -WorkingDirectory $VaultRoot -TimeoutSeconds $CompileTimeoutSeconds
    if (-not $compileResult.Success) {
        $why = if ($compileResult.TimedOut) { 'timed out' } else { 'failed or returned an empty result' }
        Invoke-JobFailure -StepName "compile:$Concept" -Detail "/compile call $why [$Tier tier]." `
            -ExitCode $compileResult.ExitCode -StdErr $compileResult.StdErr
    }
    Write-Log -Level INFO -Message "SUCCESS compiled '$Concept' [$Tier tier] — cost `$$($compileResult.CostUsd)."
    return [PSCustomObject]@{ Outcome = 'Compiled'; CostUsd = $compileResult.CostUsd }
}

function Invoke-QueueCompilePass {
    # Compiles up to -MaxCount items from $Queue (in order). After EACH
    # successful compile OR each already-done skip, rewrites $BacklogFile to
    # contain exactly the still-uncompiled/undropped items (unless -DryRun),
    # so a mid-run failure leaves an accurate on-disk backlog. Items beyond
    # -MaxCount simply remain queued (and so, via the rewrite, remain in the
    # backlog file) for next run.
    param(
        [AllowEmptyCollection()][string[]]$Queue = @(),
        [Parameter(Mandatory)][int]$MaxCount,
        [AllowEmptyCollection()][string[]]$StalePages = @(),
        [Parameter(Mandatory)][string]$BacklogFile,
        [Parameter(Mandatory)][string[]]$CompileArgs,
        [Parameter(Mandatory)][int]$CompileTimeoutSeconds
    )
    $remaining = [System.Collections.Generic.List[string]]::new()
    foreach ($item in $Queue) { $remaining.Add($item) }

    $compiledConcepts = [System.Collections.Generic.List[string]]::new()
    $totalCostThisPass = 0.0

    while ($remaining.Count -gt 0 -and $compiledConcepts.Count -lt $MaxCount) {
        $concept = $remaining[0]
        $r = Invoke-ConceptCompile -Concept $concept -Tier 'generic' -StalePages $StalePages `
            -CompileArgs $CompileArgs -CompileTimeoutSeconds $CompileTimeoutSeconds
        $totalCostThisPass += $r.CostUsd

        # Every outcome removes the current head — compiled/would-compile
        # consumes it, skipped-already-done drops it. Only the rewrite differs.
        $remaining.RemoveAt(0)
        if ($r.Outcome -eq 'Compiled' -or $r.Outcome -eq 'WouldCompile') {
            $compiledConcepts.Add($concept)
        }
        if (-not $DryRun -and ($r.Outcome -eq 'Compiled' -or $r.Outcome -eq 'SkippedAlreadyDone')) {
            Set-Backlog -BacklogFile $BacklogFile -Items @($remaining)
        }
    }

    return [PSCustomObject]@{
        CompiledConcepts = @($compiledConcepts)
        CompiledCount    = $compiledConcepts.Count
        Remaining        = @($remaining)
        TotalCostUsd     = $totalCostThisPass
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    Write-Log -Level INFO -Message "=== weekly-lint-compile starting (DryRun=$DryRun BacklogOnly=$BacklogOnly LintTimeoutSeconds=$LintTimeoutSeconds CompileTimeoutSeconds=$CompileTimeoutSeconds MaxCompiles=$MaxCompiles WorkstreamDays=$WorkstreamDays MaxWorkstreamCompiles=$MaxWorkstreamCompiles) ==="

    # Before any long claude call — this job is the reason Set-SystemAwake
    # exists (2026-09-08: standby froze /lint 65 s in and the timeout fired 19 h
    # later on wake). Not gated on -DryRun: a dry run does only local file reads
    # and finishes in seconds, so the request costs nothing either way, and
    # gating it would mean the rehearsal no longer matches the real run.
    Set-SystemAwake | Out-Null

    $totalCost = 0.0
    $compileArgs = @('--model', 'sonnet', '--permission-mode', 'dontAsk', '--allowedTools', $AllowedTools)

    # =========================================================================
    # -BacklogOnly: cheap on-demand drain. No lint, no extraction, no
    # workstream derivation — just compile up to -MaxCompiles from whatever
    # is already sitting in the backlog file.
    # =========================================================================
    if ($BacklogOnly) {
        # Plain assignment (no extra @() wrap) — Get-Backlog already returns a
        # comma-wrapped array (see its own comment), which survives a plain
        # `$x = Get-Backlog ...` capture as a real, non-null array even when
        # empty. Wrapping the call AGAIN with @() here would double-nest it
        # into a 1-element array containing that array.
        $backlog = Get-Backlog -BacklogFile $BacklogFile
        if ($backlog.Count -eq 0) {
            Write-Log -Level INFO -Message "-BacklogOnly: backlog file '$BacklogFile' is empty or missing — nothing to drain."
            Write-Log -Level INFO -Message "=== weekly-lint-compile SUCCESS (BacklogOnly) — compiled: 0, backlog remaining: 0, total cost: `$0.0 ==="
            exit 0
        }
        Write-Log -Level INFO -Message "-BacklogOnly: backlog loaded ($($backlog.Count) item(s)): $($backlog -join ', ')"

        $pass = Invoke-QueueCompilePass -Queue $backlog -MaxCount $MaxCompiles -StalePages @() `
            -BacklogFile $BacklogFile -CompileArgs $compileArgs -CompileTimeoutSeconds $CompileTimeoutSeconds
        $totalCost += $pass.TotalCostUsd

        $remainingDesc = if ($pass.Remaining.Count -gt 0) { $pass.Remaining -join ', ' } else { '(none)' }
        Write-Log -Level INFO -Message "=== weekly-lint-compile SUCCESS (BacklogOnly) — compiled: $($pass.CompiledCount), backlog remaining: $($pass.Remaining.Count) [$remainingDesc], total cost: `$$totalCost ==="
        exit 0
    }

    # =========================================================================
    # Normal run: backlog load -> workstream derivation -> lint -> extract ->
    # Tier 1 (workstream, uncapped) -> Tier 2 (generic, capped, backlog-first).
    # =========================================================================

    # --- Step 1: load persistent generic backlog -------------------------------
    # Plain assignment — see comment in the -BacklogOnly branch above.
    $backlog = Get-Backlog -BacklogFile $BacklogFile
    Write-Log -Level INFO -Message "Generic backlog loaded ($($backlog.Count) item(s)): $(if ($backlog.Count -gt 0) { $backlog -join ', ' } else { '(empty)' })"

    # --- Step 2: derive active workstreams (deterministic, no API) -------------
    $activeWorkstreamSignal = Get-ActiveWorkstreamSignal -SourcesDir $VaultSourcesDir -WorkstreamDays $WorkstreamDays
    Write-Log -Level INFO -Message "Active workstream signal (topics/call-titles from meeting notes within $WorkstreamDays day(s)): $(if ($activeWorkstreamSignal.Count -gt 0) { $activeWorkstreamSignal -join ', ' } else { '(none)' })"

    # --- Step 3: /lint -----------------------------------------------------------
    $lintArgs = @('--model', 'sonnet', '--permission-mode', 'dontAsk', '--allowedTools', $AllowedTools)
    $lintResult = Invoke-ClaudeStep -StepName 'lint' -PromptText '/lint' -ExtraArgs $lintArgs `
        -WorkingDirectory $VaultRoot -TimeoutSeconds $LintTimeoutSeconds
    if (-not $lintResult.Success) {
        $why = if ($lintResult.TimedOut) { 'timed out' } else { 'failed or returned an empty result' }
        Invoke-JobFailure -StepName 'lint' -Detail "/lint call $why." -ExitCode $lintResult.ExitCode -StdErr $lintResult.StdErr
    }
    $totalCost += $lintResult.CostUsd
    $lintReportText = if ($DryRun) { '[DRYRUN placeholder — /lint was not actually run]' } else { $lintResult.Result }

    $stalePages = Get-LintStalePages -LintReportText $lintReportText
    Write-Log -Level INFO -Message "Lint stale-pages list (existing pages eligible for recompile even though they already exist): $(if ($stalePages.Count -gt 0) { $stalePages -join ', ' } else { '(none)' })"

    # Record the lint run in the vault's own log here — before extraction and the
    # compile calls, so no other claude process is writing to log.md concurrently.
    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Would ensure cohoodOBS/log.md carries a lint entry for today (backstop makes no writes in a rehearsal)."
    } else {
        Write-VaultLintLogEntry -VaultLogPath $VaultLogPath -LintReportText $lintReportText
    }

    # --- Step 4: extract compile candidates (haiku), workstream-first split ----
    # haiku: constrained extraction/classification over text we already have
    # (the lint report + the deterministic workstream signal) — no vault
    # mutation, no multi-step rules, doesn't need sonnet.
    $workstreamSignalText = if ($activeWorkstreamSignal.Count -gt 0) {
        (($activeWorkstreamSignal | ForEach-Object { "- $_" }) -join "`n")
    } else {
        '(none — no meeting notes found within the workstream window)'
    }

    $conceptPrompt = @"
You are reviewing this week's vault lint report to identify compile candidates,
prioritized against Colby's currently active work.

ACTIVE WORKSTREAM SIGNAL (topics/call-titles from meeting notes within the
last $WorkstreamDays days):
$workstreamSignalText

LINT REPORT:
$lintReportText

TASK: Return ONLY a JSON object (no prose, no markdown code fences) with two
array fields:
{ "workstream": [...], "other": [...] }

"workstream": concepts from the lint report -- either compile candidates
(3+ source notes, no existing page) OR stale existing wiki pages needing a
recompile -- that clearly refer to the SAME project/entity/thing as one of
the active workstream signal terms above. A generic cross-cutting tag like
"staffing", "kickoff", or "status-update" does NOT by itself make a compile
target -- only concrete projects/entities/topics do. These are absolute
priority.

"other": the remaining genuine compile candidates, ranked highest-priority
first, EXCLUDING:
  (a) broad catch-all/parent-entity umbrellas the report itself flags as a
      "catch-all" or that are obviously too broad to be a useful page (e.g.
      "Deloitte"), and
  (b) obvious mis-transcription/noise tokens (short gibberish, non-concept
      fragments).

If nothing qualifies for a field, return an empty array for it. Return the
JSON object and nothing else.
"@

    $conceptResult = Invoke-ClaudeStep -StepName 'extract-compile-targets' -PromptText $conceptPrompt `
        -ExtraArgs @('--model', 'haiku', '--permission-mode', 'dontAsk') `
        -WorkingDirectory $VaultRoot -TimeoutSeconds $LintTimeoutSeconds
    if (-not $conceptResult.Success) {
        # A genuine call failure (non-zero exit / timeout) fails the job. An
        # empty/unparseable *content* result (handled below) is different —
        # that's just "no compile candidates this week," not a call failure.
        $why = if ($conceptResult.TimedOut) { 'timed out' } else { 'failed' }
        Invoke-JobFailure -StepName 'extract-compile-targets' -Detail "Compile-target extraction call $why." `
            -ExitCode $conceptResult.ExitCode -StdErr $conceptResult.StdErr
    }
    $totalCost += $conceptResult.CostUsd

    if ($DryRun) {
        Write-Log -Level INFO -Message "[DRYRUN] Extraction call not actually run — workstream/other candidate lists are empty placeholders for this rehearsal; a real run parses claude's JSON here."
        $splitResult = [PSCustomObject]@{ Workstream = @(); Other = @() }
    } else {
        $splitResult = ConvertFrom-WorkstreamSplit -RawResult $conceptResult.Result
    }

    if ($splitResult.Workstream.Count -eq 0 -and $splitResult.Other.Count -eq 0) {
        Write-Log -Level INFO -Message "Nothing to compile from this week's extraction (workstream and other both empty) — proceeding to backlog handling."
    } else {
        Write-Log -Level INFO -Message "Workstream compile targets (priority, uncapped): $(if ($splitResult.Workstream.Count -gt 0) { $splitResult.Workstream -join ', ' } else { '(none)' })"
        Write-Log -Level INFO -Message "Other compile candidates (capped, backlog-first): $(if ($splitResult.Other.Count -gt 0) { $splitResult.Other -join ', ' } else { '(none)' })"
    }

    # --- Step 5: Tier 1 -- workstream compiles (uncapped; backstop only) -------
    $workstreamList = @($splitResult.Workstream)
    $workstreamToCompile = $workstreamList
    $workstreamOverflow = @()
    if ($workstreamList.Count -gt $MaxWorkstreamCompiles) {
        $workstreamToCompile = @($workstreamList | Select-Object -First $MaxWorkstreamCompiles)
        $workstreamOverflow  = @($workstreamList | Select-Object -Skip $MaxWorkstreamCompiles)
        Write-Log -Level WARN -Message "WORKSTREAM COMPILE BACKSTOP TRIGGERED: workstream list has $($workstreamList.Count) item(s), exceeding -MaxWorkstreamCompiles=$MaxWorkstreamCompiles. This should never happen in normal use -- investigate extraction quality. Compiling first $MaxWorkstreamCompiles; deferring the rest to the backlog: $($workstreamOverflow -join ', ')"
    }

    $workstreamCompiledCount = 0
    foreach ($concept in $workstreamToCompile) {
        $r = Invoke-ConceptCompile -Concept $concept -Tier 'workstream' -StalePages $stalePages `
            -CompileArgs $compileArgs -CompileTimeoutSeconds $CompileTimeoutSeconds
        $totalCost += $r.CostUsd
        if ($r.Outcome -eq 'Compiled' -or $r.Outcome -eq 'WouldCompile') { $workstreamCompiledCount += 1 }
    }

    if ($workstreamOverflow.Count -gt 0) {
        if ($DryRun) {
            Write-Log -Level INFO -Message "[DRYRUN] Would append workstream overflow to backlog: $($workstreamOverflow -join ', ')"
        } else {
            # NOTE: Get-Backlog is called bare (no @() wrap) here — it already
            # returns a comma-wrapped array; used directly as the left side of
            # `+` it behaves as a normal (possibly empty) array, whereas
            # wrapping the live call in @() would double-nest it (see the
            # comments on Get-Backlog / Get-ActiveWorkstreamSignal above).
            $newBacklog = Get-DedupedOrderedList -Items ((Get-Backlog -BacklogFile $BacklogFile) + @($workstreamOverflow))
            Set-Backlog -BacklogFile $BacklogFile -Items $newBacklog
            $backlog = $newBacklog
            Write-Log -Level WARN -Message "Appended $($workstreamOverflow.Count) workstream-overflow item(s) to the backlog: $($workstreamOverflow -join ', ')"
        }
    }

    # --- Step 6: Tier 2 -- generic compiles (capped, backlog-first) ------------
    # Plain assignment (no outer @() wrap) — see Get-Backlog's comment above
    # for why wrapping a comma-returning function's call site double-nests it.
    $genericQueue = Get-DedupedOrderedList -Items (@($backlog) + @($splitResult.Other))
    Write-Log -Level INFO -Message "Generic compile queue (backlog-first, deduped, up to -MaxCompiles=$MaxCompiles): $(if ($genericQueue.Count -gt 0) { $genericQueue -join ', ' } else { '(empty)' })"

    $pass = Invoke-QueueCompilePass -Queue $genericQueue -MaxCount $MaxCompiles -StalePages $stalePages `
        -BacklogFile $BacklogFile -CompileArgs $compileArgs -CompileTimeoutSeconds $CompileTimeoutSeconds
    $totalCost += $pass.TotalCostUsd

    $backlogKeys = @{}
    foreach ($b in $backlog) { $backlogKeys[$b.Trim().ToLowerInvariant()] = $true }
    $fromBacklog = @($pass.CompiledConcepts | Where-Object { $backlogKeys.ContainsKey($_.Trim().ToLowerInvariant()) })
    $fromNew     = @($pass.CompiledConcepts | Where-Object { -not $backlogKeys.ContainsKey($_.Trim().ToLowerInvariant()) })

    # --- Step 8: final log -------------------------------------------------------
    $remainingDesc = if ($pass.Remaining.Count -gt 0) { $pass.Remaining -join ', ' } else { '(none)' }
    Write-Log -Level INFO -Message "=== weekly-lint-compile SUCCESS — workstream compiled: $workstreamCompiledCount, generic compiled: $($pass.CompiledCount) (from backlog: $($fromBacklog.Count), new this run: $($fromNew.Count)), backlog remaining: $($pass.Remaining.Count) [$remainingDesc], total cost: `$$totalCost ==="
    exit 0

} catch {
    Invoke-JobFailure -StepName 'unhandled-exception' -Detail $_.Exception.Message -StdErr ($_.ScriptStackTrace)
}
