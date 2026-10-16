@echo off
rem ============================================================================
rem  HEMS night auto task (run by Windows Task Scheduler)
rem  - Works in a separate git worktree (C:\hems-night) on branch night-auto;
rem    the main working folder C:\hems and branch main are never modified.
rem  - Claude Code runs with an allow-list only (project files, pytest, git);
rem    no permission-skipping options are used.
rem  - Hard time limit: TIMEOUT_MIN minutes, then the process tree is killed.
rem  Usage:  night_auto.bat            (real run)
rem          night_auto.bat --dry-run  (startup test only, changes nothing)
rem  Details: scripts\night\README.md
rem ============================================================================
setlocal EnableExtensions
chcp 65001 >nul

set "PROJECT=C:\hems"
set "WORKTREE=C:\hems-night"
set "BRANCH=night-auto"
set "TIMEOUT_MIN=90"
set "PROMPT=%PROJECT%\scripts\night\night_prompt.md"
set "REPORT=%WORKTREE%\docs\night_report.md"
if /i "%~1"=="--dry-run" (
    set "TIMEOUT_MIN=5"
    set "PROMPT=%PROJECT%\scripts\night\dryrun_prompt.md"
    set "REPORT="
)

set "LOGDIR=%PROJECT%\output\night"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%i"
set "LOG=%LOGDIR%\night_%STAMP%.log"
echo [%date% %time%] night_auto start (timeout %TIMEOUT_MIN% min) > "%LOG%.bat.txt"

rem --- 1. create branch night-auto from main (only if it does not exist) ---
cd /d "%PROJECT%" || goto :fail
git show-ref --verify --quiet refs/heads/%BRANCH%
if errorlevel 1 (
    git branch %BRANCH% main >> "%LOG%.bat.txt" 2>&1 || goto :fail
)

rem --- 2. separate worktree folder for night-auto ---
if not exist "%WORKTREE%\.git" (
    git worktree add "%WORKTREE%" %BRANCH% >> "%LOG%.bat.txt" 2>&1 || goto :fail
)
cd /d "%WORKTREE%" || goto :fail
git rev-parse --abbrev-ref HEAD | findstr /x "%BRANCH%" >nul || goto :fail

rem --- 3. bring in the latest main (fast-forward only; never rewrites history) ---
git merge --ff-only main >> "%LOG%.bat.txt" 2>&1
if errorlevel 1 echo night-auto has its own commits, keep it as is >> "%LOG%.bat.txt"

rem --- 4. use the project's virtual environment for python / pytest ---
set "PATH=%PROJECT%\.venv\Scripts;%PATH%"

rem --- 5. run Claude Code with the time limit ---
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%\scripts\night\run_claude.ps1" ^
    -Minutes %TIMEOUT_MIN% -PromptFile "%PROMPT%" -WorkDir "%WORKTREE%" -Log "%LOG%" -ReportPath "%REPORT%"
set "RC=%ERRORLEVEL%"
echo [%date% %time%] night_auto end, exit code %RC% >> "%LOG%.bat.txt"
exit /b %RC%

:fail
echo [%date% %time%] night_auto FAILED during setup >> "%LOG%.bat.txt"
exit /b 1
