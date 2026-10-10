@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 视频字幕取词工具

rem ---- 选择 Python 解释器(优先本目录内置 python\, 其次 py 启动器, 最后 PATH 里的 python) ----
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=py"
if "%PY%"=="py" (where py >nul 2>nul || set "PY=python")

rem ---- 确认 Python 可用 ----
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 没有找到 Python, 请先安装 Python 3.10+ 并勾选 "Add to PATH"。
    echo        下载: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- 检查核心依赖, 缺失则自动安装 ----
%PY% -c "import PIL, numpy, rapidocr_onnxruntime" 2>nul
if errorlevel 1 (
    echo.
    echo [提示] 检测到缺少运行依赖, 正在自动安装(首次需联网下载, 请稍候)...
    echo        pip install -r requirements.txt
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请手动执行:  %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

rem ---- 选择无控制台的解释器, 启动后不残留黑窗口 ----
set "PYW="
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if not defined PYW (where pythonw >nul 2>nul && set "PYW=pythonw")
if not defined PYW (where pyw >nul 2>nul && set "PYW=pyw")
if not defined PYW set "PYW=%PY%"

echo 正在启动取词工具...
start "" "%PYW%" "video_word_grabber.py"
exit /b 0
