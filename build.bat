@echo off
rem CtrlTranslate lite build - onedir via spec, no WebEngine.
rem Optional: also builds Inno Setup installer when ISCC is on PATH.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\pyinstaller.exe (
    echo [build] installing pyinstaller...
    .venv\Scripts\pip install pyinstaller || goto :fail
)

echo [build] cleaning old output...
if exist dist\CtrlTranslate rmdir /s /q dist\CtrlTranslate

echo [build] building CtrlTranslate - lite, onedir ...
.venv\Scripts\pyinstaller --noconfirm --clean CtrlTranslate.spec || goto :fail
if not exist dist\CtrlTranslate\CtrlTranslate.exe goto :fail
echo [build] done: dist\CtrlTranslate\CtrlTranslate.exe

where iscc >nul 2>nul
if errorlevel 1 (
    echo [build] ISCC not found, skip installer - install Inno Setup to enable
    exit /b 0
)
for /f %%v in ('.venv\Scripts\python -c "from app import __version__; print(__version__)"') do set APPVER=%%v
echo [build] building installer v%APPVER% ...
iscc /DAppVersion=%APPVER% /DVariant=lite installer\installer.iss || goto :fail
echo [build] installer: dist\installer\CtrlTranslate-Setup-%APPVER%.exe
exit /b 0

:fail
echo [build] FAILED
exit /b 1
