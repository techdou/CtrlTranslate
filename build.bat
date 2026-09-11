@echo off
rem CtrlTranslate 轻量版打包（单 exe，无控制台窗口；约 70MB）
rem 不含 WebEngine（网页版引擎在此构建中禁用并给出指引）——需要网页版用 build-web.bat
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pyinstaller.exe (
    echo [build] installing pyinstaller...
    .venv\Scripts\pip install pyinstaller || goto :fail
)

echo [build] cleaning old output...
if exist dist\CtrlTranslate.exe del dist\CtrlTranslate.exe

echo [build] building CtrlTranslate.exe (light, no WebEngine) ...
.venv\Scripts\pyinstaller ^
    --noconfirm --clean ^
    --windowed --onefile ^
    --name CtrlTranslate ^
    --icon assets\icon.ico ^
    --add-data "assets\icon.png;assets" ^
    --hidden-import comtypes.stream ^
    --collect-submodules edge_tts ^
    --exclude-module PyQt5 ^
    --exclude-module tkinter ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtWebEngineQuick ^
    --exclude-module PySide6.QtWebChannel ^
    main.py || goto :fail

echo.
echo [build] done: dist\CtrlTranslate.exe
exit /b 0

:fail
echo [build] FAILED
exit /b 1
