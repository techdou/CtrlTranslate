"""CtrlTranslate 入口：组装所有服务并接线。

数据流：双击 Ctrl → 取词 → 弹窗 → 流式翻译 → （可选）TTS / 落库。
"""

from __future__ import annotations

import base64
import sys

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app import __version__
from app.config import load_config, save_config
from app.core.autostart import is_enabled as autostart_enabled
from app.core.autostart import set_enabled as set_autostart
from app.core.backup import BackupService
from app.core.capture import TextCaptureService, get_foreground_app
from app.core.singleton import SingleInstance
from app.core.hotkey import HotkeyService, SimpleHotkey
from app.core.translator import Translator
from app.core.webai import WebAIEngine
from app.core.tts import TTSService
from app.core.update import UpdateChecker, is_newer
from app.core.vocabulary import archive_terms, parse_terms, record_history
from app.db import database
from app.logger import setup_logger
from app.ui.library import LibraryWindow
from app.ui.overlay import ScreenshotOverlay
from app.ui.popup import TranslatePopup
from app.ui.settings import SettingsDialog
from app.ui.theme import build_qss, palette
from app.ui.tray import TrayController

# 网页模式注入指令：网页会话无 system 角色，指令拼在用户消息前、跟随每次
# 请求注入（长会话下首条指令会漂移——spike 实测）。学习版要求「【术语】」段，
# 与 API 模式学习模式的术语解析格式对齐（vocabulary.parse_terms），网页模式
# 完成后据此自动归档术语到生词本。
WEBAI_OCR_PROMPT = (
    "识别图片中的文字并翻译成中文，只输出译文。若含专业术语，在译文后另起"
    "「【术语】」段落，每行一条，格式：术语 — 中文解释。"
)
WEBAI_OCR_PROMPT_CONCISE = "识别图片中的文字并翻译成中文。只输出译文，不要解释。"
WEBAI_TERM_PROMPT = (
    "请解释计算机科研领域的专业术语「{text}」。\n"
    "第一行先给一句话通俗定义（50 字以内，中英对照术语名）；\n"
    "随后简练展开：它是什么、典型使用场景或例子。\n"
    "若回答涉及其他值得了解的专业术语，在最后另起「【术语】」段落，"
    "每行一条，格式：术语 — 一句话解释。"
)
WEBAI_OCR_TERM_PROMPT = (
    "识别图片中值得解释的专业术语（计算机/科研领域优先），"
    "逐条用通俗易懂、简练的语言解释：它是什么、典型场景或例子。\n"
    "最后另起「【术语】」段落，每行一条，格式：术语 — 一句话解释，把图中识别出的术语都汇总进去。"
)
WEBAI_TRANSLATE_PROMPT = (
    "请将下面的文字翻译成中文，只输出译文。若含专业术语，在译文后另起"
    "「【术语】」段落，每行一条，格式：术语 — 中文解释。\n\n{text}"
)
WEBAI_TRANSLATE_PROMPT_CONCISE = (
    "请将下面的文字翻译成中文，只输出译文，不要解释。\n\n{text}"
)


def toggle_webai_enabled(cfg: dict) -> bool:
    """翻转网页引擎开关并返回新状态（弹窗一键切的配置写入，纯函数便于单测）。"""
    web = cfg.setdefault("webai", {})
    web["enabled"] = not web.get("enabled", False)
    return web["enabled"]


def resolve_prompt(cfg: dict, key: str, default: str) -> str:
    """按模板键取用户自定义 prompt，留空回退内置默认（纯函数便于单测）。"""
    custom = (cfg.get("prompts", {}).get(key) or "").strip()
    return custom or default


def load_icon() -> QIcon:
    # 开发环境：项目 assets/；打包后：_MEIPASS/assets/ 或 exe 同级
    from pathlib import Path

    candidates = [
        Path(getattr(sys, "_MEIPASS", "")) / "assets" / "icon.png",
        Path(sys.argv[0]).resolve().parent / "assets" / "icon.png",
        Path(__file__).resolve().parent / "assets" / "icon.png",
    ]
    for c in candidates:
        if c.exists():
            icon = QIcon(str(c))
            icon.addFile(str(c))  # 显式挂一份默认尺寸，托盘/窗口/任务栏取同一图标
            return icon
    return QIcon()


