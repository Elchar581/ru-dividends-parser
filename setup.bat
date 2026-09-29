@echo off
chcp 65001 >nul
REM ============================================================
REM  Установка окружения парсера дивидендов.
REM  Запускается ОДИН раз после клонирования репозитория.
REM  Создаёт .venv рядом с проектом и ставит туда зависимости
REM  зафиксированных версий из requirements.txt.
REM ============================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo === Проверка Python ===
python --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: python не найден в PATH.
    echo Установите Python 3.10 или новее с https://www.python.org/downloads/
    echo При установке обязательно отметьте "Add python.exe to PATH".
    exit /b 1
)
python --version

REM Проверяем версию: нужен 3.10+ (в коде используется синтаксис "X | None")
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ОШИБКА: нужен Python 3.10 или новее.
    exit /b 1
)

echo.
echo === Создание виртуального окружения .venv ===
if exist ".venv\Scripts\python.exe" (
    echo Окружение уже есть, пересоздание не требуется.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ОШИБКА: не удалось создать виртуальное окружение.
        exit /b 1
    )
)

echo.
echo === Установка зависимостей ===
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ОШИБКА: не удалось поставить зависимости.
    echo Если в тексте ошибки есть SSL или CERTIFICATE — мешает корпоративный
    echo прокси. Обходной путь:
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt ^
    echo         --trusted-host pypi.org --trusted-host files.pythonhosted.org
    exit /b 1
)

echo.
echo === Проверка сборки ===
".venv\Scripts\python.exe" tests\test_parsing.py
if errorlevel 1 (
    echo ОШИБКА: тесты разбора не прошли, окружение собрано некорректно.
    exit /b 1
)

echo.
echo ============================================================
echo  Готово. Запуск парсера:
echo      run_daily.bat
echo  или вручную:
echo      .venv\Scripts\python.exe run.py
echo ============================================================
endlocal
