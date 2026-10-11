@echo off
setlocal
title DSE News
set "APP=%LOCALAPPDATA%\DSE-News"
set "ZIP=%TEMP%\dse-news.zip"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Trying to install it with winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo If Python was installed, CLOSE this window and run this file again.
    echo Otherwise install Python 3.10+ from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
)

echo Downloading the latest DSE News app...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing 'https://codeload.github.com/Abidhr-01/dse-data-scrape/zip/refs/heads/main' -OutFile '%ZIP%'; if (Test-Path '%APP%\src') { Remove-Item -Recurse -Force '%APP%\src' }; Expand-Archive -Force '%ZIP%' '%APP%\tmp'; Move-Item '%APP%\tmp\dse-data-scrape-main' '%APP%\src'; Remove-Item -Recurse -Force '%APP%\tmp' } catch { exit 1 }"
if errorlevel 1 (
    if not exist "%APP%\src\app.py" (
        echo Download failed. Check your internet connection.
        pause
        exit /b 1
    )
    echo Could not update; using the previously downloaded version.
)

cd /d "%APP%\src"
if not exist "%APP%\.setup_done" (
    echo First-time setup: installing Playwright and Chromium ^(a few minutes^)...
    python -m pip install playwright || (pause & exit /b 1)
    python -m playwright install chromium || (pause & exit /b 1)
    echo done> "%APP%\.setup_done"
)

start "" http://127.0.0.1:8000
python app.py --output "%USERPROFILE%\Documents\dse_news"
pause