class CtrlApp(QObject):
    """必须继承 QObject：热键/取词/更新检查的信号都从工作线程 emit，
    接收者带主线程亲和（QObject）Qt 才会排队回主线程执行；否则是直连——
    回调在钩子线程里直接建 GUI 对象（如 OCR 遮罩），违反 Qt 线程规则。"""

    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.cfg = load_config()

        # 基础
        self.logger = setup_logger()
        database.init_db()

        # 服务
        self.translator = Translator(self._cfg)
        self.tts = TTSService(self._cfg)
        self.capture = TextCaptureService(self._cfg)
        self.hotkey = HotkeyService(
            interval_ms=int(self.cfg["trigger"]["interval_ms"]),
            key=self.cfg["trigger"].get("key", "ctrl"),
        )
        self.popup = TranslatePopup(self._cfg, self.tts, self.translator)
        self.ocr_hotkey = SimpleHotkey()
        self.term_hotkey = SimpleHotkey()
        self.webai = WebAIEngine(parent=self)
        self.backup = BackupService()

        # 会话状态
        self._current_source = ""
        self._current_app = ""
        self._pipeline_task = -1  # 最近一次发起的任务号：finished 归档（历史/术语/TTS）守卫
        self._capture_intent = "translate"  # 本次取词意图：translate / term（on_captured 分流）
        self._task_kind = "translate"       # 在途任务类型：translate / ocr / term（归档分流）
        self._library: LibraryWindow | None = None
        self._settings: SettingsDialog | None = None
        self._overlay: ScreenshotOverlay | None = None

        # 托盘
        self.tray = TrayController(
            load_icon(),
            enabled=self.cfg["trigger"]["enabled"],
            key=self.cfg["trigger"].get("key", "ctrl"),
        )
        self.tray.set_autostart_checked(autostart_enabled())

        # 更新检查
        self.updater = UpdateChecker()
        self.updater.done.connect(self._on_update_info)
        self.updater.failed.connect(self._on_update_failed)
        self._update_manual = False
        self._release_url = ""
        self.tray.message_clicked.connect(self._open_release_page)

        self._wire()

        qapp.setStyleSheet(build_qss(palette(self.cfg["popup"]["theme"])))

    def _cfg(self) -> dict:
        return self.cfg

    # ---------------------------------------------------------------- 接线

    def _wire(self) -> None:
        self.hotkey.triggered.connect(self.on_hotkey)
        self.capture.captured.connect(self.on_captured)
        self.capture.failed.connect(self.on_capture_failed)
        self.translator.chunk.connect(self.popup.on_chunk)
        self.translator.finished.connect(self.on_translated)
        self.translator.failed.connect(self.popup.on_error)
        self.translator.fallback_started.connect(self.popup.on_fallback_started)
        self.translator.ocr_text_ready.connect(self.on_ocr_text)
        # 网页模式不弹自定义弹窗（弹窗=内嵌站点页面，见 WebAIEngine.present_window），
        # 引擎信号只接归档/提示通道
        self.webai.finished.connect(self.on_translated)
        self.webai.failed.connect(self.on_webai_failed)
        self.webai.login_required.connect(self.on_webai_login_required)
        self.webai.upload_done.connect(self.on_webai_upload_done)
        self.popup.ocr_retry_requested.connect(self.on_ocr_retry)
        self.popup.engine_toggle_requested.connect(self.on_engine_toggle)

        self.tray.settings_requested.connect(self.open_settings)
        self.tray.library_requested.connect(self.open_library)
        self.tray.ocr_requested.connect(self.on_ocr)
        self.tray.webai_window_requested.connect(self.webai.show_window)
        self.tray.webai_new_session.connect(self.on_webai_new_session)
        self.tray.webai_upload_requested.connect(self.on_webai_upload)
        self.tray.webai_engine_changed.connect(self.on_tray_engine_changed)
        self.tray.term_ocr_requested.connect(self.on_term_ocr)
        self.ocr_hotkey.triggered.connect(self.on_ocr)
        self.term_hotkey.triggered.connect(self.on_term)
        self.tray.enabled_changed.connect(self.on_enabled_changed)
        self.tray.autostart_changed.connect(self.on_autostart_changed)
        self.tray.check_update_requested.connect(self._check_update)
        self.tray.about_requested.connect(self.open_about)
        self.tray.quit_requested.connect(self.quit)

    def start(self) -> None:
        self.tray.show()
        enabled = self.cfg["trigger"].get("enabled", True)
        if enabled:
            if not self.hotkey.start():
                self.tray.notify("启动失败", "全局键盘钩子初始化失败（权限不足？）", 6)
        self.tray.set_webai_checked(self._webai_enabled())
        self._sync_ocr_hotkey()
        self._sync_term_hotkey()
        self.tray.act_ocr.setEnabled(self.cfg.get("ocr", {}).get("enabled", True))
        self.tray.act_term_ocr.setEnabled(self.cfg.get("ocr", {}).get("enabled", True))
        if not self.cfg["provider"].get("api_key"):
            QTimer.singleShot(
                900,
                lambda: self.tray.notify(
                    "CtrlTranslate 已启动",
                    "双击 Ctrl 翻译选中文字。请先到 托盘菜单 → 设置 填写 API Key"
                    "（推荐智谱 GLM Flash，免费且国内直连）。",
                    8,
                ),
            )
        QTimer.singleShot(5000, lambda: self._check_update(manual=False))  # 静默查一次

    # ---------------------------------------------------------------- 事件链

    def on_hotkey(self) -> None:
        if not self.cfg["trigger"].get("enabled", True):
            return
        self._capture_intent = "translate"
        self._current_app = get_foreground_app()
        self.capture.capture()

    def _webai_enabled(self) -> bool:
        return bool(self.cfg.get("webai", {}).get("enabled"))

    def on_term(self) -> None:
        """术语解释热键（划词）：取词后按意图分流到术语解释链。"""
        if not self.cfg.get("term", {}).get("enabled", True):
            return
        self._capture_intent = "term"
        self._current_app = get_foreground_app()
        self.capture.capture()

    def on_capture_failed(self, msg: str) -> None:
        # 术语解释取词失败（多半是没选中文字）→ 降级为框选截图解释，交互不断
        if self._capture_intent == "term":
            self._capture_intent = "translate"
            self.on_term_ocr()
            return
        self.popup.show_message(msg)

    def on_captured(self, text: str, method: str) -> None:
        intent, self._capture_intent = self._capture_intent, "translate"
        if intent == "term" and not text.strip():
            self.on_term_ocr()  # 取到空内容同样降级截图
            return
        self._current_source = text
        if intent == "term":
            self._start_term(text, method)
            return
        self._task_kind = "translate"
        if not self._webai_enabled():
            self._pipeline_task = self.popup.show_translation(text, method)
            return
        # 网页模式：不弹自定义弹窗，引擎窗口（内嵌站点页面）直接弹出承接，
        # 用户在站点页面里看流式回复、继续追问
        if self.webai.is_busy:
            self.tray.notify("网页翻译", "上一条还在处理，请稍候再试", 4)
            return
        # 网页会话无 system 角色：翻译指令拼进 payload 随消息注入；
        # source 保持原文用于历史记录与术语归档的上下文
        mode = self.cfg.get("translate", {}).get("mode", "study")
        tpl = (resolve_prompt(self.cfg, "translate_study", WEBAI_TRANSLATE_PROMPT)
               if mode == "study"
               else resolve_prompt(self.cfg, "translate_concise", WEBAI_TRANSLATE_PROMPT_CONCISE))
        self._pipeline_task = self.webai.submit_text(tpl.format(text=text))

    def _start_term(self, text: str, method: str) -> None:
        """术语解释（划词）：payload=术语解释模板；API 引擎走 raw（不套翻译 system）。"""
        self._task_kind = "term"
        tpl = resolve_prompt(self.cfg, "term", WEBAI_TERM_PROMPT)
        payload = tpl.format(text=text)
        if not self._webai_enabled():
            self._pipeline_task = self.popup.show_translation(
                text, method, payload=payload, raw=True)
            return
        if self.webai.is_busy:
            self.tray.notify("术语解释", "上一条还在处理，请稍候再试", 4)
            return
        self._pipeline_task = self.webai.submit_text(payload)

    # ---------------------------------------------------------------- OCR 截图翻译

    def on_ocr(self) -> None:
        self._launch_overlay(kind="ocr")

    def on_term_ocr(self) -> None:
        """术语截图解释：框选屏幕 → 识别术语 → 解释（托盘入口 / 热键降级）。"""
        self._launch_overlay(kind="term")

    def _launch_overlay(self, kind: str) -> None:
        if not self.cfg.get("ocr", {}).get("enabled", True):
            return
        if self._overlay is not None:  # 已在截图流程中，忽略重复触发
            return
        self._task_kind = kind
        self._current_app = "OCR"
        self._current_source = "（屏幕截图）"
        self._overlay = ScreenshotOverlay()
        self._overlay.selected.connect(self._on_ocr_selected)
        self._overlay.cancelled.connect(self._on_ocr_cancelled)
        self._overlay.show()

    def _on_ocr_selected(self, png: bytes) -> None:
        self._discard_overlay()
        if self._webai_enabled():
            if self.webai.is_busy:
                self.tray.notify("网页翻译", "上一条还在处理，请稍候再试", 4)
                return
            self._last_ocr_png = png  # 重试链：截图 bytes 在 main 手里
            if self._task_kind == "term":
                prompt = resolve_prompt(self.cfg, "ocr_term", WEBAI_OCR_TERM_PROMPT)
            else:
                mode = self.cfg.get("translate", {}).get("mode", "study")
                prompt = (resolve_prompt(self.cfg, "ocr_study", WEBAI_OCR_PROMPT)
                          if mode == "study"
                          else resolve_prompt(self.cfg, "ocr_concise", WEBAI_OCR_PROMPT_CONCISE))
            self._pipeline_task = self.webai.submit_image(png, prompt)
            return
        self._last_ocr_png = png  # OCR 重试链：popup 只发信号，截图在这里
        self._pipeline_task = -1  # 待 adopt_task 挂回真实任务
        if self._task_kind == "term":
            # API 两阶段：识别出的术语文本填进术语解释模板走 raw 链
            followup = resolve_prompt(self.cfg, "term", WEBAI_TERM_PROMPT)
        else:
            followup = None
        self.popup.show_translation("屏幕截图 OCR", method="ocr", request=False)
        tid = self.translator.translate_image(base64.b64encode(png).decode("ascii"),
                                              followup_prompt=followup)
        self.popup.adopt_task(tid)
        self._pipeline_task = tid

    def on_ocr_retry(self) -> None:
        """popup 重试按钮（OCR 态）回调：用最近一次截图重走识别链。"""
        png = getattr(self, "_last_ocr_png", None)
        if png:
            self._on_ocr_selected(png)

    def on_ocr_text(self, text: str, task_id: int) -> None:
        """OCR 第一阶段识别完成：把识别出的真实原文补进会话状态与弹窗预览，
        历史/生词本 context 记录的才是图片里的文字，而非"（屏幕截图）"占位。"""
        if task_id != self._pipeline_task:
            return
        if self._current_app == "OCR":
            self._current_source = text
            self.popup._set_source_preview(text, "原文 · 屏幕截图识别")

    def on_engine_toggle(self) -> None:
        """弹窗一键切换引擎：写配置（单入口）+ 同步设置页/托盘勾选 + 状态栏反馈。"""
        enabled = toggle_webai_enabled(self.cfg)
        save_config(self.cfg)
        self.popup.refresh_engine_button()
        self.popup._flash_status("已切换为网页版引擎，下次划词生效" if enabled
                                 else "已切换为 API 模式，下次划词生效")
        self.tray.set_webai_checked(enabled)
        # 设置窗开着时同步勾选，防保存时旧勾选覆盖刚切的引擎
        if self._settings is not None:
            self._settings.ck_webai.setChecked(enabled)

    def on_tray_engine_changed(self, enabled: bool) -> None:
        """托盘勾选切引擎：与弹窗一键切换共用同一配置入口（防两处状态分叉）。"""
        if enabled != self._webai_enabled():
            self.on_engine_toggle()
        else:
            self.tray.set_webai_checked(enabled)  # 勾选态与配置本就一致：复位勾选框

    def on_webai_failed(self, message: str, task_id: int) -> None:
        # 网页模式没有自定义弹窗承接错误：统一走托盘通知
        self.tray.notify("网页翻译", message, 8)

    def on_webai_login_required(self) -> None:
        # 引擎已弹窗口引导登录；错误文案经 failed（划词）或 upload_done（上传）
        # 单通道显示，这里再弹一条会先后覆盖、双消息冗余
        self.logger.info("webai login required; window shown by engine")

    def on_webai_new_session(self) -> None:
        if self.webai.new_session():
            self.tray.notify("网页翻译", "已开启新会话（上下文已清空）", 3)
        else:
            self.tray.notify("网页翻译", "网页引擎尚未启动——先「打开网页窗口」或划一次词", 5)

    def on_webai_upload(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            None, "选择要上传到网页会话的文档", "",
            "文档 (*.pdf *.docx *.txt *.md *.tex);;所有文件 (*.*)")
        if not path:
            return
        if not self._webai_enabled():
            self.tray.notify("网页翻译", "请先在 设置 → 翻译服务 勾选「网页版引擎」", 5)
            return
        self.webai.upload_file(path)

    def on_webai_upload_done(self, ok: bool, message: str) -> None:
        self.tray.notify("文档上传" if ok else "上传失败", message, 6 if ok else 8)

    def _on_ocr_cancelled(self) -> None:
        self._discard_overlay()

    def _discard_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.deleteLater()
            self._overlay = None

    def _sync_ocr_hotkey(self) -> None:
        """按当前配置注册/注销 OCR 截图热键（空串 = 禁用）。"""
        ocr = self.cfg.get("ocr", {})
        hotkey = ocr.get("hotkey", "") if ocr.get("enabled", True) else ""
        if hotkey and not self.ocr_hotkey.start(hotkey):
            self.tray.notify("OCR 热键", f"热键「{hotkey}」注册失败（格式无效或被占用）", 5)
        elif not hotkey:
            self.ocr_hotkey.stop()

    def _sync_term_hotkey(self) -> None:
        """按当前配置注册/注销术语解释热键（空串 = 禁用）。"""
        term = self.cfg.get("term", {})
        hotkey = term.get("hotkey", "") if term.get("enabled", True) else ""
        if hotkey and not self.term_hotkey.start(hotkey):
            self.tray.notify("术语解释热键", f"热键「{hotkey}」注册失败（格式无效或被占用）", 5)
        elif not hotkey:
            self.term_hotkey.stop()

    def on_translated(self, translated: str, task_id: int) -> None:
        # popup 渲染：任务号不匹配（网页模式没展示弹窗 / 旧任务）由其内部守卫丢弃
        self.popup.on_done(translated, task_id)
        if task_id != self._pipeline_task:
            return
        self._pipeline_task = -1  # 双保险：finished 每个任务只归档一次
        kind = self._task_kind  # 不重置：OCR/术语重试按钮在成功态也可点，重试须沿用同 kind
        record_history(self.cfg, self._current_source, translated, self._current_app)
        self._archive_terms_if_enabled(kind, translated)
        tts_cfg = self.cfg.get("tts", {})
        if tts_cfg.get("enabled") and tts_cfg.get("auto_play"):
            what = tts_cfg.get("auto_play_what", "source")
            text = self._current_source if what == "source" else translated
            if self._current_app == "OCR":
                text = translated  # 截图场景自动朗读播译文更直接
            QTimer.singleShot(120, lambda: self.tts.speak(text))  # 音色由 TTS 按内容语言自选

    def _archive_terms_if_enabled(self, kind: str, translated: str) -> None:
        """术语自动入库（webai.auto_terms 总开关）。

        - term 任务：主词（划词原文/识别文本首段）+ 解释首行入生词本，关联术语段附加；
          两引擎都入库（术语解释没有 ☆ 手动入口语义，靠开关总控）
        - translate/ocr 任务：仅网页引擎 + 学习模式（回复带【术语】段）；API 模式
          保留弹窗里手动 ☆ 收藏
        """
        if not self.cfg.get("webai", {}).get("auto_terms", True):
            return
        context = self._current_source.strip()[:200]
        if kind == "term":
            word = self._current_source.strip()
            definition = translated.strip().splitlines()[0][:200] if translated.strip() else ""
            # 首行常为「术语 — 定义」形态：note 只留定义段（词名已在 word 字段）
            for sep in (" — ", "—", " - "):
                if sep in definition:
                    tail = definition.split(sep, 1)[1].strip()
                    if tail:
                        definition = tail
                    break
            rows = []
            if word:
                rows.append((word[:500], definition))
            rows += parse_terms(translated)
            n = archive_terms(rows, context=context)
        elif self._webai_enabled() and \
                self.cfg.get("translate", {}).get("mode", "study") == "study":
            n = archive_terms(parse_terms(translated), context=context)
        else:
            return
        if n:
            self.logger.info("terms archived (%s): %d", kind, n)

    # ---------------------------------------------------------------- 窗口

    def open_settings(self) -> None:
        if self._settings is not None:
            self._settings.raise_()
            return
        dlg = SettingsDialog(self.cfg, self.translator, backup=self.backup)
        dlg.config_saved.connect(self.on_config_saved)
        dlg.finished.connect(lambda _=0: self._settings_closed())
        self._settings = dlg
        dlg.show()

    def _settings_closed(self) -> None:
        self._settings = None

    def open_library(self) -> None:
        if self._library is None:
            self._library = LibraryWindow(theme=self.cfg["popup"]["theme"], popup=self.popup)
        self._library.set_theme(self.cfg["popup"]["theme"])
        self._library.refresh()
        self._library.show()
        self._library.raise_()
        self._library.activateWindow()

    # ---------------------------------------------------------------- 更新与关于

    def _check_update(self, manual: bool = True) -> None:
        self._update_manual = manual
        self.updater.start()

    def _on_update_info(self, latest: str, url: str) -> None:
        self._release_url = url
        if is_newer(latest, __version__):
            self.tray.notify(
                "发现新版本",
                f"v{latest} 已发布（当前 v{__version__}）。点击本通知打开下载页。",
                10,
            )
        elif self._update_manual:
            self.tray.notify("检查更新", f"已是最新版本 v{__version__}", 5)

    def _on_update_failed(self, msg: str) -> None:
        if self._update_manual:
            self.tray.notify(
                "检查更新",
                "无法获取最新版本（仓库私有 / 尚无发布 / 网络不通）",
                5,
            )

    def _open_release_page(self) -> None:
        if self._release_url:
            QDesktopServices.openUrl(QUrl(self._release_url))

    def open_about(self) -> None:
        QMessageBox.about(
            None,
            "关于 CtrlTranslate",
            f"<b>CtrlTranslate</b> v{__version__}<br><br>"
            "双击触发键划词翻译 · 屏幕截图 OCR · 流式输出 · 术语收藏 · 生词本导出 Anki · WebDAV 备份<br><br>"
            f'<a href="https://github.com/techdou/CtrlTranslate">'
            f"github.com/techdou/CtrlTranslate</a>",
        )

    # ---------------------------------------------------------------- 配置变更

    def on_config_saved(self, new_cfg: dict) -> None:
        self.cfg = new_cfg
        save_config(self.cfg)

        self.hotkey.set_interval(int(new_cfg["trigger"]["interval_ms"]))
        self.hotkey.set_key(new_cfg["trigger"].get("key", "ctrl"))
        enabled = new_cfg["trigger"].get("enabled", True)
        if enabled:
            self.hotkey.start()
        else:
            self.hotkey.stop()
        self.tray.set_enabled(enabled)
        self.tray.set_trigger_key(new_cfg["trigger"].get("key", "ctrl"))
        self._sync_ocr_hotkey()
        self._sync_term_hotkey()
        self.tray.act_ocr.setEnabled(new_cfg.get("ocr", {}).get("enabled", True))
        self.tray.act_term_ocr.setEnabled(new_cfg.get("ocr", {}).get("enabled", True))
        self.tray.set_webai_checked(self._webai_enabled())

        self.popup._apply_style()
        self.qapp.setStyleSheet(build_qss(palette(new_cfg["popup"]["theme"])))
        if self._library is not None:
            self._library.set_theme(new_cfg["popup"]["theme"])

    def on_enabled_changed(self, enabled: bool) -> None:
        self.cfg["trigger"]["enabled"] = enabled
        save_config(self.cfg)
        if enabled:
            self.hotkey.start()
        else:
            self.hotkey.stop()
            self.tts.stop()
            self.popup.close_animated()
        self.tray.set_enabled(enabled)

    def on_autostart_changed(self, on: bool) -> None:
        if set_autostart(on):
            self.tray.set_autostart_checked(autostart_enabled())  # 以注册表实际状态为准
        else:
            self.tray.notify("开机自启", "设置失败（无法写入注册表）", 4)

    def quit(self) -> None:
        self.hotkey.stop()
        self.ocr_hotkey.stop()
        self.term_hotkey.stop()
        self.tts.stop()
        self.qapp.quit()


def main() -> int:
    QApplication.setApplicationName("CtrlTranslate")
    app = QApplication(sys.argv)
    app.setWindowIcon(load_icon())  # 任务栏/标题栏统一应用图标（托盘另有实例）
    app.setQuitOnLastWindowClosed(False)

    # 单实例：第二实例唤醒主实例弹设置窗后静默退出（双击 exe = "打开程序"意图被满足）
    single = SingleInstance()
    if not single.acquire():
        single.notify_primary()
        return 0
    app.aboutToQuit.connect(single.release)

    ctrl = CtrlApp(app)
    ctrl.start()
    single.activated.connect(ctrl.open_settings)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
