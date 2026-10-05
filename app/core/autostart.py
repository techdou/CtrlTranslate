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
    # argv[0] 异常形态（python -c 时为 "-c"）回退项目根 main.py
    # （本文件在 app/core/ 下：parents[0]=core [1]=app [2]=项目根）
    argv0 = sys.argv[0] if sys.argv and not sys.argv[0].startswith("-") else ""
    entry = Path(argv0).resolve() if argv0 else Path(__file__).resolve().parents[2] / "main.py"
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
                logger.info("autostart enabled: %s", _command())
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass
                logger.info("autostart disabled")
        return True
    except OSError as e:
        logger.warning("autostart set failed: %s", e)
        return False


def reconcile(desired: bool | None) -> bool:
    """启动时对齐注册表与用户意图，修复外部删除造成的漂移（返回最终实际状态）。

    desired=None（配置从未记录过意图）不动作——老用户升级后第一次启动时
    cfg 里没有 autostart 键，若按默认 False 处理会误删已有的自启。
    漂移修复场景：勾选自启后注册表键被测试/清理工具删除 → 下次手动启动
    应用时按意图自动补写，无需用户重新勾选。
    """
    if desired is None:
        return is_enabled()
    actual = is_enabled()
    if actual == desired:
        return actual
    logger.warning("autostart drift: desired=%s registry=%s -> repairing", desired, actual)
    ok = set_enabled(desired)
    return is_enabled() if ok else actual
