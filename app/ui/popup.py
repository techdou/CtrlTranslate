"""翻译弹窗：无边框置顶，跟随鼠标，流式打字机渲染，失焦自动关闭。

交互契约：
- show_translation(source) 后由 Translator 信号驱动 on_chunk/on_done/on_error
- Esc 关闭；点击其他应用（本应用整体失活）自动关闭；「钉住」后不自动关
- 按钮：读原文 / 读译文 / 收藏 / 复制 / 重试（关闭由 Esc 与失焦覆盖，不设按钮）
- 朗读中的按钮变为「停止」，再点即停
- 错误态：错误文案进正文区（可选中复制），无关按钮隐藏
"""

from __future__ import annotations

import html
import logging

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.db import database
from app.ui.theme import palette

logger = logging.getLogger("ctrltrans.popup")

SOURCE_PREVIEW_LIMIT = 120
RESULT_MAX_GROW = 360


class TranslatePopup(QWidget):
    def __init__(self, cfg_getter, tts, translator, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._tts = tts
        self._translator = translator
        self._source = ""
        self._translated = ""
        self._task_id = -1
        self._pinned = False
        self._speaking_btn: QPushButton | None = None
        self._pending_speak_btn: QPushButton | None = None
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.hide)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._set_status(""))

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self._build_ui()
        self._apply_style()
        QApplication.instance().installEventFilter(self)
        if hasattr(tts, "state_changed"):
            tts.state_changed.connect(self._on_tts_state)

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.source_label = QLabel()
        self.source_label.setObjectName("sourcePreview")
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.source_label, 1)

        self.btn_pin = QPushButton("钉住")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("钉住后弹窗不再因点击其他程序而关闭")
        self.btn_pin.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.btn_pin.setMinimumWidth(64)  # 容纳「已钉住」三字；Fixed 策略防隐藏原文后吃满整行
        self.btn_pin.toggled.connect(self._on_pin_toggled)
        head.addWidget(self.btn_pin, 0, Qt.AlignTop)
        root.addLayout(head)

        self.result_view = QTextBrowser()
        self.result_view.setOpenExternalLinks(False)
        self.result_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.result_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result_view.setFrameShape(QTextBrowser.NoFrame)
        root.addWidget(self.result_view, 1)

        self.status_label = QLabel()
        root.addWidget(self.status_label)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.btn_speak_source = QPushButton("读原文")
        self.btn_speak_trans = QPushButton("读译文")
        self.btn_star = QPushButton("收藏")
        self.btn_copy = QPushButton("复制")
        self.btn_retry = QPushButton("重试")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)

        self.btn_speak_source.clicked.connect(
            lambda: self._toggle_speak("en", self.btn_speak_source))
        self.btn_speak_trans.clicked.connect(
            lambda: self._toggle_speak("zh", self.btn_speak_trans))
        self.btn_star.clicked.connect(self._star)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_retry.clicked.connect(self._retry)

    def _apply_style(self) -> None:
        cfg = self._cfg_getter().get("popup", {})
        p = palette(cfg.get("theme", "dark"))
        self.setWindowOpacity(float(cfg.get("opacity", 0.96)))
        self.setMinimumWidth(int(cfg.get("width", 480)))
        self.setMaximumWidth(int(cfg.get("width", 480)) + 160)
        fs = int(cfg.get("font_size", 14))
        self.setStyleSheet(f"""
            TranslatePopup {{
                background: {p['bg']};
                border: 1px solid {p['border']};
                border-radius: 10px;
            }}
            QLabel#sourcePreview {{
                background: {p['source_bg']};
                border-radius: 6px;
                padding: 8px;
                font-size: {max(fs - 2, 12)}px;
                color: {p['text_dim']};
            }}
            QLabel {{ background: transparent; border: none; }}
            QTextBrowser {{
                background: transparent; border: none;
                font-size: {fs}px; color: {p['text']};
                selection-background-color: {p['accent']};
            }}
            QPushButton {{
                background: {p['panel']}; color: {p['text_dim']};
                border: 1px solid {p['border']}; border-radius: 6px;
                padding: 4px 12px; font-size: {max(fs - 2, 12)}px;
            }}
            QPushButton:hover {{ color: {p['accent']}; border-color: {p['accent']}; }}
            QPushButton:checked {{ color: {p['accent']}; border-color: {p['accent']}; }}
        """)
        self.result_view.setMinimumHeight(60)

    # ---------------------------------------------------------------- 生命周期

    def show_translation(self, source: str, method: str = "") -> None:
        """开始一次新的翻译展示。"""
        cfg = self._cfg_getter()
        p = palette(cfg.get("popup", {}).get("theme", "dark"))
        self._source = source
        self._translated = ""
        self._placeholder_active = True  # loading 占位在正文区，首块 chunk 需替换而非追加
        preview = source[:SOURCE_PREVIEW_LIMIT] + ("…" if len(source) > SOURCE_PREVIEW_LIMIT else "")
        via = " · 取词：UIA" if method == "uia" else ""
        self.source_label.setText(f"原文{via}\n{html.escape(preview)}")
        self.source_label.setVisible(True)
        # 上一次长译文撑高的 minHeight 会残留，先复位再由本次内容决定
        self.result_view.setMinimumHeight(60)
        # loading 占位放正文区（视线落点），首个 chunk 整体替换
        self.result_view.setHtml(f"<div style='color:{p['text_dim']};'>翻译中…</div>")
        self._set_status("")
        self.btn_star.setEnabled(True)
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)

        self._place_near_cursor()
        self.show()
        self.raise_()
        logger.info("popup shown: winId=%s visible=%s", self.winId(), self.isVisible())
        self.activateWindow()
        self._start_auto_close(cfg)

        self._task_id = self._translator.translate(source)
        return self._task_id

    def show_result(self, source: str, translated: str) -> None:
        """历史/生词本回看：直接展示已有译文，不发翻译请求（重试可重新发起）。"""
        cfg = self._cfg_getter()
        p = palette(cfg.get("popup", {}).get("theme", "dark"))
        self._source = source
        self._translated = translated
        self._task_id = -1  # 作废进行中的翻译回调
        self._placeholder_active = False
        preview = source[:SOURCE_PREVIEW_LIMIT] + ("…" if len(source) > SOURCE_PREVIEW_LIMIT else "")
        self.source_label.setText(f"原文\n{html.escape(preview)}")
        self.source_label.setVisible(True)
        self.result_view.setMinimumHeight(60)  # 清掉上次长译文残留的 minHeight
        self.result_view.setHtml(_format_result(translated or "（无译文）", p))
        self._set_status("")
        self.btn_star.setEnabled(True)
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)

        self._fit_height()
        self.show()
        self.raise_()
        self.activateWindow()
        self._start_auto_close(cfg)

    def show_message(self, message: str, error: bool = True) -> None:
        """不发起翻译，仅弹出一条提示（如取词/翻译失败）。"""
        p = palette(self._cfg_getter().get("popup", {}).get("theme", "dark"))
        self._task_id = -1  # 作废进行中的翻译回调
        self._source = ""
        self._translated = ""
        self._placeholder_active = False
        self.source_label.setText("")
        self.source_label.setVisible(False)  # 空文本时 padding+底色仍会渲染，整块隐藏
        self.result_view.setMinimumHeight(60)  # 同 show_translation：清掉上次残留的 minHeight
        # 错误是此刻唯一重要的信息：进正文区、可选中复制；无关按钮隐藏
        self.result_view.setHtml(f"<div style='color:{p['error']};'>{html.escape(message)}</div>")
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setVisible(False)
        self.btn_retry.setVisible(True)

        self._place_near_cursor()
        self.show()
        logger.info("popup showing message: %s", message[:60])
        self.raise_()
        self.activateWindow()

    def _place_near_cursor(self) -> None:
        pos = QCursor_pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        self.adjustSize()
        w, h = self.width(), min(max(self.sizeHint().height(), 200), int(avail.height() * 0.6))
        self.resize(w, h)
        x, y = pos.x() + 18, pos.y() + 18
        if x + w > avail.right():
            x = pos.x() - w - 12
        if y + h > avail.bottom():
            y = max(avail.top(), pos.y() - h - 12)
        self.move(x, y)

    def _fit_height(self) -> None:
        """译文区高度贴合内容（带上限），长译文不用拖滚动条读完。"""
        doc = self.result_view.document()
        doc.setTextWidth(self.result_view.viewport().width())  # 同步触发重新排版
        text_h = int(doc.size().height()) + 8
        self.result_view.setMinimumHeight(min(text_h, RESULT_MAX_GROW))
        self._place_near_cursor()

    def _start_auto_close(self, cfg: dict) -> None:
        secs = int(cfg.get("popup", {}).get("auto_close_s", 0))
        if secs > 0:
            self._auto_close_timer.start(secs * 1000)
        else:
            self._auto_close_timer.stop()

    # ---------------------------------------------------------------- 翻译回调（由 main 接线）

    def on_chunk(self, piece: str, task_id: int) -> None:
        if task_id != self._task_id:
            return
        if self._auto_close_timer.isActive():
            self._restart_auto_close_on_activity()
        sb = self.result_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 8
        if self._placeholder_active:
            self.result_view.setPlainText(piece)  # 替换 loading 占位
            self.result_view.moveCursor(QTextCursor.MoveOperation.End)  # setPlainText 会把光标重置到开头
            self._placeholder_active = False
        else:
            self.result_view.insertPlainText(piece)
        if at_bottom:
            sb.setValue(sb.maximum())

    def on_done(self, text: str, task_id: int) -> None:
        if task_id != self._task_id:
            return
        self._translated = text
        p = palette(self._cfg_getter().get("popup", {}).get("theme", "dark"))
        self.result_view.setHtml(_format_result(text, p))
        self._fit_height()
        self._set_status("")

    def on_error(self, message: str, task_id: int) -> None:
        if task_id != self._task_id:
            return
        self.show_message(f"翻译失败：{message}")

    # ---------------------------------------------------------------- 按钮

    def _toggle_speak(self, lang: str, btn: QPushButton) -> None:
        if self._speaking_btn is btn:  # 播放中再点同一个按钮 = 停止
            self._tts.stop()
            return
        self._pending_speak_btn = btn
        if lang == "en":
            self._tts.speak(self._source, "en")
        else:
            self._tts.speak(self._translated or "", "zh")

    def _on_tts_state(self, state: str) -> None:
        if self._speaking_btn is not None:
            idle_text = self._speaking_btn.property("idle_text")
            self._speaking_btn.setText(idle_text or "朗读")
            self._speaking_btn = None
        if state == "playing" and self._pending_speak_btn is not None:
            btn = self._pending_speak_btn
            self._speaking_btn = btn
            btn.setProperty("idle_text", btn.text())
            btn.setText("停止")
        self._pending_speak_btn = None

    def _star(self) -> None:
        word = self._source.strip()
        if not word:
            return
        note = self._translated.strip()[:400]
        try:
            database.upsert_word(word[:500], note=note, context="")
            self._flash_status("已加入生词本")
            self.btn_star.setEnabled(False)
        except Exception as e:
            self._set_status(f"收藏失败：{e}", error=True)

    def _copy(self) -> None:
        text = self._translated or self._source
        QApplication.clipboard().setText(text)
        self._flash_status("已复制" + ("译文" if self._translated else "原文"))

    def _retry(self) -> None:
        if self._source:
            self.show_translation(self._source)

    def _on_pin_toggled(self, on: bool) -> None:
        self._pinned = on
        self.btn_pin.setText("已钉住" if on else "钉住")

    # ---------------------------------------------------------------- 杂项

    def _set_status(self, text: str, error: bool = False) -> None:
        p = palette(self._cfg_getter().get("popup", {}).get("theme", "dark"))
        color = p["error"] if error else (p["accent"] if text else p["text_dim"])
        self.status_label.setText(html.escape(text))
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _flash_status(self, text: str) -> None:
        self._set_status(text)
        self._status_timer.start(2000)

    def _restart_auto_close_on_activity(self) -> None:
        cfg = self._cfg_getter()
        secs = int(cfg.get("popup", {}).get("auto_close_s", 0))
        if secs > 0:
            self._auto_close_timer.start(secs * 1000)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Escape, Qt.Key_Q):
            self.hide()
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        # 点击其他应用（本应用整体失活）→ 关闭弹窗（钉住时除外）。
        # 设置等本应用窗口间的切换不会触发 ApplicationDeactivate，安全。
        if (
            obj == QApplication.instance()
            and event.type() == QEvent.ApplicationDeactivate
            and self.isVisible()
            and not self._pinned
        ):
            self.hide()
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:
        self._auto_close_timer.stop()
        self._status_timer.stop()
        self._tts.stop()
        super().hideEvent(event)


def QCursor_pos():
    from PySide6.QtGui import QCursor

    return QCursor.pos()


def _format_result(text: str, p: dict) -> str:
    """译文完成后渲染：'【术语】' 段落做轻微强调。"""
    import re

    accent, border, body_color = p["accent"], p["border"], p["text"]
    parts = re.split(r"^【术语】\s*$", text, flags=re.MULTILINE)
    body = html.escape(parts[0].strip())
    if len(parts) > 1:
        terms = html.escape(parts[1].strip())
        body += (
            f"<div style='margin-top:10px;padding-top:8px;border-top:1px solid {border};'>"
            f"<span style='color:{accent};font-weight:600;font-size:12px;'>术语</span>"
            f"<pre style='white-space:pre-wrap;font-family:inherit;margin:4px 0 0;"
            f"color:{body_color};font-size:13px;'>{terms}</pre></div>"
        )
    return f"<div style='line-height:1.55;'>{body}</div>"
