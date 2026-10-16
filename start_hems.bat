@echo off
rem Double-click to start the HEMS desktop app (uses the project's virtual environment).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-desktop.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run_app.py %*
if errorlevel 1 pause
