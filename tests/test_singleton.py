"""单实例锁测试：QLocalServer 抢锁互斥、第二实例唤醒链路、释放重抢。

QLocalServer/QLocalSocket 不依赖显示（offscreen 可跑），但需要事件循环
驱动 readyRead——用 processEvents 轮询等待。
"""

import time

from PySide6.QtWidgets import QApplication

from app.core.singleton import SingleInstance, instance_key

qapp = QApplication.instance() or QApplication([])

TEST_APP = "CtrlTranslateTest"  # 独立键名，避免与真实运行中的程序/其他测试冲突


def test_instance_key_contains_username():
    key = instance_key(TEST_APP)
    import os
    user = os.environ.get("USERNAME") or "user"
    assert key == f"{TEST_APP.lower()}-{user}"


def test_first_acquire_succeeds():
    inst = SingleInstance(TEST_APP)
    try:
        assert inst.acquire() is True
        assert inst.is_primary
    finally:
        inst.release()


def test_second_acquire_fails_and_notifies_primary():
    """双实例核心场景：第二实例抢锁失败，唤醒指令送达主实例。"""
    primary = SingleInstance(TEST_APP)
    assert primary.acquire()

    fired = []
    primary.activated.connect(lambda: fired.append(True))

    second = SingleInstance(TEST_APP)
    try:
        assert second.acquire() is False          # 抢锁失败
        assert not second.is_primary
        second.notify_primary()                    # 唤醒主实例
        deadline = time.monotonic() + 2
        while not fired and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert fired, "主实例未收到激活信号"
    finally:
        second.release()
        primary.release()


def test_release_then_reacquire():
    """主实例退出后，新实例能立即接管（Windows 管道随进程销毁无残留）。"""
    first = SingleInstance(TEST_APP)
    assert first.acquire()
    first.release()

    second = SingleInstance(TEST_APP)
    try:
        assert second.acquire()
    finally:
        second.release()
