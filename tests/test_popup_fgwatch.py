"""popup「点击其他程序关窗」前台监视测试：首次弹出未激活场景的兜底路径。

背景（bug）：热键触发时本应用是后台进程，Windows 前台锁静默拒绝
activateWindow()，应用从未激活 → QEvent.ApplicationDeactivate 不触发 →
首次点击外部弹窗不关，须先点一下弹窗激活再点外部。_on_fg_tick 以前台
句柄变化为信号，与激活状态无关，覆盖首次点击。

Win32 调用全部 mock：_user32 换 SimpleNamespace 喂句柄序列，
_fg_pid 按「本进程/外部进程」返回 pid，不碰真实系统状态。
"""

import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 不弹真窗口，CI 稳定

import pytest

from app.config import DEFAULT_CONFIG

FOREIGN = 0x111   # 弹窗弹出时的前台（用户正用的程序）
FOREIGN2 = 0x222  # 用户点击的另一个外部程序
SELF_WIN = 0x333  # 本进程窗口（弹窗自身/设置/词库）
FOREIGN3 = 0x444


class _FakeTts:
    """hideEvent 无条件调 _tts.stop()，None 会崩（生产 tts 恒为真对象）。"""

    def stop(self) -> None:
        pass


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def popup(qapp, monkeypatch):
    from app.ui import popup as mod
    from app.ui.popup import TranslatePopup

    seq: list[int] = []
    monkeypatch.setattr(mod, "_user32", types.SimpleNamespace(
        GetForegroundWindow=lambda: seq.pop(0) if seq else FOREIGN))
    monkeypatch.setattr(mod, "_fg_pid",
                        lambda hwnd: os.getpid() if hwnd == SELF_WIN
                        else os.getpid() + 1)
    p = TranslatePopup(lambda: DEFAULT_CONFIG, tts=_FakeTts(),
                       translator=None)
    p.close_calls: list[int] = []
    monkeypatch.setattr(p, "close_animated",
                        lambda: p.close_calls.append(1))
    yield p, seq
    p.hide()


def test_first_tick_records_baseline_only(popup):
    """弹窗刚显示、用户没动：首个 tick 只建基线，绝不关。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    assert p.close_calls == []
    assert p._fg_prev == FOREIGN


def test_no_foreground_change_keeps_open(popup):
    """前台句柄不变（用户在原程序里打字）：不关。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    p._on_fg_tick()
    assert p.close_calls == []


def test_switch_to_foreign_closes_on_first_click(popup):
    """核心回归：未点过弹窗（从未激活）时首次点击外部 → 关。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    seq[:] = [FOREIGN2]
    p._on_fg_tick()
    assert p.close_calls == [1]


def test_switch_to_self_window_keeps_open(popup):
    """点弹窗/设置等本进程窗口不算外部点击；之后再回外部程序才关。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    seq[:] = [SELF_WIN]
    p._on_fg_tick()
    assert p.close_calls == []
    assert p._fg_prev == SELF_WIN
    seq[:] = [FOREIGN2]
    p._on_fg_tick()
    assert p.close_calls == [1]


def test_pinned_ignores_switch_until_unpinned(popup):
    """钉住期间前台切换只推进基线不关；取消钉住后下一次切换才生效。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    p._pinned = True
    seq[:] = [FOREIGN2]
    p._on_fg_tick()
    assert p.close_calls == []
    p._pinned = False
    seq[:] = [FOREIGN3]
    p._on_fg_tick()
    assert p.close_calls == [1]


def test_show_starts_watch_and_builds_baseline_immediately(popup):
    """生命周期契约 + 盲区修复：show 瞬间启动监视并建好基线（不待首个
    tick），hide 停表——漏停表意味着弹窗隐藏后 200ms 轮询常驻后台。"""
    p, seq = popup
    p.show()
    assert p._fg_watch_timer.isActive()
    assert p._fg_prev == FOREIGN  # show 瞬间已建基线，200ms 盲区不再存在
    p.hide()
    assert not p._fg_watch_timer.isActive()


def test_null_foreground_skips_and_keeps_baseline(popup):
    """锁屏等过渡态前台为 0：跳过判定且不污染基线（docstring 明示契约）。"""
    p, seq = popup
    seq[:] = [FOREIGN]
    p._on_fg_tick()
    seq[:] = [0]
    p._on_fg_tick()
    assert p._fg_prev == FOREIGN
    assert p.close_calls == []
    seq[:] = [FOREIGN2]  # 过渡结束直接落到别的程序：按原基线判变化 → 关
    p._on_fg_tick()
    assert p.close_calls == [1]


def test_close_animated_idempotent_while_fading(popup):
    """双路竞争回归锁：淡出进行中重复 close_animated 直接短路——
    失焦事件与前台监视几乎必然先后到达，重启淡出会把关闭拉长。

    fixture 把实例的 close_animated mock 成记录器，本测试用类实现直调
    （实例属性只遮不换，真实现仍可达）。"""
    p, seq = popup
    p.show()
    real_close = type(p).close_animated
    real_close(p)                 # 第一次：创建淡出
    anim = p._close_anim
    assert anim is not None
    real_close(p)                 # 第二次：应短路
    assert p._close_anim is anim, "fade was restarted (not idempotent)"
