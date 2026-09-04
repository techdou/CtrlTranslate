from app.core import autostart


def test_autostart_roundtrip():
    # 清理可能的历史状态
    autostart.set_enabled(False)
    assert autostart.is_enabled() is False

    assert autostart.set_enabled(True) is True
    assert autostart.is_enabled() is True
    cmd = autostart._command()
    assert cmd.startswith('"') and len(cmd) > 4  # 带引号的可执行路径形态

    assert autostart.set_enabled(False) is True
    assert autostart.is_enabled() is False
