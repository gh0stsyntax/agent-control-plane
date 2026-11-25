@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ wird benoetigt: https://www.python.org/downloads/
  pause
  exit /b 1
)
python app.py %*
pause
