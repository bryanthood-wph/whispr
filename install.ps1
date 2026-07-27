<#
.SYNOPSIS
  whispr recipient-side installer (thin bootstrap). Downloads the offline
  install bundle if needed, sets up a self-contained Python environment fully
  offline, then hands off to scripts/run_installer.py for prompts, the audio
  device picker, config.yaml patching, the Startup-folder shortcut, and a
  smoke test. Safe to re-run.

.NOTES
  Do not double-click this file directly - use install.cmd, which unblocks
  the extracted files and launches this with the right execution policy.
#>

$ErrorActionPreference = 'Stop'

# Pinned release asset this installer knows how to fetch. Bumping the
# distributed version only requires updating these two lines.
$ReleaseTag        = 'v0.1.0'
$BundleAssetName   = 'whispr-offline-bundle.zip'
$ChecksumAssetName = 'SHA256SUMS.txt'
$RepoSlug          = 'bryanthood-wph/whispr'

$InstallRoot = $PSScriptRoot
Set-Location -LiteralPath $InstallRoot

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    Write-Host "FAILED: $Message" -ForegroundColor Red
    exit 1
}

$BundleZip      = Join-Path $InstallRoot $BundleAssetName
$PythonDir      = Join-Path $InstallRoot 'python'
$WheelsDir      = Join-Path $InstallRoot 'wheels'
$ModelDir       = Join-Path $InstallRoot 'model'
$GetPip         = Join-Path $InstallRoot 'get-pip.py'
$VersionMarker  = Join-Path $InstallRoot '.bundle-version'

# --- Step 0: quick compatibility checks, before downloading anything --------
Write-Step "Checking compatibility..."
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -notin @('AMD64', 'x86')) {
    Write-Host "WARNING: this machine reports architecture '$arch'. whispr's bundled Python" -ForegroundColor Yellow
    Write-Host "and dependencies are built for 64-bit x86 (AMD64) Windows only and will" -ForegroundColor Yellow
    Write-Host "likely fail to run here (e.g. ARM64 devices like Surface Pro X)." -ForegroundColor Yellow
    if ((Read-Host "Continue anyway? (y/N)") -notmatch '^[Yy]') { Write-Host "Cancelled."; exit 1 }
}

$localAppData = $env:LOCALAPPDATA
$newTeams     = Join-Path $localAppData 'Microsoft\WindowsApps\ms-teams.exe'
$classicTeams = Join-Path $localAppData 'Microsoft\Teams\current\Teams.exe'
$teamsOnPath  = Get-Command 'ms-teams.exe' -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $newTeams) -and -not $teamsOnPath) {
    if (Test-Path -LiteralPath $classicTeams) {
        Write-Host "WARNING: only the CLASSIC Teams client was found on this machine. whispr's" -ForegroundColor Yellow
        Write-Host "call detection targets the NEW Teams client and will not work with classic Teams." -ForegroundColor Yellow
    } else {
        Write-Host "WARNING: could not find Microsoft Teams installed on this machine. whispr" -ForegroundColor Yellow
        Write-Host "only records Teams calls/meetings." -ForegroundColor Yellow
    }
    if ((Read-Host "Continue anyway? (y/N)") -notmatch '^[Yy]') { Write-Host "Cancelled."; exit 1 }
}

