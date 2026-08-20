@echo off
cd /d "%~dp0"
REM Daily launch without a black console (pythonw).
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" /b pythonw run.py
  exit /b 0
)

REM Fallback: console mode when pythonw is missing (shows errors).
title FeedPilot FBS
python run.py
if errorlevel 1 (
  echo.
  echo Ошибка запуска. Проверьте, что установлен Python и зависимости:
  echo   python -m pip install -r requirements.txt
  echo.
  pause
)
