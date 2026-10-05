"""开机自启注册表单测。

历史教训：本测试曾直接操作真实键 CtrlTranslate——用户勾选自启后跑一次
pytest，结尾的 set_enabled(False) 就把真实自启键静默删掉，表现为
「开机自启时灵时不灵」。现在一律 monkeypatch 专属测试键名，并加护栏
断言保证真实键全程不被触碰。
"""

import winreg

import pytest

from app.core import autostart

REAL_KEY = "CtrlTranslate"
TEST_KEY = "CtrlTranslateSelfTest"


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(autostart, "_APP_NAME", TEST_KEY)
    autostart.set_enabled(False)  # 清理上次异常中断的残留
    yield
    autostart.set_enabled(False)  # 删除测试键


def _real_key_exists() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart._RUN_KEY) as key:
            winreg.QueryValueEx(key, REAL_KEY)
            return True
    except (FileNotFoundError, OSError):
        return False


def test_autostart_roundtrip(isolated):
    real_before = _real_key_exists()

    assert autostart.is_enabled() is False

    assert autostart.set_enabled(True) is True
    assert autostart.is_enabled() is True
    cmd = autostart._command()
    assert cmd.startswith('"') and len(cmd) > 4  # 带引号的可执行路径形态

    assert autostart.set_enabled(False) is True
    assert autostart.is_enabled() is False

    # 护栏：测试全程不得触碰用户真实自启键（本 bug 的直接回归测试）
    assert _real_key_exists() == real_before


def test_reconcile_repairs_deleted_key(isolated):
    """勾选意图为开、注册表键被外部（测试/清理工具）删除 → 启动时补写。"""
    autostart.set_enabled(True)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart._RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.DeleteValue(key, TEST_KEY)
    assert autostart.is_enabled() is False

    assert autostart.reconcile(True) is True  # 按意图修复
    assert autostart.is_enabled() is True


def test_reconcile_aligned_noop(isolated):
    assert autostart.reconcile(False) is False
    autostart.set_enabled(True)
    assert autostart.reconcile(True) is True


def test_reconcile_none_never_writes(isolated):
    """desired=None（配置从未记录意图，老用户升级首启）不得动注册表。"""
    autostart.set_enabled(True)
    assert autostart.reconcile(None) is True  # 只报告现状，不写
    assert autostart.is_enabled() is True

    autostart.set_enabled(False)
    assert autostart.reconcile(None) is False
    assert autostart.is_enabled() is False


def test_reconcile_disables_stray_key(isolated):
    """意图为关但键被外部写入 → 启动时按意图清除。"""
    autostart.set_enabled(True)
    assert autostart.reconcile(False) is False
    assert autostart.is_enabled() is False
