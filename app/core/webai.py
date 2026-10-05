"""网页 AI 翻译引擎：内嵌站点网页版，自动完成"发请求 → 读流式回复"。

与 Translator（API 模式）平行的引擎，信号接口对齐（chunk/finished/failed），
上层接线可低成本切换。仅主线程（QtWebEngine 约束）。

spike 结论（scripts/webview_spike.py 可复验，改版后先跑它）：
- PySide6 6.11.2 runJavaScript 对象返回损坏 → 所有注入脚本 JSON.stringify 收尾
- React 受控输入框必须 native setter 赋值 + input 事件
- 贴图必须真实键盘输入（keybd_event Ctrl+V）：JS 派发 paste 被合成事件
  isTrusted 过滤；因此贴图任务需短暂显示并激活承载窗口
- 站点 selector 集中在 SiteAdapter，网页改版只改 adapter 一处
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage

from app.core.capture import restore_clipboard, save_clipboard

logger = logging.getLogger("ctrltrans.webai")

POLL_MS = 800            # 回复区轮询间隔
REPLY_STABLE_ROUNDS = 2  # 连续 N 轮文本不变且无停止按钮 → 流式结束
PAGE_LOAD_TIMEOUT_S = 30
TASK_TIMEOUT_S = 120     # 无进展总超时（loading/filling/sending 阶段上限）
STREAM_STALL_S = 30      # 流式回复无进展超时——有新文本即续期（长译文不再被总时长误杀）
PASTE_SETTLE_MS = 1200   # 贴图后等缩略上传再填 prompt
UPLOAD_VERIFY_S = 8      # 上传后等待附件就绪的上限
INSTRUCTION_REFRESH_N = 8  # 同指令连续任务每 N 次强制重注入一次（防长会话格式漂移）
HTTP_CACHE_MAX_BYTES = 50 * 1024 * 1024  # Chromium 磁盘缓存上限（默认无界）


# ---------------------------------------------------------------- 站点适配

def _js_wrap(body: str) -> str:
    """统一包装：JSON.stringify 收尾（runJavaScript 对象返回损坏的绕法）。"""
    return "JSON.stringify((() => {\n" + body + "\n})())"


class DeepSeekAdapter:
    """DeepSeek 网页版动作脚本。selector 改版只动这里。"""

    name = "deepseek"
    url = "https://chat.deepseek.com/"
    login_marker = "/sign_in"

    # 页面探测：登录态（输入框可见）、发送按钮解锁、最新回复、新会话侧栏入口
    PROBE = _js_wrap("""
      const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const ta = document.querySelector('textarea');
      const send = [...document.querySelectorAll('.ds-button--primary')]
        .find(x => x.className.includes('--circle'));
      const md = [...document.querySelectorAll('[class*="markdown"]')];
      return {
        url: location.href,
        inputVisible: !!(ta && vis(ta)),
        inputValue: ta ? ta.value : '',
        sendEnabled: send ? !send.className.includes('--disabled') : false,
        replyText: md.length ? md[md.length - 1].innerText.trim() : '',
        streaming: [...document.querySelectorAll('.ds-button')]
          .some(b => (b.innerText || '').includes('停止')),
      };
    """)

    # React 受控组件：原生 setter 赋值 + input 事件，直接 el.value= 不生效
    FILL = _js_wrap("""
      const ta = document.querySelector('textarea');
      if (!ta) return {ok: false, why: 'no textarea'};
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, __TEXT__);
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.focus();
      return {ok: true};
    """)

    SEND = _js_wrap("""
      const send = [...document.querySelectorAll('.ds-button--primary')]
        .find(x => x.className.includes('--circle'));
      if (!send) return {ok: false, why: 'no send button'};
      if (send.className.includes('--disabled')) return {ok: false, why: 'send disabled'};
      send.click();
      return {ok: true};
    """)

    # 新会话兜底：根导航恢复上次会话时，点侧栏含"新"的入口
    NEW_SESSION = _js_wrap("""
      const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const aside = document.querySelector('aside') || document.querySelector('[class*="sidebar"]');
      if (!aside) return {ok: false, why: 'no sidebar'};
      const cands = [...aside.querySelectorAll('a, button, [role=button], .ds-button')]
        .filter(el => {
          if (!vis(el)) return false;
          const t = (el.innerText || '').trim();
          return 0 < t.length && t.length < 20 && t.includes('新');
        });
      if (!cands.length) return {ok: false, why: 'no new-session entry'};
      cands[0].click();
      return {ok: true};
    """)

    @staticmethod
    def fill_js(text: str) -> str:
        return DeepSeekAdapter.FILL.replace("__TEXT__", json.dumps(text))

    @staticmethod
    def focus_js() -> str:
        return _js_wrap("""
          const ta = document.querySelector('textarea');
          if (!ta) return {ok: false};
          ta.focus();
          return {ok: true};
        """)

    # 触发站点自带的文件上传入口：直接点隐藏的 input[type=file]（spike 已确认
    # 其存在于聊天页）。Chromium 由此调用宿主 chooseFiles——引擎侧静默回填路径，
    # 不弹系统对话框，无需用户手势
    CLICK_FILE_INPUT = _js_wrap("""
      const inp = document.querySelector('input[type=file]');
      if (!inp) return {ok: false, why: 'no file input'};
      inp.click();
      return {ok: true};
    """)


ADAPTERS = {"deepseek": DeepSeekAdapter}
# 新站点接入（如 kimi / chatgpt / doubao / gemini）：
#   1. 复制 DeepSeekAdapter 为模板，改 name / url / login_marker；
#   2. 跑 scripts/webview_spike.py 把 PROBE 指到新站点，从 report.json 抄真实 selector，
#      逐个改 PROBE / FILL / SEND / NEW_SESSION / CLICK_FILE_INPUT；
#   3. 注册进 ADAPTERS，config.webai.site 即可选新站点。
#   注意：selector 必须真实页面验证过才注册——没验证的骨架宁可不 ship（切换了必坏）。


# ---------------------------------------------------------------- 引擎

class _WebAIPage:
    """QWebEnginePage 子类工厂：chooseFiles 静默回填（upload_file 用）。

    以工厂而非模块级类实现：PySide6 子类化需在 Qt 模块可用时定义，
    引擎保持 QtWebEngine 惰性导入（无该模块的环境 API 模式照常可用）。
    """

    @staticmethod
    def make(profile, parent):
        from PySide6.QtWebEngineCore import QWebEnginePage

        class _Page(QWebEnginePage):
            file_to_feed = None      # upload_file 设置；非空时静默回填该路径
            chooser_fired = False    # 本次 input.click 是否到达 chooseFiles

            def chooseFiles(self, mode, oldFiles, acceptedMimeTypes):
                self.chooser_fired = True
                if self.file_to_feed:
                    path, self.file_to_feed = self.file_to_feed, None
                    logger.info("chooseFiles: feed %s silently", path)
                    return [path]
                return super().chooseFiles(mode, oldFiles, acceptedMimeTypes)

        return _Page(profile, parent)


class _WebAIWindow:
    """承载窗口工厂：关闭按钮 = 隐藏（保住 view——page 脱离 view 即失能）。

    窗口几何（位置/尺寸）经 QSettings 持久化：用户摆过位置后，隐藏再唤回
    回到原位，而不是每次跳到光标旁；首次无记录才由 present_window 落位。
    """

    @staticmethod
    def make(page, title: str):
        from PySide6.QtCore import QSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QMainWindow

        settings = QSettings("techdou", "CtrlTranslate")

        class _Win(QMainWindow):
            def _save_geo(self) -> None:
                settings.setValue("webai/geometry", self.saveGeometry())

            def closeEvent(self, event):
                # 关闭 = 裸 page = load/runJavaScript 全失能（spike v3 实证），
                # 拦截关闭改隐藏：体验上等同关闭（任务栏不再占位），view/page
                # 保留——后台收流式回复、登录态不丢，下次任务/托盘入口再弹出；
                # 程序退出时 QApplication 销毁不受影响
                self._save_geo()
                self._geo_restored = True  # 用户此刻的摆放即有效位置（本进程内不再光标重定位）
                if not getattr(self, "_allow_close", False):
                    event.ignore()
                    self.hide()
                    return
                super().closeEvent(event)

        win = _Win()
        win.setWindowTitle(title)
        geo = settings.value("webai/geometry")
        win._geo_restored = bool(geo) and win.restoreGeometry(geo)
        if not win._geo_restored:
            win.resize(1100, 780)
        view = QWebEngineView(win)
        view.setPage(page)
        win.setCentralWidget(view)
        return win


class _ClipboardWorker(QObject):
    """剪贴板快照/恢复的串行后台执行器。

    GetClipboardData 对延迟渲染格式会跨进程同步等属主渲染、无超时——绝不能
    在 Qt 主线程调（UI 事件循环会被无限期冻结）。单 worker 线程 + 队列串行：
    同一时刻只有一个剪贴板操作在跑，save/restore 顺序天然保序；结果经 done
    信号排队回主线程（worker 创建于主线程，具备主线程信号亲和）。
    """

    done = Signal(str, object)  # (job_id, 结果或 None)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, job_id: str, fn) -> None:
        self._jobs.put((job_id, fn))

    def _run(self) -> None:
        while True:
            job_id, fn = self._jobs.get()
            try:
                result = fn()
            except Exception:
                logger.exception("clipboard job %s failed", job_id)
                result = None
            self.done.emit(job_id, result)


class WebAIEngine(QObject):
    """单会话串行引擎：同一时间只处理一个任务（网页会话本质串行）。

    流程：ensure_page（懒加载+登录检测）→ 窗口弹到前台（present_window，
    网页模式的"弹窗"就是承载站点页面的本窗口）→ 动作（填字/贴图）→ 发送 →
    轮询回复区 → 稳定判定 → finished。任何一步失败走 failed（含中文
    用户可读原因）。
    """

    chunk = Signal(str, int)          # (流式增量, 调用方 tag)
    finished = Signal(str, int)       # (完整回复, 调用方 tag)
    failed = Signal(str, int)
    login_required = Signal()         # 网页未登录：上层应弹出窗口引导登录
    login_ok = Signal()               # 登录监测发现用户已完成登录（划词即用）
    upload_done = Signal(bool, str)   # 文档上传结果 (ok, 用户可读信息)
    session_ready = Signal(bool, str) # 新会话异步结果 (ok, 用户可读信息)——
    # 请求发出≠成功：重定向回旧会话/侧栏兜底失败/超时都要真实回报，勿谎报
    status_changed = Signal(str)      # 引擎状态人话文案（托盘菜单状态行展示）

    def __init__(self, site: str = "deepseek", refresh_n: int = 0,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.adapter = ADAPTERS.get(site, DeepSeekAdapter)()
        # 指令重注入间隔可配（config.webai.instruction_refresh_n）；0 = 内置默认
        self._refresh_n = refresh_n or INSTRUCTION_REFRESH_N
        self._page = None
        self._win = None        # 承载窗口（boot 先最小化建出；任务触发即弹前台）
        self._poll: QTimer | None = None
        self._task = 0
        self._tag = 0           # 当前任务的调用方标签（随信号透传，见 submit_text）
        self._phase = "idle"    # idle/loading/ready/filling/sending/reading/pasting/uploading
        self._reply_prev = ""
        self._stable = 0
        self._deadline = 0.0
        self._pending_image = False
        self._pending_text = ""
        self._clip_saved: list | None = None
        self._upload_pending = False
        self._upload_path = ""
        self._upload_deadline = 0.0
        self._ns_attempted = False
        self._ns_deadline = 0.0
        self._paste_attempts = 0
        self._nav_retries = 0
        # 指令智能注入：网页会话无 system 角色，指令只能拼在用户消息里。
        # 每次都注入会刷屏；只注入一次长会话会漂移（spike 实测）——折中：
        # 同指令连续任务只发原文，每 INSTRUCTION_REFRESH_N 次强制重注入。
        self._instruction = ""       # 本任务携带的指令前缀（submit_text 传入）
        self._last_instruction = ""  # 上一次成功发出的指令
        self._since_inject = 0       # 距上次注入的连续裸发任务数
        self._paste_image = None     # 待上剪贴板的截图（快照完成前暂存）
        self._session_has_attachment = False  # 当前会话挂着文档附件（新会话即失效）
        self._queued: dict | None = None  # 忙时待发槽（替换语义：新划词顶掉旧待发）
        self._preflight = False      # 启用引擎后的登录预检中（ready 时不派发任务）
        self._login_watch_deadline = 0.0  # 登录监测截止（login 窗口弹出后等用户操作）
        self._login_watching = False # 登录监测进行中（收尾路径不覆盖「未登录」状态）
        self._rounds = 0             # 当前会话已完成任务数（状态行"会话第 N 轮"）
        # 剪贴板快照/恢复全部走串行后台线程（主线程零 Win32 剪贴板调用）
        self._clip_worker = _ClipboardWorker(self)
        self._clip_worker.done.connect(self._on_clip_job)

    # ---- 对外 API ----

    @property
    def is_busy(self) -> bool:
        """有任务在途（调用方预检用，busy 时 submit 会被拒）。"""
        return self._phase not in ("idle", "ready")

    def submit_text(self, text: str, instruction: str = "", tag: int = 0) -> int:
        """提交纯文本任务（划词翻译）。返回引擎内部任务号；忙时返回 -1（已入待发槽）。

        tag = 调用方展示任务号（popup 的展示号），随 chunk/finished/failed
        信号原样透传回调用方——上层守卫不依赖引擎内部计数，与 API 引擎的
        任务号空间彻底隔离（两引擎切换瞬间迟到信号不会串台弹窗）。
        缺省 0 = 用引擎内部任务号（兼容旧调用/单测）。

        instruction = 模板中 {text} 之前的指令前缀。与上次注入相同且距上次
        注入不足刷新间隔（config.webai.instruction_refresh_n）→ 只发原文
        （连续翻译不刷屏）；指令变化 / 超过刷新间隔 → 指令拼在原文前重新注入。
        instruction 为空（调用方自拼全量 payload，如术语解释模板 {text} 在
        中部）→ 恒定全量。
        """
        return self._submit(text, image=False, tag=tag,
                            instruction=(instruction or "").strip())

    def submit_image(self, png_bytes: bytes, prompt: str, tag: int = 0) -> int:
        """提交图片任务（截图翻译）：贴图 + prompt 一起发送。"""
        return self._submit(prompt, image=True, tag=tag, image_bytes=png_bytes)

    def translate(self, text: str, use_cache: bool = False, raw: bool = False) -> int:
        """与 Translator.translate 同名兼容：popup 统一入口直接切换引擎。
        use_cache 被忽略（网页会话自身有上下文，不落文本缓存）；
        raw 被忽略（网页 payload 本就是完整指令，无 system 概念）。"""
        return self.submit_text(text)

    def new_session(self) -> bool:
        """开新会话（清上下文）：根导航优先，侧栏兜底（只试一次）。
        引擎未启动（未 boot）时返回 False——调用方据此提示，勿谎报成功。"""
        if self._page is None:
            logger.info("new session ignored: engine not booted")
            return False
        self._task += 1  # 作废在途任务
        self._stop_poll()
        self._settle_clipboard()
        self._last_instruction = ""  # 新会话是空画布：首个任务重新注入指令
        self._since_inject = 0
        logger.info("new session requested")
        self._status("开启新会话…")
        self._phase = "loading"
        self._ns_attempted = False
        self._ns_deadline = time.monotonic() + PAGE_LOAD_TIMEOUT_S
        self._deadline = self._ns_deadline  # 通用 _tick 用：沿用旧任务 deadline 会首轮误杀
        self._page.load(QUrl(self.adapter.url))
        self._start_poll(self._probe_new_session)
        return True

    def shutdown(self) -> None:
        """程序退出时显式收尾：停轮询、关窗口、销毁 page/profile。

        这些对象若留给解释器关闭期 GC，析构发生在 QApplication 之后——
        Chromium 对象晚析构是退出阶段 access violation 的已知来源（CI 曾
        两次复现）。必须在 qapp.quit() 前调用。
        """
        try:
            self._stop_poll()
            self._phase = "idle"
            if self._win is not None:
                self._win._allow_close = True
                self._win.close()
                self._win = None
            if self._page is not None:
                self._page.deleteLater()
                self._page = None
            profile = getattr(self, "_profile", None)
            if profile is not None:
                profile.deleteLater()
                self._profile = None
        except Exception:
            logger.exception("webai shutdown error")

    def show_window(self) -> None:
        """手动打开网页窗口（托盘入口 / 登录引导）。"""
        if self._page is None:
            self._boot()  # 首次直接建 page+窗口并最小化，再弹出
        self.present_window()

    def preflight_login(self) -> None:
        """启用网页引擎后主动预检登录态（而非等首次划词失败才发现）。

        已登录：静默就绪；未登录：弹窗引导 + login_required（上层通知）+
        启动登录监测——用户登录完成后 login_ok（上层通知"划词即用"）。
        引擎忙（在途任务）时跳过：任务链自己的探测会覆盖登录检测。
        """
        if self._phase not in ("idle", "ready"):
            return
        self._preflight = True
        self._login_watching = False  # 预检接管轮询：旧监测终结
        self._phase = "loading"  # 探测器的 phase 守卫只认 loading（真机冒烟实锤过漏置的坑）
        self._status("检查登录状态…")
        if self._page is None:
            self._boot()
        else:
            self._ensure_ready()

    def _status(self, msg: str) -> None:
        self.status_changed.emit(msg)

    def set_refresh_n(self, n: int) -> None:
        """运行时更新指令重注入间隔（设置页保存后即时生效，无需重启）。"""
        n = int(n)
        if 1 <= n != self._refresh_n:
            self._refresh_n = n
            logger.info("instruction refresh interval -> %d", n)

    def _status_ready(self) -> None:
        """就绪态文案：带会话轮数，长会话提示开新会话（防指令格式漂移）。"""
        if self._rounds >= self._refresh_n:
            self._status(f"就绪 · 会话第 {self._rounds} 轮（建议开新会话）")
        else:
            self._status("就绪" if not self._rounds else f"就绪 · 会话第 {self._rounds} 轮")

    def present_window(self) -> None:
        """把网页窗口弹到前台并定位——登录引导 / 贴图 / 「在网页中继续」入口。

        已正常显示（用户摆过位置）只抬高不挪动；隐藏/最小化态恢复显示：
        有持久化几何（用户上次摆放）回到原位，否则首次定位到光标附近。"""
        if self._win is None:  # 理论到不了这（page 与窗口同生共死）；兜底重建
            self._win = _WebAIWindow.make(
                self._page, f"CtrlTranslate · 网页翻译（{self.adapter.name}）")
        win = self._win
        if win.isVisible() and not win.isMinimized():
            win.raise_()
            win.activateWindow()
            return
        if not getattr(win, "_geo_restored", False):
            self._place_near_cursor()
        win.showNormal()
        win.raise_()
        win.activateWindow()

    def _place_near_cursor(self) -> None:
        """窗口弹到光标右下（右/底出屏则夹回光标所在屏的可用区）。"""
        cur = QCursor.pos()
        screen = QGuiApplication.screenAt(cur) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        w, h = self._win.width(), self._win.height()
        x = cur.x() + 24
        y = cur.y() + 24
        if x + w > avail.right():
            x = avail.right() - w
        if y + h > avail.bottom():
            y = max(avail.top(), avail.bottom() - h)
        self._win.move(max(avail.left(), x), y)

    def upload_file(self, path: str) -> None:
        """把文档喂给网页会话（作为后续翻译/问答的上下文附件）。

        走站点自带上传：JS 点隐藏 input[type=file] → chooseFiles 静默回填。
        结果经 upload_done 信号回报。
        """
        from pathlib import Path

        if not Path(path).exists():
            self.upload_done.emit(False, f"文件不存在：{path}")
            return
        if self._phase not in ("idle", "ready"):
            self.upload_done.emit(False, f"引擎忙（{self._phase}），稍后再传")
            return
        self._upload_pending = True
        self._upload_path = str(Path(path).resolve())
        self._task += 1  # 作废在途任务
        self._phase = "loading"
        self._login_watching = False  # 上传接管轮询：旧监测终结
        self._deadline = time.monotonic() + TASK_TIMEOUT_S
        if self._page is None:
            self._boot()
        else:
            self._ensure_ready()

    # ---- 任务装配 ----

    def _submit(self, text: str, image: bool, tag: int = 0,
                instruction: str = "", image_bytes: bytes | None = None) -> int:
        if self._phase not in ("idle", "ready"):
            # 网页会话本质串行：忙时进替换队列（新划词顶掉旧待发槽），
            # 当前任务收尾后自动发出——上层无需预检重试。全量参数入槽，
            # 不碰任何在途任务字段（曾因预写 _instruction/_image_bytes 污染
            # 在途任务的注入判断与贴图内容）
            self._queued = {"text": text, "image": image, "tag": tag,
                            "instruction": instruction, "image_bytes": image_bytes}
            logger.info("task queued (busy in %s), replaces pending slot", self._phase)
            return -1
        self._task += 1
        self._tag = tag or self._task
        self._instruction = instruction
        self._login_watching = False  # 新任务接管轮询：登录监测语义终结（勿残留锁死状态文案）
        if image:
            self._image_bytes = image_bytes
        self._pending_inject = bool(self._instruction) and (
            self._instruction != self._last_instruction
            or self._since_inject >= self._refresh_n)
        self._pending_text = (
            f"{self._instruction}\n{text}" if self._pending_inject else text)
        self._pending_image = image
        self._phase = "loading"
        self._deadline = time.monotonic() + TASK_TIMEOUT_S
        if self._page is None:
            self._boot()
            if self._page is None:
                # boot 失败（环境缺 WebEngine）：failed 已随本任务 tag 发出，
                # 页面不存在、无窗口可呈现，直接返回让上层走 failed 通道提示
                return self._task
        # 文本任务不弹窗：回复走调用方弹窗（tag 信号链），不打断当前焦点；
        # 只有图片任务必须弹——贴图要真实键盘输入（isTrusted），
        # 未登录场景由 _probe_ensure 的登录分支按需弹窗
        if image:
            self.present_window()
        self._ensure_ready()
        return self._task

    def _drain_queued(self) -> None:
        """任务收尾后发出待发槽（替换语义：槽里永远是最新一次划词）。"""
        if not self._queued or self._phase not in ("idle", "ready"):
            return
        q, self._queued = self._queued, None
        logger.info("draining queued task")
        self._submit(q["text"], q["image"], tag=q.get("tag") or 0,
                     instruction=q.get("instruction", ""),
                     image_bytes=q.get("image_bytes"))

    def _drop_queued_with_signal(self) -> None:
        """作废待发槽并给被作废任务的 tag 发终止信号。

        排队任务的弹窗已在等它的结果（loading 骨架）——静默丢弃会让弹窗
        永远转圈。发 failed 让弹窗走出错误态（用户重试/切换引擎）。"""
        q, self._queued = self._queued, None
        if q and q.get("tag"):
            self.failed.emit("网页版未登录，本条已取消——请在弹出的窗口中登录后重试",
                             q["tag"])
            logger.info("queued task cancelled (login required), tag=%s", q["tag"])

    def _boot(self) -> None:
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
        except ImportError:
            # 非常规环境（如手工裁剪依赖）没有 WebEngine：API 模式照常，网页模式给出指引
            self._phase = "idle"
            self._preflight = False    # 绕过 _cleanup_task 的出口须显式清标记
            self._login_watching = False
            self.failed.emit(
                "当前运行环境缺少网页组件（QtWebEngine）——"
                "请在设置中关闭「网页版引擎」改用 API 模式", self._tag)
            return

        # 用户数据跟项目惯例进 ~/.ctrltrans/，登录 cookie 长期有效
        from app.config import DATA_DIR
        storage = DATA_DIR / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("webai", self)
        self._profile.setPersistentStoragePath(str(storage))
        self._profile.setHttpCacheMaximumSize(HTTP_CACHE_MAX_BYTES)
        self._page = _WebAIPage.make(self._profile, self)
        # 渲染进程崩溃（显存/内存/Chromium bug）→ 作废任务并重建，下次任务自愈
        self._page.renderProcessTerminated.connect(self._on_render_crash)
        self._page.loadFinished.connect(self._on_load_finished)
        # page 必须挂在窗口 view 上（裸 page 的 load/runJavaScript 不工作，
        # spike v3 120s 超时实证）；窗口默认最小化——不抢焦点不占屏，渲染照常
        self._win = _WebAIWindow.make(
            self._page, f"CtrlTranslate · 网页翻译（{self.adapter.name}）")
        self._win.showMinimized()
        self._status("启动中…")
        logger.info("webai page booted, storage=%s", storage)
        self._navigated = True
        self._page.load(QUrl(self.adapter.url))
        self._ensure_ready()  # boot 也走统一探测（登录检测/就绪分流）

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._nav_retries = 0
            return
        # 加载失败重导航（限 3 次防循环）；否则 _navigated 已置位、probe 永假，
        # 只能干等 120s 任务超时
        self._nav_retries += 1
        if self._nav_retries <= 3:
            logger.warning("page load failed, re-navigate (%d/3)", self._nav_retries)
            self._navigated = False
            self._page.load(QUrl(self.adapter.url))
        else:
            self._fail("网页加载失败（网络不通或站点不可达）")

    def _on_render_crash(self, status, code) -> None:
        logger.error("render process terminated: status=%s code=%s", status, code)
        tag = self._tag  # 崩溃报错须带当前任务 tag（内部 _task 会撞 API 引擎任务号空间）
        if self._phase not in ("idle", "ready"):
            self.failed.emit("网页渲染进程崩溃，已自动恢复——请重试本条翻译", tag)
        # 销毁重建：page/view/profile 全部弃用，下次任务走全新 _boot。
        # 手动清场不走 _cleanup_task：预检/监测标记必须显式清——残留的
        # _preflight 会把 drain 出的下一条真实任务当预检静默吞掉
        self._stop_poll()
        self._settle_clipboard()
        self._preflight = False
        self._login_watching = False
        self._last_instruction = ""  # 页面重建 = 新会话，指令重新注入
        self._since_inject = 0
        if self._win is not None:
            self._win._allow_close = True
            self._win.close()
            self._win = None
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        profile = getattr(self, "_profile", None)
        if profile is not None:
            # 旧 Profile 不删的话，下次 _boot 同名 Profile 指向同一持久化目录，
            # 撞 Cookies/leveldb 存储锁（cookie 丢失甚至初始化卡住），且反复
            # 崩溃重建会累积整套 Chromium 上下文句柄
            profile.deleteLater()
            self._profile = None
        self._phase = "idle"
        # 页面已重建就绪路径恢复：待发槽里的任务重发到新页面（自愈重试）
        self._drain_queued()

    def _ensure_ready(self) -> None:
        """页面活着就直接用（避免每次任务重载丢会话节奏），否则导航。"""
        self._deadline = time.monotonic() + TASK_TIMEOUT_S
        self._start_poll(self._probe_ensure)

    def _probe_ensure(self, d) -> None:
        if self._phase != "loading":
            return
        if d is None:
            return
        if self.adapter.login_marker in d.get("url", ""):
            self.present_window()
            self.login_required.emit()
            was_preflight = self._preflight
            self._preflight = False
            self._drop_queued_with_signal()  # 作废待发任务必须发终止信号（其弹窗在等）
            if not was_preflight:
                self._fail("网页版未登录——请在弹出的窗口中登录后再试")
            else:
                self._cleanup_task()  # 预检无任务语义：只收尾，不发 failed
            # watch 必须在 fail/cleanup 之后启动（它们的 _stop_poll 会杀掉
            # 刚启动的监测轮询）；"未登录"状态后置覆盖 _fail 刷出的"就绪"
            self._start_login_watch()
            self._status("未登录——请在窗口中登录")
            return
        if d.get("inputVisible"):
            if self._preflight:
                # 登录预检（或登录监测）确认就绪：不派发任务，静默待命；
                # 待发槽任务此时该发出（用户可能在监测期间划过词）
                self._preflight = False
                self._phase = "idle"
                self._stop_poll()
                self._status_ready()
                self._drain_queued()
                return
            # 输入框可用 = 页面就绪，直接开任务（有历史会话则上下文延续，是特性）
            self._phase = "ready"
            if self._upload_pending:
                self._status("上传中…")
                self._do_upload_file()
            elif self._pending_image:
                self._status("贴图中…")
                self._do_paste_image()
            else:
                self._status("发送中…")
                self._do_fill()
            return
        # 页面没就绪：首次/导航后 → 加载
        if not getattr(self, "_navigated", False):
            self._navigated = True
            self._page.load(QUrl(self.adapter.url))

    # ---- 登录监测（login 窗口弹出后等用户完成登录）----

    def _start_login_watch(self) -> None:
        self._login_watch_deadline = time.monotonic() + 300  # 给足用户操作时间
        # 通用 _tick 检查的是 _deadline——不刷新的话，上一任务遗留的过期
        # deadline 会在首轮 tick 就把监测当任务超时杀掉
        self._deadline = self._login_watch_deadline
        self._login_watching = True
        self._start_poll(self._probe_login_watch)
        logger.info("login watch started")

    def _probe_login_watch(self, d) -> None:
        if d is None:
            return
        if self._phase != "idle":
            self._login_watching = False  # 用户已开始新任务：监测退位，勿锁死状态文案
            return
        url = d.get("url", "")
        if self.adapter.login_marker not in url and d.get("inputVisible"):
            self._stop_poll()
            self._login_watching = False
            self._status_ready()
            self.login_ok.emit()
            logger.info("login confirmed")
            return
        if time.monotonic() > self._login_watch_deadline:
            self._login_watch_expired()

    def _login_watch_expired(self) -> None:
        """登录监测超时：静默结束（非任务失败——不发 failed，别吓用户）。"""
        self._stop_poll()
        self._login_watching = False
        self._status("未登录")
        logger.info("login watch expired")

    # ---- 轮询骨架 ----

    def _start_poll(self, handler) -> None:
        # 复用单个 QTimer 只换 handler：每次新建+只 stop 不删会随任务数累积 QObject
        self._poll_handler = handler
        if self._poll is None:
            self._poll = QTimer(self)
            self._poll.setInterval(POLL_MS)
            self._poll.timeout.connect(lambda: self._tick(self._poll_handler))
        self._poll.start()

    def _stop_poll(self) -> None:
        if self._poll is not None:
            self._poll.stop()

    def _run_js(self, js: str, cb) -> None:
        def wrapped(result):
            try:
                data = json.loads(result) if isinstance(result, str) and result else None
            except (ValueError, TypeError):
                data = None
            cb(data)
        self._page.runJavaScript(js, wrapped)

    def _tick(self, handler) -> None:
        if time.monotonic() > self._deadline:
            # 超时出口按操作类型分流：新会话/登录监测走各自的专用收尾
            # （_probe_* 内部检查 deadline 的路径覆盖不到这里——_tick 先拦）。
            # 绑定方法每次访问是新对象，须用 ==（is 恒 False）
            h = getattr(self, "_poll_handler", None)
            if h is not None and self._phase == "idle" and \
                    h == self._probe_login_watch:
                self._login_watch_expired()
                return
            if h is not None and h == self._probe_new_session:
                self._ns_timeout()
                return
            self._fail("任务超时——网页长时间无进展（网络过慢或站点无响应）")
            return
        self._run_js(self.adapter.PROBE, handler)

    def _fail(self, why: str) -> None:
        logger.warning("task %s failed: %s", self._task, why)
        upload = self._upload_pending or self._phase == "uploading"
        self._upload_pending = False
        tag = self._tag
        self._cleanup_task()
        if upload:
            # 上传流程失败必须走 upload_done——failed 会撞 popup 任务号守卫被
            # 静默丢弃（上传时刻 popup 不持有本任务号），托盘零通知
            self.upload_done.emit(False, why)
        else:
            self.failed.emit(why, tag)
        if self._login_watching:
            self._status("未登录——请在窗口中登录")  # 登录场景的失败别刷成"就绪"
        else:
            self._status_ready()

    def _finish(self, text: str) -> None:
        logger.info("task %s finished (%d chars)", self._task, len(text))
        tag = self._tag
        self._rounds += 1
        self._cleanup_task()
        self.finished.emit(text, tag)  # cleanup 可能 drain 新任务（换 _tag），先存再发
        self._status_ready()

    def _cleanup_task(self) -> None:
        self._stop_poll()
        self._settle_clipboard()
        self._phase = "idle"
        self._preflight = False  # 异常收尾兜底：预检标记残留会把下一条真实任务静默吞掉
        self._drain_queued()

    def _settle_clipboard(self) -> None:
        if self._clip_saved is not None:
            snapshot, self._clip_saved = self._clip_saved, None
            self._clip_worker.submit("restore", lambda: restore_clipboard(snapshot))

    # ---- 动作：填字 / 贴图 ----

    def _do_fill(self) -> None:
        self._phase = "filling"
        self._run_js(self.adapter.fill_js(self._pending_text), self._on_filled)

    def _on_filled(self, r) -> None:
        if self._phase != "filling":
            return
        if not (r and r.get("ok")):
            self._fail(f"页面结构可能已改版（填入失败：{r and r.get('why')}）")
            return
        self._phase = "sending"
        self._reply_prev = ""
        self._stable = 0
        self._start_poll(self._probe_send)

    def _probe_send(self, d) -> None:
        if self._phase != "sending":
            return
        if d is None:
            return
        if d.get("sendEnabled"):
            self._run_js(self.adapter.SEND, self._on_sent)

    def _on_sent(self, r) -> None:
        if self._phase != "sending":
            return
        if r and r.get("ok"):
            # 发送成功才推进注入状态（失败重试不算）：注入轮归零计数并记下
            # 指令；裸发轮累加，逼近刷新间隔后由下次任务强制重注入
            if self._pending_inject:
                self._last_instruction = self._instruction
                self._since_inject = 0
            else:
                self._since_inject += 1
            self._phase = "reading"
            self._reply_prev = ""
            self._stable = 0
            self._status("翻译中…")
            logger.info("task %s sent, reading stream…", self._task)
            self._start_poll(self._probe_reply)
        # 未解锁时下一轮 _probe_send 重试

    # ---- 动作：文档上传 ----

    def _do_upload_file(self) -> None:
        self._phase = "uploading"
        self._upload_pending = False
        self._upload_deadline = time.monotonic() + UPLOAD_VERIFY_S
        self._page.file_to_feed = self._upload_path
        self._page.chooser_fired = False
        self._run_js(self.adapter.CLICK_FILE_INPUT, lambda r: None)
        logger.info("upload: file input clicked, path=%s", self._upload_path)
        self._start_poll(self._probe_upload)

    def _probe_upload(self, d) -> None:
        if self._phase != "uploading" or d is None:
            return
        if not self._page.chooser_fired:
            if time.monotonic() > self._upload_deadline - UPLOAD_VERIFY_S + 3:
                self._cleanup_task()
                self._status_ready()  # 收尾刷新（否则状态停在"上传中…"）
                self.upload_done.emit(False, "上传入口未响应（站点可能改版），请打开网页窗口手动上传")
            return
        # chooseFiles 已回填路径：等站点上传完成（发送键解锁且输入框空 = 附件挂上）
        if d.get("sendEnabled") and not d.get("inputValue"):
            self._session_has_attachment = True
            logger.info("upload ok: attachment ready")
            self._cleanup_task()  # 停轮询 + drain 待发任务
            self._status_ready()
            self.upload_done.emit(
                True, "文档已上传，后续翻译/问答将携带该文档上下文"
                "（附件挂在当前会话，开启新会话后不再携带）")
            return
        if time.monotonic() > self._upload_deadline:
            self._cleanup_task()
            self._status_ready()
            self.upload_done.emit(False, "上传超时——请打开网页窗口确认文件状态")

    # ---- 动作：贴图（真实键盘输入管线）----

    def _do_paste_image(self) -> None:
        # 贴图需要真实键盘输入（isTrusted），窗口必须可见且持有系统焦点
        self.present_window()
        self._phase = "pasting"
        QTimer.singleShot(400, self._paste_now)

    def _paste_now(self) -> None:
        img = QImage.fromData(self._image_bytes, "PNG")
        if img.isNull():
            self._fail("截图数据无效")
            return
        # 剪贴板快照放串行后台线程：主线程同步快照遇延迟渲染格式会被属主
        # 进程的渲染请求无限期挂起（UI 冻结）；快照完成经信号回主线程继续
        self._paste_image = img
        self._clip_worker.submit("save", save_clipboard)

    def _on_clip_job(self, job_id: str, result) -> None:
        if job_id == "save":
            self._on_clip_saved(result)
        # "restore"：fire-and-forget，无需后续动作

    def _on_clip_saved(self, snapshot) -> None:
        """后台快照完成：置图并进入焦点校验→粘贴序列（主线程）。"""
        if self._phase != "pasting":
            return  # 任务已被作废/失败：图还没上剪贴板，无需恢复
        self._clip_saved = snapshot
        QGuiApplication.clipboard().setImage(self._paste_image)
        # keybd_event 属真实输入管线（isTrusted=true），Chromium 才接受贴图；
        # 键盘事件直达系统焦点窗口——先把 Qt 焦点给 view、JS 焦点给输入框
        self._win.setFocus()
        view = self._win.centralWidget()
        view.setFocus()
        self._run_js(self.adapter.focus_js(), lambda _r: None)
        # 激活/焦点是异步的，且可能被前台锁拒绝——发键前必须验证焦点，
        # 否则 Ctrl+V 会粘进用户当前窗口（污染输入）
        self._paste_attempts = 0
        QTimer.singleShot(300, self._check_focus_then_paste)

    def _check_focus_then_paste(self) -> None:
        if self._phase != "pasting":
            return
        self._run_js("JSON.stringify({f: document.hasFocus()})", self._on_focus_checked)

    def _on_focus_checked(self, r) -> None:
        if self._phase != "pasting":
            return
        if r and r.get("f"):
            self._send_ctrl_v()
            return
        self._paste_attempts += 1
        if self._paste_attempts >= 6:  # ~2s 内 6 次激活重试
            self._fail("无法激活网页窗口接收粘贴（前台被其他程序占用）——请重试")
            return
        self._win.raise_()
        self._win.activateWindow()
        self._run_js(self.adapter.focus_js(), lambda _r: None)
        QTimer.singleShot(300, self._check_focus_then_paste)

    def _send_ctrl_v(self) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        VK_CONTROL, VK_V, KEYUP = 0x11, ord("V"), 0x0002
        for vk, flags in ((VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYUP), (VK_CONTROL, KEYUP)):
            user32.keybd_event(vk, 0, flags, 0)
            time.sleep(0.01)
        logger.info("image pasted (%d bytes png)", len(self._image_bytes))
        QTimer.singleShot(PASTE_SETTLE_MS, self._after_paste)

    def _after_paste(self) -> None:
        if self._phase != "pasting":
            return
        self._settle_clipboard()   # 立刻恢复用户剪贴板（图已进网页）
        # 贴图使命完成：窗口是专为 isTrusted 键盘输入弹出的，收起不打扰；
        # 流式回复走调用方弹窗（tag 信号链），追问时「在网页中继续」唤回
        if self._win is not None and self._win.isVisible():
            self._win.hide()
            logger.info("web window hidden after paste (reply via popup)")
        self._do_fill()

    # ---- 新会话 ----

    def _probe_new_session(self, d) -> None:
        if d is None:
            return
        url = d.get("url", "")
        if self.adapter.login_marker in url:
            self.login_required.emit()
            self._drop_queued_with_signal()  # 作废待发任务必须发终止信号
            self._fail("网页版未登录")
            self.session_ready.emit(False, "新会话开启失败：网页版未登录")
            self._start_login_watch()  # fail 之后启动（其 _stop_poll 不杀监测）
            self._status("未登录——请在窗口中登录")
            return
        # 判定成功：输入框可用 + 无回复残留 + 非流式（新会话是空画布）。
        # 不依赖根路径判断——DeepSeek 侧栏兜底成功后 url 是 /a/chat/s/<新id>。
        # 至少等 2s：根→旧会话的重定向发生前页面可能短暂"空画布"造成误判
        settled = time.monotonic() > self._ns_deadline - PAGE_LOAD_TIMEOUT_S + 2
        if settled and d.get("inputVisible") and not (d.get("replyText") or "").strip() \
                and not d.get("streaming"):
            self._phase = "ready"
            self._stop_poll()
            had_attachment = self._session_has_attachment
            self._session_has_attachment = False
            self._rounds = 0  # 新会话空画布：轮数与漂移提示重新计数
            logger.info("new session ready (url=%s)", url)
            self._status_ready()
            self.session_ready.emit(
                True,
                "会话已清空（文档附件不再携带）" if had_attachment
                else "已开启新会话（上下文已清空）")
            self._drain_queued()  # 新会话期间划的词此时发出
            return
        # 根导航被重定向回旧会话 → 侧栏兜底，只试一次（防循环点击）
        if not self._ns_attempted and time.monotonic() > self._ns_deadline - PAGE_LOAD_TIMEOUT_S + 5:
            self._ns_attempted = True
            self._run_js(self.adapter.NEW_SESSION, lambda r: logger.info("new-session fallback: %s", r))
        if time.monotonic() > self._ns_deadline:
            self._ns_timeout()

    def _ns_timeout(self) -> None:
        """新会话超时收尾：真实回报 session_ready(False) + 发出待发任务。

        _tick 的超时分流与探测器内部共用本出口——只走一边。"""
        logger.warning("new session not confirmed within timeout")
        self._phase = "idle" if self._phase == "loading" else self._phase
        self._status_ready()  # 收尾刷新状态（否则停在"开启新会话…"）
        self._cleanup_task()  # stop + idle + drain：待发任务发到现有会话
        self.session_ready.emit(
            False, "新会话未确认（站点响应慢或改版）——上下文可能未清空，"
            "可打开网页窗口手动确认")

    # ---- 读流式回复 ----

    def _probe_reply(self, d) -> None:
        if self._phase != "reading" or d is None:
            return
        text = (d.get("replyText") or "").strip()
        if text and text == self._reply_prev and not d.get("streaming"):
            self._stable += 1
        elif text != self._reply_prev:
            # 流式有新文本 = 有进展：滚动续期，长译文不因总时长超时被误杀
            self._deadline = time.monotonic() + STREAM_STALL_S
            if not self._reply_prev:
                self.chunk.emit(text, self._tag)  # 首块：popup 用它替换 loading 占位
            elif text.startswith(self._reply_prev):
                self.chunk.emit(text[len(self._reply_prev):], self._tag)
            # 非前缀扩展（站点重排/修正）不发 chunk：popup 只会追加渲染，
            # 全量重发会拼出脏文本——保持旧文不动，等 finished 全量覆盖纠正
            self._stable = 0
            self._reply_prev = text
        if self._stable >= REPLY_STABLE_ROUNDS and text:
            self._finish(text)
