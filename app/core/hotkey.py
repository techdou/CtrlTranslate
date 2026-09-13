"""全局热键：双击触发（默认 Ctrl，可换 Alt / Shift）+ 组合热键状态机。

双击检测（DoubleTapDetector）是纯逻辑状态机（不依赖 keyboard 库，可单测）：
吃 (key_name, is_down) 事件流，在"两次目标键按下间隔 ≤ 阈值且期间无其他
按键事件"成立时，于目标键**抬起**瞬间确认触发——避免用户还按着 Ctrl 时
就发起取词（后续要模拟 Ctrl+C，会冲突）。

组合热键（alt+q 等）由 ComboDetector 处理：与双击检测同吃 keyboard.hook
事件流、只依赖事件顺序匹配——避开 keyboard.add_hotkey 的查表竞态（快按
快放下组合键漏触发，表现为时灵时不灵）。SimpleHotkey 触发前用物理键态
（GetAsyncKeyState）复核并自愈状态机，防御抬起事件丢失导致的残留误触发。

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
        if "windows" in n or "super" in n or "meta" in n:
            return "windows"
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


_MODIFIER_KEYS = {"ctrl", "alt", "shift", "windows"}


def is_modifier(key: str) -> bool:
    return key in _MODIFIER_KEYS


class ComboDetector:
    """组合热键状态机（如 alt+q）：修饰键精确按下集合 + 目标键 down 即触发。

    为什么不用 keyboard.add_hotkey：它的匹配靠查询"事件被消费时刻"的共享
    按下键集合——按得快时（q down 还在队列排队、alt up 已先处理），查表
    集合里已没有 alt，匹配漏掉，表现为热键时灵时不灵。本实现与
    DoubleTapDetector 同管线只吃事件流本身（顺序 + 自家状态），无该竞态。

    语义与 add_hotkey 对齐：多余修饰键不触发（alt+shift+q 不命中 alt+q）；
    目标键按住重复 down 只触发一次，抬起后可再触发。
    """

    def __init__(self, combo: str):
        raw = [p.strip() for p in (combo or "").split("+")]
        if not combo or not all(raw):
            raise ValueError(f"invalid combo: {combo!r}")
        parts = [p.lower() for p in raw]
        if len(set(parts)) != len(parts):
            raise ValueError(f"invalid combo: {combo!r}")
        self.key = parts[-1]
        if is_modifier(self.key):
            raise ValueError(f"combo target key must not be a modifier: {combo!r}")
        self.mods = frozenset(parts[:-1])
        for m in self.mods:
            if not is_modifier(m):
                raise ValueError(f"non-modifier in combo prefix: {combo!r}")
        self._mods_down: set[str] = set()
        self._fired = False

    def resync_modifiers(self, physically_down: set[str]) -> bool:
        """用物理按下状态刷新修饰键集合，返回当前是否恰好组成注册的组合。

        抬起事件极小概率丢失会让状态机残留"修饰键还按着"（之后裸按目标键
        即误触发）；触发前用真实键盘状态对齐一次即可自愈。
        """
        self._mods_down = {m for m in _MODIFIER_KEYS if m in physically_down}
        return self._mods_down == self.mods

    def feed(self, key_name: str, is_down: bool) -> bool:
        """输入一个键盘事件；返回 True 表示本次事件命中组合热键。"""
        key = DoubleTapDetector.normalize(key_name)
        if is_modifier(key):
            if is_down:
                self._mods_down.add(key)
            else:
                self._mods_down.discard(key)
            return False
        if key != self.key:
            return False
        if is_down:
            if self._mods_down == self.mods and not self._fired:
                self._fired = True
                return True
        else:
            self._fired = False  # 目标键抬起后允许下一次触发
        return False


def format_hotkey(combo: str) -> str:
    """配置串 → 展示串："alt+q" → "Alt+Q"（每段首字母大写：f1→F1、esc→Esc）。"""
    parts = [p.strip() for p in (combo or "").split("+") if p.strip()]
    return "+".join(p[:1].upper() + p[1:] for p in parts)


def physical_mods() -> set[str] | None:
    """当前物理按下的修饰键集合；非 Windows 平台无法查询，返回 None。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
    except AttributeError:  # windll 仅 Windows 存在
        return None
    vk = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "windows": 0x5B}
    return {n for n, v in vk.items() if user32.GetAsyncKeyState(v) & 0x8000}


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
    """单组合热键（OCR 截图 alt+q 等），与 HotkeyService 同用 keyboard.hook
    事件流 + ComboDetector 匹配（add_hotkey 的查表竞态见其 docstring）；
    hotkey 为空串 = 不注册。"""

    triggered = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._hook = None
        self._detector: ComboDetector | None = None

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
            self._detector = ComboDetector(hotkey)
        except ValueError:
            logging.getLogger("ctrltrans.hotkey").warning("invalid hotkey: %r", hotkey)
            return False
        self._hook = keyboard.hook(self._on_event, suppress=False)
        logging.getLogger("ctrltrans.hotkey").info("combo hotkey %r registered", hotkey)
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
        self._detector = None

    def _on_event(self, event) -> None:
        try:
            if self._detector is None or not self._detector.feed(
                    getattr(event, "name", ""), event.event_type == "down"):
                return
            # 触发前物理复核：状态机说命中，还要真实键盘上修饰键确实按着，
            # 否则（抬起事件丢失导致的残留）放弃本次触发并自愈状态机
            physical = physical_mods()
            if physical is not None and not self._detector.resync_modifiers(physical):
                logging.getLogger("ctrltrans.hotkey").info(
                    "combo hit but physical mods mismatch (resynced): %s", physical)
                return
            logging.getLogger("ctrltrans.hotkey").info("combo hotkey fired")
            self._fire()
        except Exception:
            # 钩子线程里绝不抛异常
            logging.getLogger("ctrltrans.hotkey").exception("combo hotkey handler error")

    def _fire(self) -> None:
        try:
            self.triggered.emit()  # keyboard 回调线程 → Qt 自动排队到主线程
        except Exception:
            pass
