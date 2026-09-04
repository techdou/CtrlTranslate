"""双击 Ctrl 全局热键。

DoubleTapDetector 是纯逻辑状态机（不依赖 keyboard 库，可单测）：
吃 (key_name, is_down) 事件流，在"两次目标键按下间隔 ≤ 阈值且期间无其他
按键事件"成立时，于目标键**抬起**瞬间确认触发——避免用户还按着 Ctrl 时
就发起取词（后续要模拟 Ctrl+C，会冲突）。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, Signal


class DoubleTapDetector:
    def __init__(self, target_key: str = "ctrl", interval_ms: int = 300, clock=time.monotonic):
        self.target_key = target_key
        self.interval_ms = interval_ms
        self._clock = clock
        self._last_down: float = float("-inf")
        self._dirty = False          # 上次目标键按下后是否出现过其他按键事件
        self._pending = False        # 已检测到双击，等待目标键抬起

    @staticmethod
    def normalize(name: str) -> str:
        n = (name or "").lower()
        if "ctrl" in n or "control" in n:
            return "ctrl"
        return n

    def feed(self, key_name: str, is_down: bool) -> bool:
        """输入一个键盘事件；返回 True 表示本次事件确认了一次双击触发。"""
        key = self.normalize(key_name)
        if key != self.target_key:
            # 其他键的任何动作都使当前窗口失效（用户可能在按组合键）
            self._dirty = True
            self._pending = False
            return False

        if is_down:
            now = self._clock()
            if now - self._last_down <= self.interval_ms / 1000.0 and not self._dirty:
                self._pending = True
            # 无论成不成，本轮以这次按下为新起点
            self._last_down = now
            self._dirty = False
            return False

        # 抬起
        if self._pending:
            self._pending = False
            self._last_down = float("-inf")  # 三连击只触发一次
            return True
        return False


class HotkeyService(QObject):
    """把 keyboard 库的钩子事件喂给状态机，触发时发 Qt 信号（跨线程排队到主线程）。"""

    triggered = Signal()

    def __init__(self, interval_ms: int = 300, parent: QObject | None = None):
        super().__init__(parent)
        self.detector = DoubleTapDetector(interval_ms=interval_ms)
        self._hook = None

    def set_interval(self, interval_ms: int) -> None:
        self.detector.interval_ms = interval_ms

    def start(self) -> bool:
        if self._hook is not None:
            return True
        try:
            import keyboard
        except ImportError:
            return False
        self._hook = keyboard.hook(self._on_event, suppress=False)
        return True

    def stop(self) -> None:
        if self._hook is None:
            return
        try:
            import keyboard
            keyboard.unhook(self._hook)
        except Exception:
            pass
        self._hook = None

    def _on_event(self, event) -> None:
        try:
            if self.detector.feed(getattr(event, "name", ""), event.event_type == "down"):
                logging.getLogger("ctrltrans.hotkey").info("double-ctrl detected, trigger fired")
                self.triggered.emit()
        except Exception:
            # 钩子线程里绝不抛异常
            pass
