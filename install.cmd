@echo off
setlocal
cd /d "%~dp0"

echo Preparing files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse | Unblock-File" >nul 2>&1

echo Starting whispr installer - this can take a few minutes on first run...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
    echo ==================================================
    echo  Setup did not finish successfully - see messages above.
    echo  You can safely run install.cmd again after fixing
    echo  whatever it reported.
    echo ==================================================
) else (
    echo ==================================================
    echo  Setup finished - see the messages above for details.
    echo ==================================================
)
echo.
pause
