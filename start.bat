@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.11 or newer from python.org, then run start.bat again.
  pause
  exit /b 1
)
py -3 app.py --open %*
pause
