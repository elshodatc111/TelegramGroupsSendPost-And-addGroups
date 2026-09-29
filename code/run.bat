@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Telegram Group Post

rem ---- 1. Python topish ----
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython

rem ---- 2. Virtual muhit (faqat birinchi marta) ----
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Virtual muhit yaratilmoqda...
    %PY% -m venv .venv
    if errorlevel 1 goto fail
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3. Kutubxonalar (faqat birinchi marta yoki requirements o'zgarganda) ----
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
    echo [2/3] Kutubxonalar o'rnatilmoqda, biroz kuting...
    "%VPY%" -m pip install --upgrade pip -q
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 goto fail
    copy /y requirements.txt .venv\requirements.installed >nul
)

rem ---- 4. Ishga tushirish (brauzer avtomatik ochiladi) ----
echo [3/3] Dastur ishga tushmoqda...
"%VPY%" -m app
if errorlevel 1 goto fail
goto end

:nopython
echo.
echo Python topilmadi. Avtomatik o'rnatishga urinilmoqda (winget)...
where winget >nul 2>nul
if errorlevel 1 goto manualpython
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Python o'rnatildi. Shu run.bat faylini QAYTA ishga tushiring.
pause
exit /b 0

:manualpython
echo.
echo Python 3.10 yoki yangisini o'rnating: https://www.python.org/downloads/
echo O'rnatishda "Add python.exe to PATH" katagini belgilang, keyin run.bat ni qayta oching.
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo XATO yuz berdi. Yuqoridagi xabarni o'qing.
pause
exit /b 1

:end
pause
