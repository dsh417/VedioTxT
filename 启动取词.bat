@echo off
cd /d "%~dp0"
title Subtitle Word Grabber

rem ---- Pick interpreter: bundled .\python first, then py launcher, then PATH ----
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=py"
if "%PY%"=="py" (where py >nul 2>nul || set "PY=python")

%PY% --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found. Install Python 3.10+ with "Add to PATH",
    echo        or restore the bundled .\python\ folder.
    echo.
    pause
    exit /b 1
)

rem ---- Check core deps, auto-install if missing ----
%PY% -c "import PIL, numpy, rapidocr_onnxruntime" 2>nul
if errorlevel 1 (
    echo.
    echo [INFO] Missing dependencies, installing...
    echo        pip install -r requirements.txt
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Install failed. Run manually:  %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

rem ---- Prefer windowed interpreter so no console stays open ----
set "PYW="
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if not defined PYW (where pythonw >nul 2>nul && set "PYW=pythonw")
if not defined PYW (where pyw >nul 2>nul && set "PYW=pyw")
if not defined PYW set "PYW=%PY%"

echo Starting word grabber...
start "" "%PYW%" "video_word_grabber.py"
exit /b 0
