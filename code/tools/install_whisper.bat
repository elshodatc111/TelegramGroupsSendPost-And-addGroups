@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
title Whisper o'rnatish (lokal audio -> matn)
echo.
echo  Lokal transkripsiya o'rnatilmoqda (ffmpeg + faster-whisper).
echo  Video/audio matnga aylantirish bepul va internetsiz ishlaydi.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo .venv topilmadi. Avval run.bat ni bir marta ishga tushiring.
    pause & exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [1/2] ffmpeg o'rnatilmoqda...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget topilmadi. ffmpeg ni qo'lda o'rnating: https://www.gyan.dev/ffmpeg/builds/  ^(PATH ga qo'shing^)
    ) else (
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    )
) else (
    echo [1/2] ffmpeg mavjud.
)
echo [2/2] faster-whisper va GPU kutubxonalari o'rnatilmoqda ^(GTX 1660 Super uchun^)...
".venv\Scripts\python.exe" -m pip install --upgrade faster-whisper nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
if errorlevel 1 (
    echo GPU kutubxonalari o'rnatilmadi, faqat faster-whisper bilan urinib ko'rilmoqda ^(protsessorda ishlaydi^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade faster-whisper
)
echo.
echo  Tayyor. Dasturni yopib, run.bat ni QAYTA ishga tushiring.
echo  Birinchi transkripsiyada large-v3 modeli yuklab olinadi ^(~3 GB, bir marta^).
pause
