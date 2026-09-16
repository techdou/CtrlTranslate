"""取词防护测试：剪贴板快照白名单 + busy 看门狗（挂死自愈）。

不触真实剪贴板——win32 调用全部 monkeypatch，只验证门控逻辑。
"""

import time
import types

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def make_service(monkeypatch, cfg=None):
    """构造服务并拦截工作线程启动（不真正跑取词链）。返回 (svc, started)。"""
    from app.core import capture as cap

    cfg = cfg or {"capture": {"prefer_uia": False}, "translate": {"max_chars": 100}}
    svc = cap.TextCaptureService(lambda: cfg)
    started = []
    monkeypatch.setattr(
        cap.threading, "Thread",
        lambda target, daemon: types.SimpleNamespace(start=lambda: started.append(target)))
    return svc, started


# ---------------------------------------------------------------- 快照白名单

def test_snapshot_whitelist_covers_standard_formats():
    from app.core.capture import (
        CF_DIB, CF_DIBV5, CF_HDROP, CF_UNICODETEXT, SNAPSHOT_FORMATS,
    )

    assert SNAPSHOT_FORMATS == frozenset({CF_UNICODETEXT, CF_DIB, CF_DIBV5, CF_HDROP})
    # 私有/注册格式（>= 0xC000）必须不在白名单——延迟渲染主要藏在那里
    assert all(fmt < 0xC000 for fmt in SNAPSHOT_FORMATS)


def test_save_clipboard_skips_non_whitelist_formats(monkeypatch):
    from app.core import capture as cap

    # 模拟剪贴板含：白名单文本 + 一个私有格式（49161 = 0xC009 注册格式）
    fmts = iter([cap.CF_UNICODETEXT, 49161, 0])
    monkeypatch.setattr(cap._user32, "OpenClipboard", lambda *_: 1)
    monkeypatch.setattr(cap._user32, "CloseClipboard", lambda: 1)
    monkeypatch.setattr(cap._user32, "EnumClipboardFormats", lambda f: next(fmts))
    read_calls = []

    def fake_read(fmt):
        read_calls.append(fmt)
        return "文本" if fmt == cap.CF_UNICODETEXT else b"\x00"

    monkeypatch.setattr(cap, "_read_format", fake_read)
    saved = cap._save_clipboard()
    assert saved == [(cap.CF_UNICODETEXT, "文本")]   # 私有格式被跳过
    assert read_calls == [cap.CF_UNICODETEXT]        # 且未被读取（不触发延迟渲染）


# ---------------------------------------------------------------- busy 看门狗

def test_busy_recent_run_is_blocked(monkeypatch):
    svc, started = make_service(monkeypatch)
    svc._busy = True
    svc._busy_since = time.monotonic()          # 刚开始，未超时
    svc.capture()
    assert started == []                        # 拒绝并发，静默吞掉
    assert svc._busy is True


def test_busy_watchdog_resets_after_timeout(monkeypatch):
    svc, started = make_service(monkeypatch)
    svc._busy = True
    svc._busy_since = time.monotonic() - 999    # 远超 CAPTURE_WATCHDOG_S
    svc.capture()
    assert started, "看门狗超时后必须放行新任务（热键自愈）"
    assert svc._busy is True and svc._busy_since > 0   # 新一轮已置位


def test_watchdog_threshold_uses_module_constant(monkeypatch):
    from app.core import capture as cap

    svc, started = make_service(monkeypatch)
    svc._busy = True
    # 恰好差 1s 到阈值：仍拒绝
    svc._busy_since = time.monotonic() - (cap.CAPTURE_WATCHDOG_S - 1)
    svc.capture()
    assert started == []


def test_run_resets_busy_in_finally(monkeypatch):
    """正常路径 finally 复位 _busy（看门狗之外的基本回收仍成立）。"""
    from app.core import capture as cap

    cfg = {"capture": {"prefer_uia": False}, "translate": {"max_chars": 100}}
    svc = cap.TextCaptureService(lambda: cfg)
    monkeypatch.setattr(cap, "uia_get_selection", lambda: "")
    monkeypatch.setattr(cap, "clipboard_get_selection", lambda wait_ms=400: "hello")
    svc._busy = True
    svc._busy_since = time.monotonic()
    svc._run()
    assert svc._busy is False
