"""WebEngine 改造冒烟：boot → preflight 登录预检 → 信号链（独立存储，零干扰）。

用独立临时 storage（不碰运行中实例的 webview 存储锁、不用真实登录态），
验证未登录路径的完整信号链——这正是"启用引擎→引导登录"的新用户体验路径：
1. boot 成功（page/profile/窗口建出，无异常）
2. preflight 探测到登录页：不发 failed（预检无任务语义）、发 login_required、
   状态到「未登录」、登录监测启动
3. status_changed 信号链真实发射
跑完 shutdown 干净退出。present_window 被 mock（不真弹窗打扰）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

tmp = Path(tempfile.mkdtemp(prefix="webai-smoke-"))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

app = QApplication([])

import app.config as cfgmod
cfgmod.DATA_DIR = tmp  # 独立存储：无登录态 → 走 preflight 未登录路径

from app.core.webai import WebAIEngine

eng = WebAIEngine()
eng.present_window = lambda: None  # 冒烟不真弹窗
status, fails, logins = [], [], []
eng.status_changed.connect(lambda s: status.append(s))
eng.failed.connect(lambda m, t: fails.append(m))
eng.login_required.connect(lambda: logins.append(True))

eng.preflight_login()

loop = QEventLoop()
QTimer.singleShot(22000, loop.quit)  # 真实站点加载 + 轮询窗口
loop.exec()

print("STATUS_TRACE:", status)
print("FAILED:", fails)
print("LOGIN_REQUIRED:", logins)
print("PHASE:", eng._phase)
print("WATCH_ACTIVE:", eng._poll is not None and eng._poll.isActive())

# 验收断言：未登录路径必须走到登录引导（否则 preflight 状态机有断点）。
# 须在 shutdown 之前取轮询状态（shutdown 会停掉它）。
ok = (logins == [True] and not fails
      and any("未登录" in s for s in status)
      and (eng._poll is not None and eng._poll.isActive()))
eng.shutdown()
print("SMOKE " + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
