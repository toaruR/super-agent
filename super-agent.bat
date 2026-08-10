@echo off
rem super-agent.bat — thin wrapper: `python -m harness.cli` from the local venv.
rem Usage:  super-agent <subcommand> [args]
rem   e.g.  super-agent integrate --task T1 --task_file ./probe/sample/my-design-tasks.md
rem
rem Operates on the CALLER's current directory (the target repo), not on
rem src/ where this script lives. harness/ (this script's dir, %~dp0) is
rem resolved via PYTHONPATH instead of `cd`-ing there, so `git worktree` etc.
rem run against whatever repo you invoked super-agent from.

setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%.cve-venv\Scripts\python.exe" (
    set "PY=%SCRIPT_DIR%.cve-venv\Scripts\python.exe"
) else (
    set "PY=python.exe"
)
set "PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%"
"%PY%" -m harness.cli %*
endlocal
