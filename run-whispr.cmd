@echo off
rem Launches whispr using its bundled, isolated Python -- never the machine's
rem own Python if one happens to be installed separately. This is what the
rem Startup-folder shortcut points at; also useful for a manual start.
set PYTHONNOUSERSITE=1
start "" "%~dp0python\pythonw.exe" -m whispr
