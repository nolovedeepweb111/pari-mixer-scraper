@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run setup first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

echo Starting PARI Mixer Cup app on http://127.0.0.1:5000
echo Press Ctrl+C to stop.
".venv\Scripts\python.exe" app.py

pause
