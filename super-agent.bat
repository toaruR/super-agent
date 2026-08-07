@echo off
rem super-agent.bat — thin wrapper: `python -m harness.cli` from the local venv.
rem Usage:  super-agent <subcommand> [args]
rem   e.g.  super-agent integrate --task T1 --tasks ./probe/sample/my-design-tasks.md
rem
rem cd's into src/ (where .cve-venv lives) then calls python.exe, which resolves to
rem .cve-venv\Scripts\python.exe when that dir is on PATH / current. If not, set PATH
rem or call super-agent.bat from within the venv-activated shell.

setlocal
cd /d "%~dp0" || exit /b 1
python.exe -m harness.cli %*
endlocal