# --- Step 1: obtain the offline bundle (skip if already staged AND current) -
$alreadyStaged = (Test-Path -LiteralPath $PythonDir) -and (Test-Path -LiteralPath $WheelsDir) -and (Test-Path -LiteralPath $ModelDir) `
    -and (Test-Path -LiteralPath $VersionMarker) -and ((Get-Content -LiteralPath $VersionMarker -Raw).Trim() -eq $ReleaseTag)
if (-not $alreadyStaged) {
    # A stale bundle from an older release may already be sitting here (e.g. a
    # recipient re-running install.cmd after a new version was published) --
    # remove it first so old and new files never mix.
    foreach ($old in @($PythonDir, $WheelsDir, $ModelDir, $VersionMarker)) {
        if (Test-Path -LiteralPath $old) {
            Write-Step "Removing older bundle contents at '$old'..."
            Remove-Item -LiteralPath $old -Recurse -Force
        }
    }

    if (-not (Test-Path -LiteralPath $BundleZip)) {
        $bundleUrl = "https://github.com/$RepoSlug/releases/download/$ReleaseTag/$BundleAssetName"
        Write-Step "Downloading the offline install bundle (one-time, ~550MB)..."
        Write-Host "    $bundleUrl"
        try {
            Invoke-WebRequest -Uri $bundleUrl -OutFile $BundleZip -UseBasicParsing
        } catch {
            Fail "Could not download the install bundle. Check your internet connection and try again. ($($_.Exception.Message))"
        }
    }

    Write-Step "Verifying bundle checksum..."
    $sumsUrl  = "https://github.com/$RepoSlug/releases/download/$ReleaseTag/$ChecksumAssetName"
    $sumsPath = Join-Path $InstallRoot $ChecksumAssetName
    try {
        Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsPath -UseBasicParsing
    } catch {
        Fail "Could not download the checksum file. ($($_.Exception.Message))"
    }
    $expectedLine = Get-Content -LiteralPath $sumsPath | Where-Object { $_ -match [regex]::Escape($BundleAssetName) } | Select-Object -First 1
    if (-not $expectedLine) {
        Fail "Checksum file did not list '$BundleAssetName' - cannot verify the download."
    }
    $expected = ($expectedLine -split '\s+')[0].Trim().ToLower()
    $actual   = (Get-FileHash -LiteralPath $BundleZip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Fail "Bundle checksum mismatch (expected $expected, got $actual). The download may be corrupted - delete '$BundleZip' and run install.cmd again."
    }

    Write-Step "Extracting offline bundle (this can take a minute)..."
    Expand-Archive -LiteralPath $BundleZip -DestinationPath $InstallRoot -Force
}

if (-not (Test-Path -LiteralPath $PythonDir)) { Fail "Bundle extraction did not produce a 'python' folder under '$InstallRoot'." }
if (-not (Test-Path -LiteralPath $WheelsDir)) { Fail "Bundle extraction did not produce a 'wheels' folder under '$InstallRoot'." }
if (-not (Test-Path -LiteralPath $ModelDir))  { Fail "Bundle extraction did not produce a 'model' folder under '$InstallRoot'." }

# Only now that python/wheels/model are confirmed present: record the version
# staged, and remove the downloaded zip + checksum file -- keeping them
# around would silently double the install's disk footprint for no reason.
Set-Content -LiteralPath $VersionMarker -Value $ReleaseTag -NoNewline
if (Test-Path -LiteralPath $BundleZip) {
    Remove-Item -LiteralPath $BundleZip -Force
    Remove-Item -LiteralPath (Join-Path $InstallRoot $ChecksumAssetName) -Force -ErrorAction SilentlyContinue
}

# --- Step 2: enable site-packages in the embeddable Python, and make the
# whispr/ package (which lives in $InstallRoot, one level above python\)
# importable. ._pth paths resolve relative to the ._pth file's OWN directory,
# not the process's working directory -- "-m whispr" would otherwise fail
# with "No module named whispr" regardless of cwd. Both edits are idempotent.
$PthFile = Get-ChildItem -LiteralPath $PythonDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $PthFile) { Fail "Could not find the embeddable Python's ._pth file under '$PythonDir'." }
$pthLines = Get-Content -LiteralPath $PthFile.FullName
$pthLines = $pthLines -replace '^\s*#\s*import\s+site\s*$', 'import site'
if (-not ($pthLines -contains '..')) { $pthLines += '..' }
Set-Content -LiteralPath $PthFile.FullName -Value $pthLines

$PythonExe = Join-Path $PythonDir 'python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) { Fail "Bundled python.exe missing at '$PythonExe'." }

# Never consult a per-user site-packages directory that might exist from some
# unrelated Python install on this machine -- the whole point of bundling an
# embeddable Python is isolation from whatever else is installed. Must be set
# for every python.exe invocation below AND at runtime (see run-whispr.cmd).
$env:PYTHONNOUSERSITE = '1'

# --- Step 3: bootstrap pip fully offline ---
Write-Step "Setting up the Python environment (offline, no internet needed from here)..."
& "$PythonExe" "$GetPip" --no-index --find-links "$WheelsDir"
if ($LASTEXITCODE -ne 0) { Fail "pip bootstrap failed (exit $LASTEXITCODE)." }

# --- Step 4: install whispr's pinned dependencies, fully offline ---
Write-Step "Installing whispr's dependencies..."
& "$PythonExe" -m pip install --no-index --find-links "$WheelsDir" -r (Join-Path $InstallRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed (exit $LASTEXITCODE)." }

# --- Step 5: hand off to the Python-side installer ---
Write-Step "Continuing setup (this part asks a couple of questions)..."
& "$PythonExe" (Join-Path $InstallRoot 'scripts\run_installer.py') --install-root "$InstallRoot"
exit $LASTEXITCODE
