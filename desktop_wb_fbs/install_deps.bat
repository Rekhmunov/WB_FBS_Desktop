@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title FeedPilot FBS — установка зависимостей
echo Установка Python-пакетов для FeedPilot FBS...
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Ошибка установки. Проверьте, что Python в PATH.
  pause
  exit /b 1
)
echo.
echo Готово. Запустите "FeedPilot FBS.vbs" или "FeedPilot FBS.bat".
pause
