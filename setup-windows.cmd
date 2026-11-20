@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python scripts\local.py setup
) else (
  py -3 scripts\local.py setup
)
pause
