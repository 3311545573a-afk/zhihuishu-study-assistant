@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run start.cmd first to set up the environment.
  pause
  exit /b 1
)
echo Starting the web console on http://127.0.0.1:8765/
echo Press Ctrl+C here (or use the Stop button) to stop the console and the assistant it started.
echo.
".venv\Scripts\python.exe" -X utf8 web_console.py %*
echo.
pause
exit /b
