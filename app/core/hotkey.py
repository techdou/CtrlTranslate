"""全局热键：双击触发（默认 Ctrl，可换 Alt / Shift）+ 组合热键状态机。

双击检测（DoubleTapDetector）是纯逻辑状态机（不依赖 keyboard 库，可单测）：
吃 (key_name, is_down) 事件流，在"两次目标键按下间隔 ≤ 阈值且期间无其他
按键事件"成立时，于目标键**抬起**瞬间确认触发——避免用户还按着 Ctrl 时
就发起取词（后续要模拟 Ctrl+C，会冲突）。

组合热键（alt+q 等）由 ComboDetector 处理：与双击检测同吃 keyboard.hook
事件流、只依赖事件顺序匹配——避开 keyboard.add_hotkey 的查表竞态（快按
快放下组合键漏触发，表现为时灵时不灵）。触发时机 = 修饰键全部松开（对齐
双击检测的抬起触发，保证紧随其后的取词模拟 Ctrl+C 不被按住的修饰键污
染）；抬起事件丢失由物理键态对齐自愈（防热键卡死直到重启）。

触发键可配置：键盘钩子始终监听全部按键（feed 内过滤目标键），换键只需
更新 detector，无需重装钩子。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal


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

# 目标键两次 down 的最小人间隔：小于它视为系统 auto-repeat（按住不放的
# 重复 down），大于它必然是一次新的物理按压（上一次的 up 事件已丢失，
# 状态机借此自愈，防"热键卡死直到重启"）
REPEAT_GUARD_S = 0.3

# 命中后等待修饰键全松的时效：超时作废。修饰键 up 事件丢失时 _pending 会
# 陈旧驻留——之后用户任意一次松开任何修饰键（如打完大写字母松 Shift）都会
# spontaneous 触发截图/翻译（幽灵触发）
PENDING_EXPIRE_S = 2.0

# 钩子看门狗重挂周期：Windows 对回调超时的 WH_KEYBOARD_LL 会静默摘钩
# （LowLevelHooksTimeout 机制，keyboard 库不检测不重装）——摘钩即热键全灭。
# 周期性无条件重挂（unhook+hook 成本极低），最多丢一个周期的事件
HOOK_REARM_MS = 60_000


def is_modifier(key: str) -> bool:
    return key in _MODIFIER_KEYS


class ComboDetector:
    """组合热键状态机（如 alt+q）：修饰键精确按下 + 目标键 down 命中，
    **修饰键全部松开的瞬间触发**（与 DoubleTapDetector 的抬起触发对齐——
    触发时修饰键已松开，紧随其后的取词模拟 Ctrl+C 才不会变成 Alt+Ctrl+C
    而复制失败，这是"划了词仍降级截图"的根因）。

    为什么不用 keyboard.add_hotkey：它的匹配靠查询"事件被消费时刻"的共享
    按下键集合——按得快时（q down 还在队列排队、alt up 已先处理），查表
    集合里已没有 alt，匹配漏掉，表现为热键时灵时不灵。本实现与
    DoubleTapDetector 同管线只吃事件流本身（顺序 + 自家状态），无该竞态。

    抬起事件丢失的自愈：目标键 down 间隔超过 REPEAT_GUARD_S 视为新按压
    （清 _fired 重评）；修饰键残留由 SimpleHotkey 在目标键 down 时用物理
    键态对齐（resync_modifiers）。多余修饰键不命中（alt+shift+q 不算 alt+q）。
    """

    def __init__(self, combo: str, clock=time.monotonic):
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
        self._clock = clock
        self._mods_down: set[str] = set()
        self._fired = False        # 本轮按压已命中（防 auto-repeat 重复触发）
        self._pending = False      # 已命中，等修饰键全部松开即触发
        self._pending_since = float("-inf")  # _pending 置位时刻（时效判定用）
        self._last_target_down = float("-inf")

    def resync_modifiers(self, physically_down: set[str]) -> bool:
        """用物理按下状态刷新修饰键集合，返回当前是否恰好组成注册的组合。

        抬起事件极小概率丢失会让状态机残留"修饰键还按着"（后续组合永不
        命中，热键卡死直到重启）；目标键 down 时对齐一次物理真相即可自愈。
        """
        self._mods_down = {m for m in _MODIFIER_KEYS if m in physically_down}
        return self._mods_down == self.mods

    def feed(self, key_name: str, is_down: bool) -> bool:
        """输入一个键盘事件；返回 True 表示本次事件命中组合热键（触发）。"""
        key = DoubleTapDetector.normalize(key_name)
        if is_modifier(key):
            if is_down:
                self._mods_down.add(key)
            else:
                self._mods_down.discard(key)
                if self._pending and not self._mods_down:
                    self._pending = False
                    # 陈旧挂起（修饰键 up 丢失后残留）不作数：宁可错过一次
                    # 触发，也不能让用户松 Shift 时凭空弹截图遮罩
                    if self._clock() - self._pending_since <= PENDING_EXPIRE_S:
                        return True  # 修饰键全部松开：此刻触发（取词可安全模拟 Ctrl+C）
            return False
        if key != self.key:
            return False
        if is_down:
            now = self._clock()
            if now - self._last_target_down > REPEAT_GUARD_S:
                self._fired = False  # 新的一次物理按压（上轮 up 已丢则借此重置）
            self._last_target_down = now
            if self._mods_down == self.mods and not self._fired:
                self._fired = True
                self._pending = True  # 命中：等修饰键全松
                self._pending_since = now
        else:
            self._fired = False
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
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(HOOK_REARM_MS)
        self._watchdog.timeout.connect(self._rearm)

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
        self._watchdog.start()
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

    def _rearm(self) -> None:
        """看门狗：周期性无条件重挂钩子。

        Windows 对回调超时的 LL 钩子会静默摘除且不通知（摘除即双击检测全灭，
        进程还在、表现为"后台卡掉"），keyboard 库自身不检测——只能靠定期
        重挂自愈。_hook 为 None（用户禁用）时不动作。
        """
        if self._hook is None:
            return
        try:
            import keyboard
            keyboard.unhook(self._hook)
        except Exception:
            pass
        self._hook = None
        if self.start():
            logging.getLogger("ctrltrans.hotkey").debug("double-tap hook re-armed (watchdog)")
        else:
            logging.getLogger("ctrltrans.hotkey").warning("watchdog re-arm failed")

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
        self._hotkey = ""
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(HOOK_REARM_MS)
        self._watchdog.timeout.connect(self._rearm)

    def start(self, hotkey: str, quiet: bool = False) -> bool:
        """注册热键（如 "alt+q"）；空串或格式无效返回 False。已注册时先换绑。

        quiet=True 供看门狗重挂复用：降为 debug 日志（否则每分钟一条 INFO
        刷爆 2MB 轮转日志）。
        """
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
        self._hotkey = hotkey
        self._hook = keyboard.hook(self._on_event, suppress=False)
        self._watchdog.start()
        log = logging.getLogger("ctrltrans.hotkey")
        (log.debug if quiet else log.info)("combo hotkey %r registered", hotkey)
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

    def _rearm(self) -> None:
        """看门狗：周期性无条件重挂（防系统静默摘钩后热键全灭），见 HotkeyService。"""
        if self._hook is None or not self._hotkey:
            return
        try:
            import keyboard
            keyboard.unhook(self._hook)
        except Exception:
            pass
        self._hook = None
        if self.start(self._hotkey, quiet=True):
            logging.getLogger("ctrltrans.hotkey").debug("combo hook re-armed (watchdog): %s", self._hotkey)
        else:
            logging.getLogger("ctrltrans.hotkey").warning("watchdog re-arm failed: %s", self._hotkey)

    def _on_event(self, event) -> None:
        try:
            if self._detector is None:
                return
            name = getattr(event, "name", "")
            down = event.event_type == "down"
            if down and not is_modifier(DoubleTapDetector.normalize(name)):
                # 目标键按下瞬间用物理键态对齐修饰键集合：抬起事件极小概率
                # 丢失会让状态机残留"修饰键还按着"，组合永不命中（热键卡死
                # 直到重启）；物理对齐一次即自愈。物理态是真相，提前知会无害
                physical = physical_mods()
                if physical is not None:
                    self._detector.resync_modifiers(physical)
            if self._detector.feed(name, down):
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
