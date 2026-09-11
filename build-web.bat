@echo off
rem CtrlTranslate 完整版打包（单 exe；约 220MB）
rem 含 WebEngine = 网页版引擎（内嵌 DeepSeek 网页翻译）可用
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pyinstaller.exe (
    echo [build] installing pyinstaller...
    .venv\Scripts\pip install pyinstaller || goto :fail
)

echo [build] cleaning old output...
if exist dist\CtrlTranslate-Web.exe del dist\CtrlTranslate-Web.exe

echo [build] building CtrlTranslate-Web.exe (full, with WebEngine) ...
.venv\Scripts\pyinstaller ^
    --noconfirm --clean ^
    --windowed --onefile ^
    --name CtrlTranslate-Web ^
    --icon assets\icon.ico ^
    --add-data "assets\icon.png;assets" ^
    --hidden-import comtypes.stream ^
    --collect-submodules edge_tts ^
    --exclude-module PyQt5 ^
    --exclude-module tkinter ^
    main.py || goto :fail

echo.
echo [build] done: dist\CtrlTranslate-Web.exe
exit /b 0

:fail
echo [build] FAILED
exit /b 1
