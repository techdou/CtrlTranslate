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
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QImage

from app.core.capture import restore_clipboard, save_clipboard

logger = logging.getLogger("ctrltrans.webai")

POLL_MS = 800            # 回复区轮询间隔
REPLY_STABLE_ROUNDS = 2  # 连续 N 轮文本不变且无停止按钮 → 流式结束
PAGE_LOAD_TIMEOUT_S = 30
TASK_TIMEOUT_S = 120     # 单任务总超时（含流式）
PASTE_SETTLE_MS = 1200   # 贴图后等缩略上传再填 prompt
UPLOAD_VERIFY_S = 8      # 上传后等待附件就绪的上限


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
    """承载窗口工厂：关闭按钮 = 最小化（保住 view——page 脱离 view 即失能）。"""

    @staticmethod
    def make(page, title: str):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QMainWindow

        class _Win(QMainWindow):
            def closeEvent(self, event):
                # 关闭 = 裸 page = load/runJavaScript 全失能（spike v3 实证），
                # 拦截关闭改最小化；程序退出时 QApplication 销毁不受影响
                if not getattr(self, "_allow_close", False):
                    event.ignore()
                    self.showMinimized()
                    return
                super().closeEvent(event)

        win = _Win()
        win.setWindowTitle(title)
        win.resize(1100, 780)
        view = QWebEngineView(win)
        view.setPage(page)
        win.setCentralWidget(view)
        return win


