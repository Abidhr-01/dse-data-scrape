@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is required. Install it from https://www.python.org/downloads/ & pause & exit /b 1)

if not exist ".setup_done" (
    echo First-time setup: installing Playwright and Chromium...
    python -m pip install playwright || (pause & exit /b 1)
    python -m playwright install chromium || (pause & exit /b 1)
    echo done> .setup_done
)

start "" http://127.0.0.1:8000
python app.py
pause
