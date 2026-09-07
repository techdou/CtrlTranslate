"""开机自启：HKCU Run 注册表键（无需管理员权限）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("ctrltrans.autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "CtrlTranslate"


def _command() -> str:
    """自启命令：打包后直接 exe；开发态用 pythonw 静默跑 main.py。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # 开发环境：venv 里找 pythonw（无控制台窗口）
    python_dir = Path(sys.executable).parent
    pythonw = python_dir / "pythonw.exe"
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    entry = Path(sys.argv[0]).resolve() if sys.argv[0] else Path("main.py").resolve()
    return f'"{interpreter}" "{entry}"'


def is_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _open_run_key(access: int):
    """打开 HKCU Run 键；精简用户配置文件（如 CI runner）下该键可能缺失，创建之。"""
    import winreg

    try:
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access)
    except FileNotFoundError:
        return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access)


def set_enabled(on: bool) -> bool:
    try:
        import winreg

        with _open_run_key(winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        logger.warning("autostart set failed: %s", e)
        return False
