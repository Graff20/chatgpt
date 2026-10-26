@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv-build
if errorlevel 1 exit /b 1
.venv-build\Scripts\python -m pip install --disable-pip-version-check pyinstaller==6.16.0
if errorlevel 1 exit /b 1
.venv-build\Scripts\python -m PyInstaller --noconfirm --clean --onedir --additional-hooks-dir windows/hooks --name Ceh --add-data "static;static" app.py
if errorlevel 1 exit /b 1
echo Build: dist\Ceh\Ceh.exe --open
echo Copy the ENTIRE dist\Ceh folder to the Windows workstation.
pause