class WebAIEngine(QObject):
    """单会话串行引擎：同一时间只处理一个任务（网页会话本质串行）。

    流程：ensure_page（懒加载+登录检测）→ 动作（填字/贴图）→ 发送 →
    轮询回复区 → 稳定判定 → finished。任何一步失败走 failed（含中文
    用户可读原因）。
    """

    chunk = Signal(str, int)          # (流式增量, 任务号)
    finished = Signal(str, int)       # (完整回复, 任务号)
    failed = Signal(str, int)
    login_required = Signal()         # 网页未登录：上层应弹出窗口引导登录
    upload_done = Signal(bool, str)   # 文档上传结果 (ok, 用户可读信息)

    def __init__(self, site: str = "deepseek", parent: QObject | None = None):
        super().__init__(parent)
        self.adapter = ADAPTERS.get(site, DeepSeekAdapter)()
        self._page = None
        self._win = None        # 承载窗口（默认最小化；贴图/登录弹前台）
        self._poll: QTimer | None = None
        self._task = 0
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
        self._retract_after_task = False   # 任务前窗口是收着的才在收尾收回
        self._nav_retries = 0

    # ---- 对外 API ----

    @property
    def is_busy(self) -> bool:
        """有任务在途（调用方预检用，busy 时 submit 会被拒）。"""
        return self._phase not in ("idle", "ready")

    def submit_text(self, text: str) -> int:
        """提交纯文本任务（划词翻译）。返回任务号；忙时返回 -1。"""
        return self._submit(text, image=False)

    def submit_image(self, png_bytes: bytes, prompt: str) -> int:
        """提交图片任务（截图翻译）：贴图 + prompt 一起发送。"""
        self._image_bytes = png_bytes
        return self._submit(prompt, image=True)

    def translate(self, text: str, use_cache: bool = False) -> int:
        """与 Translator.translate 同名兼容：popup 统一入口直接切换引擎。
        use_cache 被忽略（网页会话自身有上下文，不落文本缓存）。"""
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
        logger.info("new session requested")
        self._phase = "loading"
        self._ns_attempted = False
        self._ns_deadline = time.monotonic() + PAGE_LOAD_TIMEOUT_S
        self._page.load(QUrl(self.adapter.url))
        self._start_poll(self._probe_new_session)
        return True

    def show_window(self) -> None:
        """把网页窗口弹到前台（登录引导 / 手动对话用）。"""
        if self._page is None:
            self._boot()  # 首次直接建 page+窗口并最小化，再弹出
        self._ensure_window()

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
        self._deadline = time.monotonic() + TASK_TIMEOUT_S
        if self._page is None:
            self._boot()
        else:
            self._ensure_ready()

    # ---- 任务装配 ----

    def _submit(self, text: str, image: bool) -> int:
        if self._phase not in ("idle", "ready"):
            logger.warning("busy (%s), task dropped", self._phase)
            return -1
        self._task += 1
        self._pending_text = text
        self._pending_image = image
        self._phase = "loading"
        self._deadline = time.monotonic() + TASK_TIMEOUT_S
        if self._page is None:
            self._boot()
        else:
            self._ensure_ready()
        return self._task

    def _boot(self) -> None:
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
        except ImportError:
            # 轻量版（构建时排除 WebEngine）没有该组件：API 模式照常，网页模式给出指引
            self._phase = "idle"
            self.failed.emit(
                "此构建未包含网页组件——请下载完整版（CtrlTranslate-Web），"
                "或在设置中关闭「网页版引擎」改用 API 模式", self._task)
            return

        # 用户数据跟项目惯例进 ~/.ctrltrans/，登录 cookie 长期有效
        from app.config import DATA_DIR
        storage = DATA_DIR / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("webai", self)
        self._profile.setPersistentStoragePath(str(storage))
        self._page = _WebAIPage.make(self._profile, self)
        # 渲染进程崩溃（显存/内存/Chromium bug）→ 作废任务并重建，下次任务自愈
        self._page.renderProcessTerminated.connect(self._on_render_crash)
        self._page.loadFinished.connect(self._on_load_finished)
        # page 必须挂在窗口 view 上（裸 page 的 load/runJavaScript 不工作，
        # spike v3 120s 超时实证）；窗口默认最小化——不抢焦点不占屏，渲染照常
        self._win = _WebAIWindow.make(
            self._page, f"CtrlTranslate · 网页翻译（{self.adapter.name}）")
        self._win.showMinimized()
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
        if self._phase not in ("idle", "ready"):
            self.failed.emit("网页渲染进程崩溃，已自动恢复——请重试本条翻译", self._task)
        # 销毁重建：page/view 全部弃用，下次任务走全新 _boot
        self._stop_poll()
        self._settle_clipboard()
        if self._win is not None:
            self._win._allow_close = True
            self._win.close()
            self._win = None
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        self._phase = "idle"

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
            self._ensure_window()
            self.login_required.emit()
            self._fail("网页版未登录——请在弹出的窗口中登录后再试")
            return
        if d.get("inputVisible"):
            # 输入框可用 = 页面就绪，直接开任务（有历史会话则上下文延续，是特性）
            self._phase = "ready"
            if self._upload_pending:
                self._do_upload_file()
            elif self._pending_image:
                self._do_paste_image()
            else:
                self._do_fill()
            return
        # 页面没就绪：首次/导航后 → 加载
        if not getattr(self, "_navigated", False):
            self._navigated = True
            self._page.load(QUrl(self.adapter.url))

    # ---- 轮询骨架 ----

    def _start_poll(self, handler) -> None:
        self._stop_poll()
        self._poll_handler = handler
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(lambda: self._tick(self._poll_handler))
        self._poll.start()

    def _stop_poll(self) -> None:
        if self._poll is not None:
            self._poll.stop()
            self._poll = None

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
            self._fail(f"任务超时（{TASK_TIMEOUT_S}s）——网页未响应或网络过慢")
            return
        self._run_js(self.adapter.PROBE, handler)

    def _fail(self, why: str) -> None:
        logger.warning("task %s failed: %s", self._task, why)
        upload = self._upload_pending or self._phase == "uploading"
        self._upload_pending = False
        self._cleanup_task()
        if upload:
            # 上传流程失败必须走 upload_done——failed 会撞 popup 任务号守卫被
            # 静默丢弃（上传时刻 popup 不持有本任务号），托盘零通知
            self.upload_done.emit(False, why)
        else:
            self.failed.emit(why, self._task)

    def _finish(self, text: str) -> None:
        logger.info("task %s finished (%d chars)", self._task, len(text))
        self._cleanup_task()
        self.finished.emit(text, self._task)

    def _cleanup_task(self) -> None:
        self._stop_poll()
        self._settle_clipboard()
        self._retract_window()
        self._phase = "idle"

    def _settle_clipboard(self) -> None:
        if self._clip_saved is not None:
            restore_clipboard(self._clip_saved)
            self._clip_saved = None

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
            self._phase = "reading"
            self._reply_prev = ""
            self._stable = 0
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
                self._phase = "idle"
                self._stop_poll()
                self.upload_done.emit(False, "上传入口未响应（站点可能改版），请打开网页窗口手动上传")
            return
        # chooseFiles 已回填路径：等站点上传完成（发送键解锁且输入框空 = 附件挂上）
        if d.get("sendEnabled") and not d.get("inputValue"):
            self._phase = "idle"
            self._stop_poll()
            logger.info("upload ok: attachment ready")
            self.upload_done.emit(True, "文档已上传，后续翻译/问答将携带该文档上下文")
            return
        if time.monotonic() > self._upload_deadline:
            self._phase = "idle"
            self._stop_poll()
            self.upload_done.emit(False, "上传超时——请打开网页窗口确认文件状态")

    # ---- 动作：贴图（真实键盘输入管线）----

    def _ensure_window(self):
        """贴图/登录需要可见窗口：把承载窗口弹到前台。"""
        if self._win is not None:
            self._win.showNormal()
            self._win.raise_()
            self._win.activateWindow()
            return
        # 理论到不了这（page 与窗口同生共死）；兜底重建
        self._win = _WebAIWindow.make(
            self._page, f"CtrlTranslate · 网页翻译（{self.adapter.name}）")
        self._win.show()

    def _do_paste_image(self) -> None:
        # 任务前窗口是收着的，收尾才收回；用户开着窗口手动对话时不抢
        self._retract_after_task = not (self._win is not None and self._win.isVisible())
        self._ensure_window()
        self._win.showNormal()
        self._win.raise_()
        self._win.activateWindow()
        self._phase = "pasting"
        QTimer.singleShot(400, self._paste_now)

    def _paste_now(self) -> None:
        import ctypes

        img = QImage.fromData(self._image_bytes, "PNG")
        if img.isNull():
            self._fail("截图数据无效")
            return
        self._clip_saved = save_clipboard()
        QGuiApplication.clipboard().setImage(img)
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
        self._retract_window()     # 缩回窗口，后续流程全后台
        self._do_fill()

    def _retract_window(self) -> None:
        if self._retract_after_task and self._win is not None and self._win.isVisible():
            self._win.showMinimized()
        self._retract_after_task = False

    # ---- 新会话 ----

    def _probe_new_session(self, d) -> None:
        if d is None:
            return
        url = d.get("url", "")
        if self.adapter.login_marker in url:
            self.login_required.emit()
            self._fail("网页版未登录")
            return
        # 判定成功：输入框可用 + 无回复残留 + 非流式（新会话是空画布）。
        # 不依赖根路径判断——DeepSeek 侧栏兜底成功后 url 是 /a/chat/s/<新id>。
        # 至少等 2s：根→旧会话的重定向发生前页面可能短暂"空画布"造成误判
        settled = time.monotonic() > self._ns_deadline - PAGE_LOAD_TIMEOUT_S + 2
        if settled and d.get("inputVisible") and not (d.get("replyText") or "").strip() \
                and not d.get("streaming"):
            self._phase = "ready"
            self._stop_poll()
            logger.info("new session ready (url=%s)", url)
            return
        # 根导航被重定向回旧会话 → 侧栏兜底，只试一次（防循环点击）
        if not self._ns_attempted and time.monotonic() > self._ns_deadline - PAGE_LOAD_TIMEOUT_S + 5:
            self._ns_attempted = True
            self._run_js(self.adapter.NEW_SESSION, lambda r: logger.info("new-session fallback: %s", r))
        if time.monotonic() > self._ns_deadline:
            self._phase = "idle" if self._phase == "loading" else self._phase
            self._stop_poll()
            logger.warning("new session not confirmed within timeout (url=%s)", url)

    # ---- 读流式回复 ----

    def _probe_reply(self, d) -> None:
        if self._phase != "reading" or d is None:
            return
        text = (d.get("replyText") or "").strip()
        if text and text == self._reply_prev and not d.get("streaming"):
            self._stable += 1
        elif text != self._reply_prev:
            if not self._reply_prev:
                self.chunk.emit(text, self._task)  # 首块：popup 用它替换 loading 占位
            elif text.startswith(self._reply_prev):
                self.chunk.emit(text[len(self._reply_prev):], self._task)
            # 非前缀扩展（站点重排/修正）不发 chunk：popup 只会追加渲染，
            # 全量重发会拼出脏文本——保持旧文不动，等 finished 全量覆盖纠正
            self._stable = 0
            self._reply_prev = text
        if self._stable >= REPLY_STABLE_ROUNDS and text:
            self._finish(text)
