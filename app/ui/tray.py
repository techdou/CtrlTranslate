"""系统托盘：功能开关与入口。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger("ctrltrans.tray")

KEY_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}


class TrayController(QObject):
    settings_requested = Signal()
    library_requested = Signal()
    ocr_requested = Signal()
    enabled_changed = Signal(bool)
    autostart_changed = Signal(bool)
    check_update_requested = Signal()
    about_requested = Signal()
    message_clicked = Signal()     # 托盘气泡被点击（转发 QSystemTrayIcon.messageClicked）
    quit_requested = Signal()
    webai_window_requested = Signal()   # 打开内嵌网页窗口（登录 / 手动对话）
    webai_new_session = Signal()        # 网页会话开新主题（清上下文）
    webai_upload_requested = Signal()   # 上传文档到网页会话（上下文附件）
    webai_engine_changed = Signal(bool) # 勾选切换 API/网页引擎（网页模式无弹窗后的切换入口）

    def __init__(self, icon: QIcon, enabled: bool = True, key: str = "ctrl",
                 parent: QObject | None = None):
        super().__init__(parent)
        self._key_label = KEY_LABELS.get(key, KEY_LABELS["ctrl"])
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f"CtrlTranslate · 双击 {self._key_label} 划词翻译")

        menu = QMenu()
        self.act_enable = QAction(f"启用双击 {self._key_label} 取词", menu)
        self.act_enable.setCheckable(True)
        self.act_enable.setChecked(enabled)
        self.act_enable.toggled.connect(self.enabled_changed.emit)

        self.act_autostart = QAction("开机自启", menu)
        self.act_autostart.setCheckable(True)
        self.act_autostart.toggled.connect(self.autostart_changed.emit)

        act_settings = QAction("设置…", menu)
        act_settings.triggered.connect(self.settings_requested.emit)
        act_library = QAction("历史与生词本…", menu)
        act_library.triggered.connect(self.library_requested.emit)
        self.act_ocr = QAction("屏幕截图翻译…", menu)
        self.act_ocr.triggered.connect(self.ocr_requested.emit)
        act_update = QAction("检查更新…", menu)
        act_update.triggered.connect(self.check_update_requested.emit)
        act_about = QAction("关于 CtrlTranslate", menu)
        act_about.triggered.connect(self.about_requested.emit)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)

        # 网页版引擎子菜单（无网页任务时也可用：登录入口本就在这）
        web_menu = QMenu("网页翻译", menu)
        self.act_webai_engine = QAction("启用网页版引擎（划词走 DeepSeek）", web_menu)
        self.act_webai_engine.setCheckable(True)
        self.act_webai_engine.toggled.connect(self.webai_engine_changed.emit)
        act_web_open = QAction("打开网页窗口（登录 / 对话）…", web_menu)
        act_web_open.triggered.connect(self.webai_window_requested.emit)
        act_web_new = QAction("开启新会话（清上下文）", web_menu)
        act_web_new.triggered.connect(self.webai_new_session.emit)
        act_web_upload = QAction("上传文档到会话…", web_menu)
        act_web_upload.triggered.connect(self.webai_upload_requested.emit)
        web_menu.addAction(self.act_webai_engine)
        web_menu.addAction(act_web_open)
        web_menu.addAction(act_web_new)
        web_menu.addAction(act_web_upload)

        menu.addAction(self.act_enable)
        menu.addAction(self.act_autostart)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addAction(act_library)
        menu.addAction(self.act_ocr)
        menu.addMenu(web_menu)
        menu.addSeparator()
        menu.addAction(act_update)
        menu.addAction(act_about)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.messageClicked.connect(self.message_clicked.emit)

    def set_autostart_checked(self, on: bool) -> None:
        self.act_autostart.setChecked(on)

    def set_webai_checked(self, on: bool) -> None:
        """同步网页引擎勾选态（配置变更/弹窗切换后回调，不触发 toggled 之外的副作用——
        setChecked 在值未变时也不发 toggled，值变化时 main 侧 on_tray_engine_changed 幂等）。"""
        self.act_webai_engine.setChecked(on)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.settings_requested.emit()

    def show(self) -> None:
        self.tray.show()

    def notify(self, title: str, message: str, seconds: int = 4) -> None:
        self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, seconds * 1000)

    def set_enabled(self, enabled: bool) -> None:
        self.act_enable.setChecked(enabled)
        self.tray.setToolTip(
            f"CtrlTranslate · 双击 {self._key_label} 划词翻译"
            if enabled
            else "CtrlTranslate · 已暂停（托盘菜单可启用）"
        )

    def set_trigger_key(self, key: str) -> None:
        """触发键变更后同步菜单与 tooltip 文案（不改勾选状态）。"""
        self._key_label = KEY_LABELS.get(key, KEY_LABELS["ctrl"])
        self.act_enable.setText(f"启用双击 {self._key_label} 取词")
        if self.act_enable.isChecked():
            self.tray.setToolTip(f"CtrlTranslate · 双击 {self._key_label} 划词翻译")
