@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 demo.py
py -3 app.py --data data/demo --port 8766 --open
pause
