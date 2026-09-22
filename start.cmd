@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import playwright, rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
if not exist "config.json" copy "config.example.json" "config.json" >nul
".venv\Scripts\python.exe" -X utf8 study_assistant.py %*
pause
exit /b
:fail
echo Setup failed. Install Python 3.10+ and check your network connection.
pause
exit /b 1
