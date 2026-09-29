@echo off
chcp 65001 >nul
REM ============================================================
REM  Ежедневный прогон парсера дивидендов.
REM  Ставится в Планировщик заданий Windows: см. README.md,
REM  раздел "Работа по расписанию".
REM  Перед первым запуском выполните setup.bat.
REM ============================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: виртуальное окружение не найдено.
    echo Выполните setup.bat — он создаст .venv и поставит зависимости.
    exit /b 1
)

REM Явно вызываем python из .venv: планировщик запускается с чужим PATH,
REM и полагаться на "активацию" окружения здесь нельзя.
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 (
    echo.
    echo Прогон завершился с ошибкой. Подробности: output\run.log
    exit /b 1
)
endlocal
