@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Set up the Qt environment using the Windows steps in README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m xsync.desktop %*
if errorlevel 1 pause
