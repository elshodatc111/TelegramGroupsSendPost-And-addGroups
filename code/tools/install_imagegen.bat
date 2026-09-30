@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
title Lokal rasm yaratish o'rnatilmoqda
echo.
echo  Lokal rasm yaratish (PyTorch + diffusers) o'rnatilmoqda. Video karta: NVIDIA GTX/RTX.
echo  Hajmi ~3 GB, model esa birinchi ishlatishda yuklanadi (2-7 GB). Bir marta.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo .venv topilmadi. Avval run.bat ni bir marta ishga tushiring.
    pause & exit /b 1
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" --version
echo.
call :trytorch
if not errorlevel 1 goto torch_ok

echo.
echo  Bu Python versiyasi uchun PyTorch topilmadi. Python 3.12 bilan yangi muhit yaratiladi.
echo  MUHIM: run.bat (dastur) oynasi ochiq bo'lsa, avval uni YOPING, keyin bu faylni qayta bosing.
echo.
py -3.12 --version >nul 2>nul
if errorlevel 1 (
    echo  Python 3.12 o'rnatilmoqda ^(winget^)...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    py -3.12 --version >nul 2>nul
    if errorlevel 1 goto nopy312
)
if exist ".venv_old" rmdir /s /q ".venv_old"
ren ".venv" ".venv_old"
if errorlevel 1 goto locked
py -3.12 -m venv .venv
if errorlevel 1 goto restore
"%VPY%" -m pip install --upgrade pip -q
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto restore
copy /y requirements.txt .venv\requirements.installed >nul
call :trytorch
if errorlevel 1 goto fail
goto torch_ok

:torch_ok
"%VPY%" -m pip install --upgrade diffusers transformers accelerate safetensors pillow
if errorlevel 1 goto fail
"%VPY%" -c "import torch;print();print(' CUDA:',torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
echo.
echo  Tayyor. Dasturni yopib, run.bat ni QAYTA ishga tushiring.
if exist ".venv_old" echo  (Eski muhit .venv_old papkasida; hammasi ishlasa o'chirib tashlashingiz mumkin.)
pause & exit /b 0

:trytorch
for %%c in (cu128 cu126 cu124 cu121) do (
    echo  PyTorch %%c varianti sinab ko'rilmoqda...
    "%VPY%" -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/%%c
    if not errorlevel 1 exit /b 0
)
exit /b 1

:nopy312
echo.
echo  Python 3.12 o'rnatilmadi. Qo'lda o'rnating: https://www.python.org/downloads/release/python-3120/
echo  ("Add python.exe to PATH" va "py launcher" belgilangan bo'lsin), keyin bu faylni qayta bosing.
pause & exit /b 1

:locked
echo.
echo  .venv papkasini almashtirib bo'lmadi: dastur hali ishlayapti. run.bat oynasini yoping va qayta urinib ko'ring.
pause & exit /b 1

:restore
echo.
echo  Yangi muhit yaratishda xato. Eski muhit tiklanmoqda...
if exist ".venv" rmdir /s /q ".venv"
if exist ".venv_old" ren ".venv_old" ".venv"
goto fail

:fail
echo.
echo  O'rnatishda xato chiqdi. Internet/VPN ni tekshirib, yuqoridagi xato matni bilan menga yuboring.
pause & exit /b 1
