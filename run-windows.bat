@echo off
rem wxbrief on Windows. Double-click to start. Requires Python 3.10+ from python.org.
cd /d "%~dp0content"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -u app.py
) else (
    python -u app.py
)
pause
