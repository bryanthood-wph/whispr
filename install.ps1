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

$BundleZip = Join-Path $InstallRoot $BundleAssetName
$PythonDir = Join-Path $InstallRoot 'python'
$WheelsDir = Join-Path $InstallRoot 'wheels'
$ModelDir  = Join-Path $InstallRoot 'model'
$GetPip    = Join-Path $InstallRoot 'get-pip.py'

# --- Step 1: obtain the offline bundle (skip entirely if already staged) ---
$alreadyStaged = (Test-Path -LiteralPath $PythonDir) -and (Test-Path -LiteralPath $WheelsDir) -and (Test-Path -LiteralPath $ModelDir)
if (-not $alreadyStaged) {
    if (-not (Test-Path -LiteralPath $BundleZip)) {
        $bundleUrl = "https://github.com/$RepoSlug/releases/download/$ReleaseTag/$BundleAssetName"
        Write-Step "Downloading the offline install bundle (one-time, ~1GB)..."
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

# --- Step 2: enable site-packages in the embeddable Python ---
$PthFile = Get-ChildItem -LiteralPath $PythonDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $PthFile) { Fail "Could not find the embeddable Python's ._pth file under '$PythonDir'." }
(Get-Content -LiteralPath $PthFile.FullName) -replace '^\s*#\s*import\s+site\s*$', 'import site' |
    Set-Content -LiteralPath $PthFile.FullName

$PythonExe = Join-Path $PythonDir 'python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) { Fail "Bundled python.exe missing at '$PythonExe'." }

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
