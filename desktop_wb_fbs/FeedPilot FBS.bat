@echo off
cd /d "%~dp0"
title FeedPilot FBS
python run.py
if errorlevel 1 (
  echo.
  echo Ошибка запуска. Проверьте, что установлен Python и зависимости:
  echo   python -m pip install -r requirements.txt
  echo.
  pause
)
