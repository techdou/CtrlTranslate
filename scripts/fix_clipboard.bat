@echo off
rem 系统剪贴板卡死（所有程序都复制不了）时双击运行本脚本。
rem 自动请求管理员权限并重启 Windows 剪贴板服务。

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo 正在重启剪贴板服务（cbdhsvc）...
powershell -NoProfile -Command "Get-Service cbdhsvc* | Restart-Service -Force"
echo.
echo 完成。现在试试在任何程序里复制粘贴是否恢复。
pause
