<#
.SYNOPSIS
  Author-side packaging: builds the offline install bundle (embeddable Python
  + wheels for every pinned dependency + the faster-whisper "small" model)
  that install.ps1 downloads on a recipient's machine. Run this on Colby's own
  machine, which has network access and the working .venv/HF cache that are
  this bundle's source of truth -- never run it on a recipient's machine.

.NOTES
  Output: dist\whispr-offline-bundle.zip + dist\SHA256SUMS.txt, ready to
  attach to a GitHub Release (tag must match install.ps1's $ReleaseTag).
  dist\ is gitignored -- this is never committed as a git blob.
#>

$ErrorActionPreference = 'Stop'

# Expected SHA256 of the embeddable Python zip and get-pip.py. Neither
# python.org nor bootstrap.pypa.io serve an official checksum for these
# directly, so this is a trust-on-first-use pin rather than independent
# verification: the first build after this script starts populates these
# values (printed to the console -- copy them in below), and every build
# after that fails loudly if what gets downloaded doesn't match, instead of
# silently folding a changed file into the bundle. If you deliberately bump
# the Python version, clear the value, run once to see the new hash, then
# pin it again.
$ExpectedEmbedHash  = 'BA6BD811C4EEDB19195CF275770EF127E893D63701E24152606E2CB76F6D876A'
$ExpectedGetPipHash = 'A341E1A43E38001C551A1508A73FF23636A11970B61D901D9A1CAD2A18F57055'

function Test-PinnedHash {
    param([string]$FilePath, [string]$Expected, [string]$Label)
    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash
    if (-not $Expected) {
        Write-Host "  NOTE: no pinned hash set for $Label yet -- observed SHA256: $actual" -ForegroundColor Yellow
        Write-Host "        Pin it in build-dist.ps1 (`$Expected*Hash) once you've reviewed it." -ForegroundColor Yellow
        return
    }
    if ($actual.ToUpper() -ne $Expected.ToUpper()) {
        Fail "$Label hash mismatch. Expected $Expected, got $actual. Either the upstream file changed or something tampered with it -- do not proceed without reviewing this."
    }
}

$RepoRoot      = Split-Path -Parent $PSScriptRoot           # C:\github\whispr
$DevPython     = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Requirements  = Join-Path $RepoRoot 'requirements.txt'
$DistDir       = Join-Path $RepoRoot 'dist'
$StageDir      = Join-Path $DistDir 'bundle-stage'
$OutputZip     = Join-Path $DistDir 'whispr-offline-bundle.zip'
$ChecksumFile  = Join-Path $DistDir 'SHA256SUMS.txt'
$ModelCacheSrc = Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--Systran--faster-whisper-small'

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Fail { param([string]$Message) Write-Host "FAILED: $Message" -ForegroundColor Red; exit 1 }

if (-not (Test-Path -LiteralPath $DevPython)) { Fail "Dev venv python not found at '$DevPython' - build this on Colby's own machine." }
if (-not (Test-Path -LiteralPath $Requirements)) { Fail "requirements.txt not found at '$Requirements' - generate it first (pip freeze)." }
if (-not (Test-Path -LiteralPath $ModelCacheSrc)) { Fail "faster-whisper 'small' model not found in the HF cache at '$ModelCacheSrc'. Run the app (or a transcription) once to populate it first." }

# Fresh stage every time - no stale leftovers from a previous build.
if (Test-Path -LiteralPath $StageDir) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
New-Item -ItemType Directory -Path $StageDir | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StageDir 'wheels') | Out-Null

# --- Step 1: download wheels for every pinned dependency, wheel-only only ---
Write-Step "Downloading wheels for all pinned dependencies (wheel-only, no source builds)..."
& $DevPython -m pip download -r $Requirements --dest (Join-Path $StageDir 'wheels') --no-deps --only-binary=:all:
if ($LASTEXITCODE -ne 0) { Fail "pip download failed for one or more pinned packages - a prebuilt wheel may not exist for this Python/platform. See output above." }

Write-Step "Downloading pip's own wheel (needed to bootstrap pip inside the embeddable Python)..."
& $DevPython -m pip download pip --dest (Join-Path $StageDir 'wheels') --no-deps --only-binary=:all:
if ($LASTEXITCODE -ne 0) { Fail "Could not download pip's own wheel." }

