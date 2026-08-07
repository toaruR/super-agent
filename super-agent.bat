@echo off
rem super-agent.bat — thin wrapper around `python -m harness.cli`.
rem Lets you run:  super-agent <subcommand> [args]
rem e.g.  super-agent integrate --task T1 --tasks ./probe/sample/my-design-tasks.md
rem
rem Lives in src/ (next to the .cve-venv and the harness package).
rem Prefers the local virtualenv python, falls back to `python` on PATH.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

if exist ".cve-venv\Scripts\python.exe" (
    set "PY=.cve-venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m harness.cli %*
endlocal
