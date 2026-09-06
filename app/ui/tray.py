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
    enabled_changed = Signal(bool)
    autostart_changed = Signal(bool)
    check_update_requested = Signal()
    about_requested = Signal()
    quit_requested = Signal()

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
        act_update = QAction("检查更新…", menu)
        act_update.triggered.connect(self.check_update_requested.emit)
        act_about = QAction("关于 CtrlTranslate", menu)
        act_about.triggered.connect(self.about_requested.emit)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)

        menu.addAction(self.act_enable)
        menu.addAction(self.act_autostart)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addAction(act_library)
        menu.addSeparator()
        menu.addAction(act_update)
        menu.addAction(act_about)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)

    def set_autostart_checked(self, on: bool) -> None:
        self.act_autostart.setChecked(on)

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
