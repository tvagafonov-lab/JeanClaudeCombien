@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Claude Monitor — Install

echo.
echo  ◆ Claude Monitor — Установка
echo  ================================
echo.

:: ── Проверка Python ───────────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo  [ОШИБКА] Python не найден.
    echo.
    echo  Скачай Python 3.10+ с https://python.org/downloads/
    echo  При установке ОБЯЗАТЕЛЬНО поставь галку:
    echo    [x] Add Python to PATH
    echo.
    pause
    exit /b 1
)

:: Проверка версии >= 3.10
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)
if !PY_MAJOR! LSS 3 goto :bad_ver
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 10 goto :bad_ver
echo  Python !PY_VER! OK
goto :deps

:bad_ver
echo  [ОШИБКА] Нужен Python 3.10+. У тебя: !PY_VER!
echo  Скачай с https://python.org/downloads/
pause
exit /b 1

:: ── Установка зависимостей ────────────────────────────────────────────────────
:deps
echo.
echo  Устанавливаю зависимости...
python -m pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo  [ОШИБКА] Ошибка pip. Попробуй вручную: pip install cloudscraper requests
    pause
    exit /b 1
)
echo  Зависимости установлены.

:: ── Ярлык автозапуска ─────────────────────────────────────────────────────────
echo.
echo  Создаю ярлык автозапуска...

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SCRIPT=%~dp0claude_monitor.py
:: Убираем завершающий \ у dp0, иначе ярлык сломается
set WORKDIR=%~dp0
if "!WORKDIR:~-1!"=="\" set WORKDIR=!WORKDIR:~0,-1!

:: Ищем pythonw.exe рядом с python.exe
for /f "delims=" %%p in ('where python') do (
    set PYTHONDIR=%%~dpp
    goto :found_python
)
:found_python
set PYTHONW=!PYTHONDIR!pythonw.exe
if not exist "!PYTHONW!" set PYTHONW=python.exe

:: Создаём ярлык через PowerShell (одна строка, надёжно)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('!STARTUP!\ClaudeMonitor.lnk'); $s.TargetPath='!PYTHONW!'; $s.Arguments='\"!SCRIPT!\"'; $s.WorkingDirectory='!WORKDIR!'; $s.WindowStyle=7; $s.Save()"

if errorlevel 1 (
    echo  [Предупреждение] Ярлык создать не удалось — создай вручную.
) else (
    echo  Ярлык автозапуска создан.
)

:: ── Запуск Setup ──────────────────────────────────────────────────────────────
echo.
echo  Открываю окно настройки sessionKey...
echo.
python "%~dp0setup.py"

echo.
echo  ◆ Готово!
echo    - При следующем входе в Windows Claude Monitor запустится автоматически.
echo    - Запустить сейчас: дважды кликни start_monitor.bat
echo.
pause
