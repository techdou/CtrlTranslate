"""双击触发全局热键（默认 Ctrl，可换 Alt / Shift）。

DoubleTapDetector 是纯逻辑状态机（不依赖 keyboard 库，可单测）：
吃 (key_name, is_down) 事件流，在"两次目标键按下间隔 ≤ 阈值且期间无其他
按键事件"成立时，于目标键**抬起**瞬间确认触发——避免用户还按着 Ctrl 时
就发起取词（后续要模拟 Ctrl+C，会冲突）。

触发键可配置：键盘钩子始终监听全部按键（feed 内过滤目标键），换键只需
更新 detector，无需重装钩子。
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
        if "alt" in n:    # alt / left alt / right alt / alt gr
            return "alt"
        if "shift" in n:  # shift / left shift / right shift
            return "shift"
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

    def __init__(self, interval_ms: int = 300, key: str = "ctrl", parent: QObject | None = None):
        super().__init__(parent)
        self.detector = DoubleTapDetector(target_key=key, interval_ms=interval_ms)
        self._hook = None

    def set_interval(self, interval_ms: int) -> None:
        self.detector.interval_ms = interval_ms

    def set_key(self, key: str) -> None:
        """切换触发键：重建状态机（丢弃半截的判定状态），钩子无需重装。"""
        if self.detector.target_key == key:
            return
        self.detector = DoubleTapDetector(
            target_key=key, interval_ms=self.detector.interval_ms
        )

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


class SimpleHotkey(QObject):
    """单组合热键（keyboard.add_hotkey），OCR 截图等一次性动作用。
    与 HotkeyService 的全局钩子同库共存；hotkey 为空串 = 不注册。"""

    triggered = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._handle = None

    def start(self, hotkey: str) -> bool:
        """注册热键（如 "alt+q"）；空串或格式无效返回 False。已注册时先换绑。"""
        hotkey = (hotkey or "").strip()
        self.stop()
        if not hotkey:
            return False
        try:
            import keyboard
        except ImportError:
            return False
        try:
            self._handle = keyboard.add_hotkey(hotkey, self._fire)
            return True
        except Exception:
            logging.getLogger("ctrltrans.hotkey").warning("invalid hotkey: %r", hotkey)
            self._handle = None
            return False

    def stop(self) -> None:
        if self._handle is None:
            return
        try:
            import keyboard
            keyboard.remove_hotkey(self._handle)
        except Exception:
            pass
        self._handle = None

    def _fire(self) -> None:
        try:
            self.triggered.emit()  # keyboard 回调线程 → Qt 自动排队到主线程
        except Exception:
            pass