# --- Step 2: stage the embeddable Python distribution -----------------------
$verOut = & $DevPython --version
if ($verOut -notmatch '(\d+\.\d+\.\d+)') { Fail "Could not parse the dev venv's Python version from '$verOut'." }
$PyVersion = $Matches[1]
Write-Step "Downloading the Windows embeddable Python $PyVersion distribution..."
$embedZip = Join-Path $StageDir 'python-embed.zip'
$embedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
try {
    Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
} catch {
    Fail "Could not download embeddable Python from $embedUrl. ($($_.Exception.Message))"
}
Test-PinnedHash -FilePath $embedZip -Expected $ExpectedEmbedHash -Label 'embeddable Python zip'
$pythonDir = Join-Path $StageDir 'python'
Expand-Archive -LiteralPath $embedZip -DestinationPath $pythonDir -Force
Remove-Item -LiteralPath $embedZip -Force

Write-Step "Downloading get-pip.py..."
$getPipPath = Join-Path $StageDir 'get-pip.py'
try {
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPipPath -UseBasicParsing
} catch {
    Fail "Could not download get-pip.py. ($($_.Exception.Message))"
}
Test-PinnedHash -FilePath $getPipPath -Expected $ExpectedGetPipHash -Label 'get-pip.py'

# --- Step 3: stage the faster-whisper model ----------------------------------
# Verified empirically on this build machine (2026-07-27): huggingface_hub's
# Windows cache here stores real files under snapshots/ (LinkType blank, sizes
# match blobs exactly) rather than symlinks -- no Developer Mode / symlink
# privilege on this account. A plain recursive copy is therefore correct here.
# Still verify sizes after copying rather than trust that blindly, in case a
# future build machine's cache does use real reparse points.
Write-Step "Staging the faster-whisper 'small' model (~460MB copy)..."
$modelDestRoot = Join-Path $StageDir 'model'
$modelDest = Join-Path $modelDestRoot 'models--Systran--faster-whisper-small'
New-Item -ItemType Directory -Path $modelDestRoot -Force | Out-Null
Copy-Item -LiteralPath $ModelCacheSrc -Destination $modelDest -Recurse -Force

$srcFiles = Get-ChildItem -LiteralPath $ModelCacheSrc -Recurse -File
$mismatch = $false
foreach ($f in $srcFiles) {
    $rel = $f.FullName.Substring($ModelCacheSrc.Length).TrimStart('\')
    $destFile = Join-Path $modelDest $rel
    if (-not (Test-Path -LiteralPath $destFile)) {
        Write-Host "  MISSING: $rel" -ForegroundColor Red
        $mismatch = $true
        continue
    }
    $destSize = (Get-Item -LiteralPath $destFile).Length
    if ($destSize -ne $f.Length) {
        Write-Host "  SIZE MISMATCH: $rel (source $($f.Length) vs staged $destSize)" -ForegroundColor Red
        $mismatch = $true
    }
}
if ($mismatch) { Fail "Model staging produced files that don't match the source (broken link or truncated copy) - see above." }
Write-Step "Model staged correctly ($($srcFiles.Count) files verified byte-size-identical to source)."

# --- Step 4: zip the staged bundle (contents at archive root, not nested) ---
Write-Step "Compressing the bundle to $OutputZip ..."
if (Test-Path -LiteralPath $OutputZip) { Remove-Item -LiteralPath $OutputZip -Force }
Compress-Archive -Path (Join-Path $StageDir '*') -DestinationPath $OutputZip -CompressionLevel Optimal

# --- Step 5: checksum ---------------------------------------------------------
Write-Step "Writing checksum file..."
$hash = (Get-FileHash -LiteralPath $OutputZip -Algorithm SHA256).Hash.ToLower()
"$hash  whispr-offline-bundle.zip" | Set-Content -LiteralPath $ChecksumFile -Encoding ascii

$sizeMB = [math]::Round((Get-Item -LiteralPath $OutputZip).Length / 1MB, 1)
Write-Host ""
Write-Host "Done. Bundle: $OutputZip ($sizeMB MB)" -ForegroundColor Green
Write-Host "Checksum:  $ChecksumFile"
Write-Host ""
Write-Host "Next: attach both files to a GitHub Release on bryanthood-wph/whispr"
Write-Host "(tag must match install.ps1's `$ReleaseTag, currently 'v0.1.0')."
