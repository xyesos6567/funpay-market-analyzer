@echo off
setlocal

rem Запуск анализатора рынка FunPay одной командой.
rem При первом запуске создаёт виртуальное окружение и ставит зависимости.
rem ВАЖНО: файл сохранён в кодировке cp866. Не пересохраняйте его в UTF-8 --
rem cmd.exe читает пакетные файлы в OEM-кодировке, и кириллица в UTF-8
rem ломает разбор строк.

cd /d "%~dp0"

rem --- Поиск интерпретатора Python ---------------------------------------
rem Порядок: лаунчер py, затем python из PATH, затем типовой путь установки.
rem Запасной путь нужен потому, что после установки Python переменные PATH
rem обновляются только в новых окнах, и в текущем его может не быть.
set "PY="
py -3.12 -c "pass" >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"

if not defined PY (
    python -c "pass" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    )
)

if not defined PY (
    echo Python 3.12 не найден. Установите его командой:
    echo   winget install --id Python.Python.3.12 --exact --source winget
    pause
    exit /b 1
)

rem --- Окружение и зависимости -------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Создаю виртуальное окружение...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Не удалось создать окружение.
        pause
        exit /b 1
    )
    echo [2/3] Устанавливаю зависимости...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить зависимости.
        pause
        exit /b 1
    )
)

echo [3/3] Запускаю анализ рынка...
echo.
".venv\Scripts\python.exe" -m analyzer run %*

if errorlevel 1 (
    echo.
    echo Анализ завершился с ошибкой. Подробности выше.
    pause
    exit /b 1
)

echo.
echo Готово. Открываю отчёт...
start "" "out\report.html"
pause
