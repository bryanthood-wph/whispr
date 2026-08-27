@echo off
rem Launches whispr using an isolated Python -- never the machine's own Python
rem if one happens to be installed separately.
rem
rem Two supported layouts, probed in this order:
rem   .venv\Scripts\pythonw.exe  -- development checkout (a working copy of this
rem                                 repo, where install.ps1 was never run)
rem   python\pythonw.exe         -- installed distribution (the embeddable
rem                                 Python bundled by install.ps1)
rem
rem Dev is probed FIRST, and the order is load-bearing in one direction only: an
rem installed distribution never contains a .venv, so preferring it cannot
rem mis-route an installed copy. The reverse was not safe -- hardcoding the
rem distribution path made this script silently unusable in the dev checkout,
rem where python\ does not exist (found 2026-08-26 while wiring the recorder
rem watchdog, which would have inherited the same dead path).
rem
rem PYTHONNOUSERSITE=1 is set for BOTH layouts. It is load-bearing for the
rem embeddable distribution: enabling `import site` (required for pip and
rem site-packages to work at all) also enables site.ENABLE_USER_SITE, which
rem would pull in any pre-existing per-user Python install on the recipient's
rem machine and defeat the point of bundling an isolated interpreter -- see
rem CLAUDE.md, "Embeddable Python isolation". For a venv it is a harmless no-op
rem (venvs already disable user site-packages), so one setting covers both
rem branches instead of them diverging.
setlocal
set PYTHONNOUSERSITE=1

rem Anchor the working directory to the repo root. `-m whispr` resolves the
rem package from the CURRENT directory, so launching this script from anywhere
rem else started pythonw.exe, which died instantly on "No module named whispr"
rem while `start` still reported exit 0 -- a silent no-op (found 2026-08-27).
rem /d is required for the drive to change too. pushd rather than cd so the
rem caller's directory is restored on exit, and so a UNC path is mapped to a
rem temporary drive letter instead of failing outright.
pushd "%~dp0" || (
    echo ERROR: cannot enter "%~dp0" -- whispr not launched.
    exit /b 1
)

if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" -m whispr
    popd
    exit /b 0
)

if exist "%~dp0python\pythonw.exe" (
    start "" "%~dp0python\pythonw.exe" -m whispr
    popd
    exit /b 0
)

rem Both probes missed. Nothing is launched, and this is NOT recoverable by
rem retrying, so fail loudly with the paths that were tried. Invisible when
rem launched from a shortcut (no console), but the exit code still propagates.
echo ERROR: no whispr Python interpreter found. Tried:
echo   "%~dp0.venv\Scripts\pythonw.exe"   (development checkout)
echo   "%~dp0python\pythonw.exe"          (installed distribution)
echo Run install.ps1, or create the venv, before launching whispr.
popd
exit /b 1
