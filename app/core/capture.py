"""取词：获取当前选中文字。

两级策略：
1. UIA（UI Automation TextPattern.GetSelection）——不动剪贴板，Chromium 系
   浏览器等可用；部分 PDF 阅读器不支持。
2. 剪贴板模拟——模拟 Ctrl+C 复制选中文本再读剪贴板，几乎万能；期间完整
   保存/恢复剪贴板（文本、HTML、DIB 位图等），不破坏用户剪贴板。
   注：文件列表（CF_HDROP）暂不恢复，划词场景几乎不涉及。

仅支持 Windows。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import re
import time

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("ctrltrans.capture")

CF_UNICODETEXT = 13
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# 64 位下 ctypes 默认 restype=c_int 会把 HGLOBAL/指针截断，必须显式声明，
# 否则 GlobalAlloc 返回高地址句柄时剪贴板写入静默失败
_HGLOBAL = ctypes.c_void_p
_user32.SetClipboardData.restype = _HGLOBAL
_user32.SetClipboardData.argtypes = [ctypes.c_uint, _HGLOBAL]
_user32.GetClipboardData.restype = _HGLOBAL
_user32.GetClipboardData.argtypes = [ctypes.c_uint]
_user32.OpenClipboard.restype = ctypes.c_int
_user32.OpenClipboard.argtypes = [_HGLOBAL]
_user32.CloseClipboard.restype = ctypes.c_int
_user32.CloseClipboard.argtypes = []
_user32.EnumClipboardFormats.restype = ctypes.c_uint
_user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
_user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
_user32.GetClipboardSequenceNumber.argtypes = []
_kernel32.GlobalAlloc.restype = _HGLOBAL
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalLock.restype = _HGLOBAL
_kernel32.GlobalLock.argtypes = [_HGLOBAL]
_kernel32.GlobalUnlock.restype = ctypes.c_int
_kernel32.GlobalUnlock.argtypes = [_HGLOBAL]
_kernel32.GlobalFree.restype = _HGLOBAL
_kernel32.GlobalFree.argtypes = [_HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalSize.argtypes = [_HGLOBAL]


# ---------------------------------------------------------------- 文本清洗

_HYPHEN_BREAK = re.compile(r"([A-Za-z])-\n([a-z])")   # PDF 断词：inter-\nnetwork
_WORD_BREAK = re.compile(r"([A-Za-z,;:])\n([a-z])")   # 无标点换行：local\nmemory


def clean_text(raw: str, max_chars: int = 3000) -> str:
    """合并 PDF/网页里的硬换行、压缩空白；超长截断。"""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # 段落 = 连续非空行；段内行合并
    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        merged = lines[0]
        for ln in lines[1:]:
            merged = _merge_line(merged, ln)
        paragraphs.append(merged)
    text = "\n".join(paragraphs)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _merge_line(acc: str, nxt: str) -> str:
    tail, head = acc[-1:], nxt[:1]
    if tail and head:
        # 行尾连字符：保留（原词带连字符的场景更常见，软连字符由 LLM 容错）
        if tail == "-":
            return acc + nxt
        if (tail.isalnum() or tail in ",;:") and head.islower():
            return acc + " " + nxt         # 英文句中断行补空格
        if tail.isalnum() and head.isalnum():
            return acc + nxt               # CJK 等直接拼
    return acc + "\n" + nxt


# ---------------------------------------------------------------- UIA 取词

def uia_get_selection(timeout_s: float = 0.8) -> str:
    try:
        import uiautomation as auto
    except Exception:
        return ""
    try:
        el = auto.GetFocusedElement(timeout=int(timeout_s * 1000))
        if el is None:
            return ""
        pattern = el.GetPattern(auto.PatternId.TextPattern)
        if pattern is None:
            return ""
        ranges = pattern.GetSelection()
        parts = []
        for r in ranges:
            try:
                parts.append(r.GetText(-1))
            except Exception:
                pass
        return "\n".join(p for p in parts if p)
    except Exception:
        return ""


# ---------------------------------------------------------------- 剪贴板工具

def _open_clipboard(retries: int = 10, delay: float = 0.04) -> bool:
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def _save_clipboard() -> list[tuple[int, str | bytes]]:
    """快照剪贴板常见格式。返回 [(fmt, data)]；文本 str、其余 bytes。"""
    saved: list[tuple[int, str | bytes]] = []
    if not _open_clipboard():
        return saved
    try:
        fmt = _user32.EnumClipboardFormats(0)
        while fmt:
            data = _read_format(fmt)
            if data is not None:
                saved.append((fmt, data))
            fmt = _user32.EnumClipboardFormats(fmt)
    finally:
        _user32.CloseClipboard()
    return saved


def _read_format(fmt: int) -> str | bytes | None:
    h = _user32.GetClipboardData(fmt)
    if not h:
        return None
    size = _kernel32.GlobalSize(h)
    ptr = _kernel32.GlobalLock(h)
    if not ptr or size == 0:
        if ptr:
            _kernel32.GlobalUnlock(h)
        return None
    try:
        if fmt == CF_UNICODETEXT:
            raw = ctypes.string_at(ptr, size)
            return raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
        return ctypes.string_at(ptr, size)
    finally:
        _kernel32.GlobalUnlock(h)


def _restore_clipboard(saved: list[tuple[int, str | bytes]]) -> None:
    if not saved:
        # 保存阶段失败时宁可保留剪贴板现状（可能是刚复制的新内容），
        # 也不能 EmptyClipboard 把用户数据清掉
        logger.warning("skip restore: clipboard snapshot was empty")
        return
    if not _open_clipboard():
        return
    try:
        _user32.EmptyClipboard()
        for fmt, data in saved:
            if isinstance(data, str):
                payload = data.encode("utf-16-le") + b"\x00\x00"
            else:
                payload = data
            h = _kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(payload))
            if not h:
                continue
            ptr = _kernel32.GlobalLock(h)
            if not ptr:
                _kernel32.GlobalFree(h)
                continue
            try:
                ctypes.memmove(ptr, payload, len(payload))
            finally:
                _kernel32.GlobalUnlock(h)
            _user32.SetClipboardData(fmt, h)  # 成功后系统接管内存
    finally:
        _user32.CloseClipboard()


def get_clipboard_text() -> str:
    if not _open_clipboard():
        return ""
    try:
        data = _read_format(CF_UNICODETEXT)
        return data if isinstance(data, str) else ""
    finally:
        _user32.CloseClipboard()


# ---------------------------------------------------------------- 模拟 Ctrl+C

VK_CONTROL = 0x11
KEYEVENTF_KEYUP = 0x0002


def send_ctrl_c() -> None:
    for vk, flags in (
        (VK_CONTROL, 0),
        (ord("C"), 0),
        (ord("C"), KEYEVENTF_KEYUP),
        (VK_CONTROL, KEYEVENTF_KEYUP),
    ):
        _user32.keybd_event(vk, 0, flags, 0)
        time.sleep(0.01)


def clipboard_get_selection(wait_ms: int = 400) -> str:
    """复制当前选中内容到剪贴板并取回，全程恢复剪贴板。"""
    saved = _save_clipboard()
    seq_before = _user32.GetClipboardSequenceNumber()
    send_ctrl_c()
    deadline = time.monotonic() + wait_ms / 1000.0
    text = ""
    try:
        while time.monotonic() < deadline:
            if _user32.GetClipboardSequenceNumber() != seq_before:
                time.sleep(0.05)  # 等目标程序写完
                text = get_clipboard_text()
                break
            time.sleep(0.03)
    finally:
        time.sleep(0.05)
        _restore_clipboard(saved)
    return text


# ---------------------------------------------------------------- 对外服务

def get_foreground_app() -> str:
    """前台窗口进程名（仅记录用，失败返回空串）。"""
    try:
        import os

        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wt.DWORD(512)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return ""
    return ""


class TextCaptureService(QObject):
    """取词服务。UIA 调用放短命线程，避免 COM 初始化/超时卡主线程。"""

    captured = Signal(str, str)   # (text, method)
    failed = Signal(str)

    def __init__(self, cfg_getter, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._busy = False

    def capture(self) -> None:
        if self._busy:
            return
        self._busy = True
        import threading

        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cfg = self._cfg_getter()
            max_chars = int(cfg.get("translate", {}).get("max_chars", 3000))
            method = "clipboard"
            text = ""
            logger.info("capture started (uia preferred=%s)",
                        cfg.get("capture", {}).get("prefer_uia", True))
            if cfg.get("capture", {}).get("prefer_uia", True):
                text = uia_get_selection()
                logger.info("uia selection: %d chars", len(text))
                if text:
                    method = "uia"
            if not text:
                wait_ms = int(cfg.get("capture", {}).get("clipboard_wait_ms", 400))
                text = clipboard_get_selection(wait_ms)
                logger.info("clipboard selection: %d chars", len(text))
            text = clean_text(text, max_chars)
            if text:
                self.captured.emit(text, method)
            else:
                key = cfg.get("trigger", {}).get("key", "ctrl")
                key_label = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}.get(key, "Ctrl")
                self.failed.emit(f"未取到选中文本（请先选中一段文字再双击 {key_label}）")
        except Exception as e:
            logger.exception("capture failed")
            self.failed.emit(f"取词失败：{e}")
        finally:
            self._busy = False
