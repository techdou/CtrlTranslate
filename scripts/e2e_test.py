"""端到端自动验证：记事本选中文本 → 模拟双击 Ctrl → 检测翻译弹窗。

验证链路：全局热键 → UIA 取词 → 弹窗显示 → （无 Key 时）错误提示 /（有 Key 时）真实译文。
无需 API Key 也能跑：无 Key 的失败路径本身就是链路终点之一。

用法：
    .venv/Scripts/python scripts/e2e_test.py             # 仅机器空闲（>60s 无键鼠）时运行
    .venv/Scripts/python scripts/e2e_test.py --force     # 跳过空闲检测立即运行（运行期间勿动键鼠）
    .venv/Scripts/python scripts/e2e_test.py --idle 120  # 自定义空闲阈值（秒）
"""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

u32 = ctypes.windll.user32
VK_CONTROL = 0x11
KEYEVENTF_KEYUP = 0x0002

SENTENCE = "The gradient descent algorithm minimizes the loss function iteratively."


def user_idle_seconds() -> float:
    """距用户最后一次键鼠输入的秒数（无人值守保护）。"""

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not u32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0


def tap(vk: int, hold_ms: int = 30) -> None:
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(hold_ms / 1000)
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def double_ctrl(interval_ms: int = 120) -> None:
    tap(VK_CONTROL)
    time.sleep(interval_ms / 1000)
    tap(VK_CONTROL)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="跳过空闲检测立即运行")
    ap.add_argument("--idle", type=int, default=60, help="空闲阈值秒数（默认 60）")
    args = ap.parse_args()

    idle = user_idle_seconds()
    if not args.force and idle < args.idle:
        print(f"[e2e] SKIP: user active {idle:.0f}s ago — 本测试会模拟键盘输入，"
              f"只在机器空闲（>{args.idle}s 无键鼠）时自动运行；确要立即运行请加 --force")
        return 2

    prev_fg = u32.GetForegroundWindow()  # 结束后还原焦点
    # 0. 清场：杀掉可能残留的 main.py 实例（此前轮次的"真身"进程会持有单实例锁，
    #    导致本轮新实例被 QLockFile 挡在"已在运行"弹窗上）
    _kill_all_main_py()
    time.sleep(1)

    # 1. 启动应用（真实 GUI，非 offscreen）。
    #    注意：.venv 的 python.exe 是转发器，真正跑代码的是它启动的子进程——
    #    窗口/钩子/日志都属于"真身" pid，检测与清理都要用真身。
    app_proc = subprocess.Popen(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "main.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )
    app_pid = _resolve_real_pid(app_proc.pid, timeout_s=8)
    print(f"[e2e] app launcher pid={app_proc.pid}, real pid={app_pid}, waiting tray ...")
    if not app_pid:
        print("[e2e] FAIL: cannot resolve real app process")
        return 1
    time.sleep(3)

    target_proc = None
    try:
        # 2. 靶窗口（独立 Qt QTextEdit，自动全选；不用系统记事本——
        #    Win11 记事本是标签模式单进程，新实例并入旧进程，pid 找不到窗口）
        target_proc = subprocess.Popen(
            [str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), str(ROOT / "scripts" / "e2e_target.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
        )
        tgt_hwnd = _wait_title("E2E target", timeout_s=10)
        if not tgt_hwnd:
            print("[e2e] FAIL: target window never appeared")
            return 1
        baseline = _visible_windows(app_pid)  # 弹窗检测基线（此刻 app 只有托盘无窗口）

        if not _force_foreground(tgt_hwnd):
            print("[e2e] FAIL: cannot bring target to foreground (Windows 前台锁定)")
            return 1
        time.sleep(2.2)  # 等靶窗口完成自动全选

        # 3. 双击 Ctrl（松开后触发）
        print("[e2e] sending double-Ctrl ...")
        double_ctrl()

        # 4. 等 app 真身进程出现新可见窗口（翻译弹窗），再轮询采样其文本
        popup_hwnd = _wait_new_window(app_pid, baseline, timeout_s=12)
        found: list[str] = []
        translated = False
        if popup_hwnd:
            # 轮询等流式翻译落定（弹窗里出现中文译文特征），最多 15 秒
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                found = _window_texts(popup_hwnd)
                joined_now = " ".join(found)
                if ("梯度下降" in joined_now or "算法" in joined_now or "损失函数" in joined_now):
                    translated = True
                    break
                if found and "API Key" in joined_now:
                    break  # 无 Key 的失败路径，快速返回
                time.sleep(0.5)

        print(f"[e2e] popup hwnd={popup_hwnd} translated={translated} content -> {found!r}")
        if not popup_hwnd:
            print("[e2e] FAIL: popup never appeared")
            return 1
        if not found:
            print("[e2e] PARTIAL: popup appeared but no text readable via UIA")
            return 1
        joined = " ".join(found)
        if translated:
            print("[e2e] PASS: full chain OK — real translation shown in popup")
        elif SENTENCE[:30] in joined:
            print("[e2e] PASS: source text captured and shown in popup")
        elif "API Key" in joined or "翻译" in joined:
            print("[e2e] PASS: popup alive with translate/key feedback")
        else:
            print("[e2e] PARTIAL: popup exists but content unclear")
            return 1
        return 0
    finally:
        # 树杀：转发器 + 真身一起（taskkill /T），只 kill() 转发器会留下真身持锁
        subprocess.run(
            ["taskkill", "/PID", str(app_proc.pid), "/T", "/F"],
            capture_output=True,
        )
        if target_proc:
            subprocess.run(
                ["taskkill", "/PID", str(target_proc.pid), "/T", "/F"],
                capture_output=True,
            )
        if prev_fg:
            u32.SetForegroundWindow(prev_fg)


def _kill_all_main_py() -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main\\.py' }"
         " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True,
    )


def _resolve_real_pid(launcher_pid: int, timeout_s: float) -> int:
    """venv python.exe 是转发器：找它启动的同命令行子进程（真身）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={launcher_pid}\""
             " | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").strip().splitlines():
            line = line.strip()
            if line.isdigit() and int(line) != launcher_pid:
                return int(line)
        time.sleep(0.3)
    return 0


def _wait_title(title_prefix: str, timeout_s: float) -> int:
    """轮询 EnumWindows 找标题匹配的可见顶层窗口句柄。"""
    import ctypes.wintypes as wt

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith(title_prefix):
                found.append(hwnd)
        return True

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found.clear()
        u32.EnumWindows(cb, 0)
        if found:
            return found[0]
        time.sleep(0.2)
    return 0


def _visible_windows(pid: int) -> set[int]:
    """属于 pid 的可见顶层窗口集合。"""
    import ctypes.wintypes as wt

    out: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        wpid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and u32.IsWindowVisible(hwnd):
            out.add(hwnd)
        return True

    u32.EnumWindows(cb, 0)
    return out


def _wait_new_window(pid: int, baseline: set[int], timeout_s: float) -> int:
    """轮询等待 pid 出现不在 baseline 里的新可见窗口。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        now = _visible_windows(pid) - baseline
        if now:
            return next(iter(now))
        time.sleep(0.2)
    return 0


def _force_foreground(hwnd: int) -> bool:
    """绕过 Windows 前台锁定：Alt-trick + AttachThreadInput，附验证。"""
    if u32.GetForegroundWindow() == hwnd:
        return True
    # Alt-trick：按一下 Alt 让系统认为本进程收到输入，解除前台锁定
    u32.keybd_event(0x12, 0, 0, 0)
    u32.keybd_event(0x12, 0, 2, 0)
    if u32.SetForegroundWindow(hwnd) and u32.GetForegroundWindow() == hwnd:
        return True

    fg = u32.GetForegroundWindow()
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_tid = u32.GetWindowThreadProcessId(fg, None)
    tgt_tid = u32.GetWindowThreadProcessId(hwnd, None)
    k32 = ctypes.windll.kernel32
    k32.AttachThreadInput(cur_tid, fg_tid, True)
    k32.AttachThreadInput(cur_tid, tgt_tid, True)
    try:
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
    finally:
        k32.AttachThreadInput(cur_tid, fg_tid, False)
        k32.AttachThreadInput(cur_tid, tgt_tid, False)
    return u32.GetForegroundWindow() == hwnd


def _window_texts(hwnd: int) -> list[str]:
    """从指定窗口收集 UIA 文本：控件 Name + TextPattern 文档内容（三层深度）。"""
    import uiautomation as auto

    texts: list[str] = []
    try:
        win = auto.ControlFromHandle(hwnd)
        if win is None:
            return []
        texts.append(win.Name or "")
        for ctl in win.GetChildren():
            texts.append(ctl.Name or "")
            texts.extend(_textpattern_content(ctl))
            for sub in ctl.GetChildren():
                texts.append(sub.Name or "")
                texts.extend(_textpattern_content(sub))
    except Exception as e:
        print(f"[e2e] uia walk error: {e}")
    return [t for t in texts if t]


def _textpattern_content(ctl) -> list[str]:
    """读控件的 TextPattern 文档文本（QTextBrowser 的译文在这里，Name 属性拿不到）。"""
    import uiautomation as auto

    try:
        tp = ctl.GetPattern(auto.PatternId.TextPattern)
        if tp and tp.DocumentRange is not None:
            content = tp.DocumentRange.GetText(-1)
            if content:
                return [content.strip()[:600]]
    except Exception:
        pass
    return []


def _find_popup_text(pid: int) -> list[str]:
    """在目标进程的顶层窗口里收集可见文本（含深层，截断防慢）。"""
    import uiautomation as auto

    texts: list[str] = []
    try:
        root = auto.GetRootControl()
        for win in root.GetChildren():
            if win.ProcessId != pid:
                continue
            texts.append(win.Name or "")
            for ctl in win.GetChildren():
                texts.append(ctl.Name or "")
                for sub in ctl.GetChildren():
                    texts.append(sub.Name or "")
    except Exception as e:
        print(f"[e2e] uia walk error: {e}")
    return [t for t in texts if t]


if __name__ == "__main__":
    code = main()
    sys.exit(code)
