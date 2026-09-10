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
from app.core.vocabulary import record_history
from app.db import database
from app.logger import setup_logger
from app.ui.library import LibraryWindow
from app.ui.overlay import ScreenshotOverlay
from app.ui.popup import TranslatePopup
from app.ui.settings import SettingsDialog
from app.ui.theme import build_qss, palette
from app.ui.tray import TrayController

# 网页模式截图翻译指令：跟随每次请求注入（网页会话无 system 角色，
# 且长会话下首条指令会漂移——spike 实测）
WEBAI_OCR_PROMPT = "识别图片中的文字，翻译成中文。只输出译文，不要解释。"


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
        self.webai = WebAIEngine(parent=self)
        self.backup = BackupService()

        # 会话状态
        self._current_source = ""
        self._current_app = ""
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
        self.capture.failed.connect(lambda msg: self.popup.show_message(msg))
        self.translator.chunk.connect(self.popup.on_chunk)
        self.translator.finished.connect(self.on_translated)
        self.translator.failed.connect(self.popup.on_error)
        self.translator.fallback_started.connect(self.popup.on_fallback_started)
        self.webai.chunk.connect(self.popup.on_chunk)
        self.webai.finished.connect(self.on_translated)
        self.webai.failed.connect(self.popup.on_error)
        self.webai.login_required.connect(self.on_webai_login_required)

        self.tray.settings_requested.connect(self.open_settings)
        self.tray.library_requested.connect(self.open_library)
        self.tray.ocr_requested.connect(self.on_ocr)
        self.ocr_hotkey.triggered.connect(self.on_ocr)
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
        self._sync_ocr_hotkey()
        self.tray.act_ocr.setEnabled(self.cfg.get("ocr", {}).get("enabled", True))
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
        self._current_app = get_foreground_app()
        self.capture.capture()

    def _webai_enabled(self) -> bool:
        return bool(self.cfg.get("webai", {}).get("enabled"))

    def on_captured(self, text: str, method: str) -> None:
        self._current_source = text
        engine = self.webai if self._webai_enabled() else None
        self.popup.show_translation(text, method, engine=engine)

    # ---------------------------------------------------------------- OCR 截图翻译

    def on_ocr(self) -> None:
        if not self.cfg.get("ocr", {}).get("enabled", True):
            return
        if self._overlay is not None:  # 已在截图流程中，忽略重复触发
            return
        self._current_app = "OCR"
        self._current_source = "（屏幕截图）"
        self._overlay = ScreenshotOverlay()
        self._overlay.selected.connect(self._on_ocr_selected)
        self._overlay.cancelled.connect(self._on_ocr_cancelled)
        self._overlay.show()

    def _on_ocr_selected(self, png: bytes) -> None:
        self._discard_overlay()
        self.popup.show_translation("屏幕截图 OCR", method="ocr", request=False)
        if self._webai_enabled():
            tid = self.webai.submit_image(png, WEBAI_OCR_PROMPT)
        else:
            tid = self.translator.translate_image(base64.b64encode(png).decode("ascii"))
        self.popup.adopt_task(tid)

    def on_webai_login_required(self) -> None:
        self.popup.show_message("网页版未登录——请在弹出的网页窗口中登录 DeepSeek 后重试")

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

    def on_translated(self, translated: str, task_id: int) -> None:
        self.popup.on_done(translated, task_id)
        if self.popup._task_id != task_id:
            return
        record_history(self.cfg, self._current_source, translated, self._current_app)
        tts_cfg = self.cfg.get("tts", {})
        if tts_cfg.get("enabled") and tts_cfg.get("auto_play"):
            what = tts_cfg.get("auto_play_what", "source")
            text = self._current_source if what == "source" else translated
            if self._current_app == "OCR":
                text = translated  # 截图场景没有原文文本，播译文
            QTimer.singleShot(120, lambda: self.tts.speak(text))  # 音色由 TTS 按内容语言自选

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
        self.tray.act_ocr.setEnabled(new_cfg.get("ocr", {}).get("enabled", True))

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
