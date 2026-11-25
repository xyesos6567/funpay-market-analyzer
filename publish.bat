@echo off
setlocal enabledelayedexpansion

rem Публикация проекта на GitHub и включение GitHub Pages.
rem Запускать только после: setup_github.bat и gh auth login
rem ВАЖНО: файл в кодировке cp866, не пересохраняйте в UTF-8.

cd /d "%~dp0"

set "REPO_NAME=funpay-market-analyzer"

where gh >nul 2>nul
if errorlevel 1 (
    echo GitHub CLI не найден. Сначала запустите setup_github.bat
    pause
    exit /b 1
)

gh auth status >nul 2>nul
if errorlevel 1 (
    echo === Вход в GitHub ===
    echo.
    echo Сейчас откроется браузер. Что делать:
    echo   1. Нажмите Continue with GitHub
    echo   2. Скопируйте код, который появится ниже, и вставьте его в браузер
    echo   3. Разрешите доступ
    echo.
    gh auth login --hostname github.com --git-protocol https --web
    if errorlevel 1 (
        echo.
        echo Вход не выполнен. Запустите publish.bat заново и завершите вход.
        pause
        exit /b 1
    )
    echo.
    echo Вход выполнен.
    echo.
)

echo === Публикую репозиторий ===
echo.

gh repo view >nul 2>nul
if errorlevel 1 (
    echo Создаю репозиторий %REPO_NAME% и отправляю код...
    gh repo create %REPO_NAME% --public --source=. --remote=origin --push
    if errorlevel 1 (
        echo Не удалось создать репозиторий. Возможно, имя занято.
        echo Поменяйте REPO_NAME в начале этого файла и попробуйте снова.
        pause
        exit /b 1
    )
) else (
    echo Репозиторий уже связан, отправляю изменения...
    git add -A
    git commit -m "Обновление" >nul 2>nul
    git push -u origin main
    if errorlevel 1 (
        echo Не удалось отправить код.
        pause
        exit /b 1
    )
)

echo.
echo === Включаю GitHub Pages ===
echo.

for /f "delims=" %%a in ('gh repo view --json nameWithOwner -q .nameWithOwner') do set "OWNER_REPO=%%a"

rem Из "владелец/репозиторий" берём только владельца -- он нужен для адреса страницы.
for /f "tokens=1 delims=/" %%a in ("!OWNER_REPO!") do set "OWNER=%%a"

rem Pages может быть уже включён -- тогда GitHub вернёт ошибку, это нормально.
gh api --method POST "repos/!OWNER_REPO!/pages" -f build_type=workflow >nul 2>nul
gh api --method PUT "repos/!OWNER_REPO!/pages" -f build_type=workflow >nul 2>nul

echo.
echo === Готово ===
echo.
echo Отчёт: https://!OWNER!.github.io/%REPO_NAME%/
echo Запуск обновления: https://github.com/!OWNER_REPO!/actions/workflows/analyze.yml
echo.
echo Первый прогон начните вручную: откройте ссылку "Запуск обновления"
echo и нажмите "Run workflow". Через 1-2 минуты страница отчёта обновится.
echo.
pause
