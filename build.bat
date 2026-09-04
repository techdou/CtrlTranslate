@echo off
rem CtrlTranslate 一键打包脚本（单 exe，无控制台窗口）
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pyinstaller.exe (
    echo [build] installing pyinstaller...
    .venv\Scripts\pip install pyinstaller || goto :fail
)

echo [build] cleaning old output...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [build] building CtrlTranslate.exe ...
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
    main.py || goto :fail

echo.
echo [build] done: dist\CtrlTranslate.exe
exit /b 0

:fail
echo [build] FAILED
exit /b 1
