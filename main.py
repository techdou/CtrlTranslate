"""CtrlTranslate 入口：组装所有服务并接线。

数据流：双击 Ctrl → 取词 → 弹窗 → 流式翻译 → （可选）TTS / 落库。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.config import DATA_DIR, load_config, save_config
from app.core.autostart import is_enabled as autostart_enabled
from app.core.autostart import set_enabled as set_autostart
from app.core.capture import TextCaptureService, get_foreground_app
from app.core.hotkey import HotkeyService
from app.core.translator import Translator
from app.core.tts import TTSService
from app.core.vocabulary import record_history
from app.db import database
from app.logger import setup_logger
from app.ui.library import LibraryWindow
from app.ui.popup import TranslatePopup
from app.ui.settings import SettingsDialog
from app.ui.theme import build_qss, palette
from app.ui.tray import TrayController

ICON_PATH = DATA_DIR / "assets" / "icon.png"


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
            return QIcon(str(c))
    return QIcon()


class CtrlApp:
    def __init__(self, qapp: QApplication):
        self.qapp = qapp
        self.cfg = load_config()

        # 基础
        self.logger = setup_logger()
        database.init_db()

        # 服务
        self.translator = Translator(self._cfg)
        self.tts = TTSService(self._cfg)
        self.capture = TextCaptureService(self._cfg)
        self.hotkey = HotkeyService(interval_ms=int(self.cfg["trigger"]["interval_ms"]))
        self.popup = TranslatePopup(self._cfg, self.tts, self.translator)

        # 会话状态
        self._current_source = ""
        self._current_app = ""
        self._library: LibraryWindow | None = None
        self._settings: SettingsDialog | None = None

        # 托盘
        self.tray = TrayController(load_icon(), enabled=self.cfg["trigger"]["enabled"])
        self.tray.set_autostart_checked(autostart_enabled())
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

        self.tray.settings_requested.connect(self.open_settings)
        self.tray.library_requested.connect(self.open_library)
        self.tray.enabled_changed.connect(self.on_enabled_changed)
        self.tray.autostart_changed.connect(self.on_autostart_changed)
        self.tray.quit_requested.connect(self.quit)

    def start(self) -> None:
        self.tray.show()
        enabled = self.cfg["trigger"].get("enabled", True)
        if enabled:
            if not self.hotkey.start():
                self.tray.notify("启动失败", "全局键盘钩子初始化失败（权限不足？）", 6)
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

    # ---------------------------------------------------------------- 事件链

    def on_hotkey(self) -> None:
        if not self.cfg["trigger"].get("enabled", True):
            return
        self._current_app = get_foreground_app()
        self.capture.capture()

    def on_captured(self, text: str, method: str) -> None:
        self._current_source = text
        self.popup.show_translation(text, method)

    def on_translated(self, translated: str, task_id: int) -> None:
        self.popup.on_done(translated, task_id)
        if self.popup._task_id != task_id:
            return
        record_history(self.cfg, self._current_source, translated, self._current_app)
        tts_cfg = self.cfg.get("tts", {})
        if tts_cfg.get("enabled") and tts_cfg.get("auto_play"):
            what = tts_cfg.get("auto_play_what", "source")
            text = self._current_source if what == "source" else translated
            lang = "en" if what == "source" else "zh"
            QTimer.singleShot(120, lambda: self.tts.speak(text, lang))

    # ---------------------------------------------------------------- 窗口

    def open_settings(self) -> None:
        if self._settings is not None:
            self._settings.raise_()
            return
        dlg = SettingsDialog(self.cfg, self.translator)
        dlg.config_saved.connect(self.on_config_saved)
        dlg.finished.connect(lambda _=0: self._settings_closed())
        self._settings = dlg
        dlg.show()

    def _settings_closed(self) -> None:
        self._settings = None

    def open_library(self) -> None:
        if self._library is None:
            self._library = LibraryWindow(theme=self.cfg["popup"]["theme"])
        self._library.set_theme(self.cfg["popup"]["theme"])
        self._library.refresh()
        self._library.show()
        self._library.raise_()
        self._library.activateWindow()

    # ---------------------------------------------------------------- 配置变更

    def on_config_saved(self, new_cfg: dict) -> None:
        self.cfg = new_cfg
        save_config(self.cfg)

        self.hotkey.set_interval(int(new_cfg["trigger"]["interval_ms"]))
        enabled = new_cfg["trigger"].get("enabled", True)
        if enabled:
            self.hotkey.start()
        else:
            self.hotkey.stop()
        self.tray.set_enabled(enabled)

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
            self.popup.hide()
        self.tray.set_enabled(enabled)

    def on_autostart_changed(self, on: bool) -> None:
        if set_autostart(on):
            self.tray.set_autostart_checked(autostart_enabled())  # 以注册表实际状态为准
        else:
            self.tray.notify("开机自启", "设置失败（无法写入注册表）", 4)

    def quit(self) -> None:
        self.hotkey.stop()
        self.tts.stop()
        self.qapp.quit()


def main() -> int:
    QApplication.setApplicationName("CtrlTranslate")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA_DIR / "ctrltrans.lock"))
    if not lock.tryLock(50):
        QMessageBox.information(
            None, "CtrlTranslate", "已在运行中，请查看系统托盘（可能已折叠）。"
        )
        return 0

    ctrl = CtrlApp(app)
    ctrl.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
