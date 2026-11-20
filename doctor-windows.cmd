@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-windows.cmd first.
) else (
  ".venv\Scripts\python.exe" scripts\local.py doctor
)
pause
