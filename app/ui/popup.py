"""翻译弹窗：无边框置顶，锚定取词时的鼠标位置（流式重排不追实时鼠标），流式打字机渲染，失焦自动关闭。

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
import re

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QTextCursor
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
CURSOR_OFFSET = 24  # 弹窗离锚点（取词时鼠标位置）的偏移，右下与翻转侧同距
RESULT_GROW_RATIO = 0.45  # 译文区高度上限 = 锚点屏可用高度的比例；小屏 120px 兜底
GROW_THROTTLE_MS = 300  # 流式输出期间窗口跟随长高的最小间隔，防逐 chunk 抖动


def _derived_fs(fs: int) -> tuple[int, int]:
    """由用户正文字号推导辅助区字号：(小号, 术语号)。

    状态行/术语标题用小号（fs-2, 下限 12），术语正文略大（fs-1, 下限 13）
    ——术语是读外刊最需要看清的部分，不能比状态行还小。"""
    return max(fs - 2, 12), max(fs - 1, 13)
_TERM_SPLIT = re.compile(r"^【术语】\s*$", re.MULTILINE)
_TERM_SEPS = [" — ", "—", " - ", " – ", "-"]


def _parse_terms(text: str) -> list[tuple[str, str]]:
    """解析译文里的【术语】段：每行一条 (术语, 含义)。分隔符容错多种破折号。"""
    parts = _TERM_SPLIT.split(text or "")
    if len(parts) < 2:
        return []
    out: list[tuple[str, str]] = []
    for raw in parts[1].splitlines():
        ln = raw.strip().lstrip("-·•* ").strip()  # LLM 偶尔加列表符号
        if not ln:
            continue
        for sep in _TERM_SEPS:
            word, _, meaning = ln.partition(sep)
            if meaning.strip():
                out.append((word.strip()[:500], meaning.strip()[:400]))
                break
        else:
            out.append((ln[:500], ""))  # 无分隔符：整行当术语
    return out


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
        self._p = palette("dark")   # 当前主题令牌，_apply_style 刷新；方法内统一用它不再各自查
        self._fs = 14               # 当前正文字号，同上
        self._speaking_btn: QPushButton | None = None
        self._pending_speak_btn: QPushButton | None = None
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.hide)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._set_status(""))
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(400)
        self._loading_timer.timeout.connect(self._tick_loading)
        self._loading_dots = 0
        self._drag_pos = None
        self._dragged = False  # 用户手动拖过弹窗后，内容重排只改尺寸不再挪位置
        self._anchor = None  # 本次展示的锚点（取词瞬间的鼠标位置），重排围绕它而非实时鼠标
        self._grow_timer = QTimer(self)
        self._grow_timer.setSingleShot(True)
        self._grow_timer.setInterval(GROW_THROTTLE_MS)
        self._grow_timer.timeout.connect(self._fit_height)
        self._terms: list[tuple[str, str]] = []  # 当前译文的术语表，☆ 链接收藏用

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
        head.addWidget(self.btn_pin, 0, Qt.AlignTop | Qt.AlignRight)
        root.addLayout(head)

        self.result_view = QTextBrowser()
        self.result_view.setOpenExternalLinks(False)
        self.result_view.setOpenLinks(False)  # 术语 ☆ 链接只发 anchorClicked，不做导航
        self.result_view.anchorClicked.connect(self._on_anchor)
        self.result_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.result_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result_view.setFrameShape(QTextBrowser.NoFrame)
        root.addWidget(self.result_view, 1)

        self.status_label = QLabel()
        self.status_label.setVisible(False)  # 空状态不占行高，窗口高度贴合内容
        root.addWidget(self.status_label)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.btn_speak_source = QPushButton("读原文")
        self.btn_speak_trans = QPushButton("读译文")
        self.btn_star = QPushButton("收藏")
        self.btn_copy = QPushButton("复制")
        self.btn_copy.setObjectName("primary")  # 复制是最高频动作，给主按钮视觉
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
        self._p = palette(cfg.get("theme", "dark"))
        self._fs = int(cfg.get("font_size", 14))
        self._target_opacity = float(cfg.get("opacity", 0.96))
        p, fs = self._p, self._fs
        fs_small, _ = _derived_fs(fs)
        self.setWindowOpacity(self._target_opacity)
        self.setMinimumWidth(int(cfg.get("width", 480)))
        self.setMaximumWidth(int(cfg.get("width", 480)) + 160)
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
                font-size: {fs_small}px;
                color: {p['text_dim']};
            }}
            QLabel {{ background: transparent; border: none; }}
            QTextBrowser {{
                background: transparent; border: none;
                font-size: {fs}px; color: {p['text']};
                selection-background-color: {p['accent']};
            }}
            /* 按钮只声明与全局 theme.py 的布局差异（弹窗空间敏感，padding 更紧凑、
               字号随用户设置）；颜色/hover/checked/disabled/primary 规则统一走全局，
               此前两处各写一套已经分叉过一次（padding 4/12 vs 6/16）。 */
            QPushButton {{
                color: {p['text_dim']};
                padding: 4px 12px;
                font-size: {fs_small}px;
            }}
            QPushButton#primary {{ font-weight: 600; }}
        """)
        # 主题/字号变化后按当前内容重排高度；空窗口回到基线（各展示路径会自行重设）
        if self.result_view.toPlainText().strip():
            self._fit_height()
        else:
            self.result_view.setFixedHeight(60)

    # ---------------------------------------------------------------- 生命周期

    def _reset_for_show(self) -> None:
        """三种展示入口的公共复位：作废回调、解除拖拽冻结、记录本次锚点。

        锚点固定后，译文流式/完成时的重排都围绕取词瞬间的鼠标位置，
        不再跟踪实时鼠标——避免用户移向按钮时弹窗跳位。"""
        self._task_id = -1
        self._dragged = False
        self._anchor = QCursor.pos()

    def show_translation(self, source: str, method: str = "", force: bool = False) -> None:
        """开始一次新的翻译展示。force=True 绕过缓存强制重译（重试入口）。"""
        cfg = self._cfg_getter()
        p = self._p
        self._source = source
        self._translated = ""
        self._terms = []
        self._reset_for_show()
        self._placeholder_active = True  # loading 占位在正文区，首块 chunk 需替换而非追加
        preview = source[:SOURCE_PREVIEW_LIMIT] + ("…" if len(source) > SOURCE_PREVIEW_LIMIT else "")
        via = " · 取词：UIA" if method == "uia" else ""
        self.source_label.setText(f"原文{via}\n{html.escape(preview)}")
        self.source_label.setVisible(True)
        # 上一次译文残留的 fixed 高度先复位，loading 态窗口收敛到一行占位
        self.result_view.setFixedHeight(60)
        # loading 占位放正文区（视线落点），首个 chunk 整体替换
        self.result_view.setHtml(f"<div style='color:{p['text_dim']};'>翻译中…</div>")
        self._loading_dots = 0
        self._loading_timer.start()
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)
        # 译文未出：读译文/复制拿到空文本，收藏会写入无译文生词——译文到位后再启用
        for b in (self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setEnabled(False)

        self._place_near_anchor()
        self.show()
        self.raise_()
        logger.info("popup shown: winId=%s visible=%s", self.winId(), self.isVisible())
        self.activateWindow()
        self._start_auto_close(cfg)

        self._task_id = self._translator.translate(source, use_cache=not force)
        return self._task_id

    def show_result(self, source: str, translated: str) -> None:
        """历史/生词本回看：直接展示已有译文，不发翻译请求（重试可重新发起）。"""
        cfg = self._cfg_getter()
        p = self._p
        self._source = source
        self._translated = translated
        self._reset_for_show()
        self._placeholder_active = False
        preview = source[:SOURCE_PREVIEW_LIMIT] + ("…" if len(source) > SOURCE_PREVIEW_LIMIT else "")
        self.source_label.setText(f"原文\n{html.escape(preview)}")
        self.source_label.setVisible(True)
        self.result_view.setFixedHeight(60)  # 清掉上次残留的 fixed 高度，再由 _fit_height 按内容定
        self._terms = _parse_terms(translated or "")
        self.result_view.setHtml(_format_result(translated or "（无译文）", p, self._terms, fs=self._fs))
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)
            b.setEnabled(True)

        self._fit_height()
        self.show()
        self.raise_()
        self.activateWindow()
        self._start_auto_close(cfg)

    def show_message(self, message: str, error: bool = True) -> None:
        """不发起翻译，仅弹出一条提示（如取词/翻译失败）。"""
        p = self._p
        self._reset_for_show()  # 含 _dragged 复位：错误提示也要弹回鼠标旁，而非上次拖放的旧位置
        self._source = ""
        self._translated = ""
        self._terms = []
        self._placeholder_active = False
        self.source_label.setText("")
        self.source_label.setVisible(False)  # 空文本时 padding+底色仍会渲染，整块隐藏
        self._loading_timer.stop()
        self.result_view.setFixedHeight(60)  # 同 show_translation：清掉上次残留的 fixed 高度
        # 错误是此刻唯一重要的信息：进正文区、可选中复制；无关按钮隐藏
        self.result_view.setHtml(f"<div style='color:{p['error']};'>{html.escape(message)}</div>")
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setVisible(False)
        self.btn_retry.setVisible(True)

        self._fit_height()
        self.show()
        logger.info("popup showing message: %s", message[:60])
        self.raise_()
        self.activateWindow()

    def _avail_geometry(self):
        """锚点所在屏的可用区域（已扣任务栏）；锚点屏失效时回退主屏。"""
        screen = QGuiApplication.screenAt(self._anchor) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _place_near_anchor(self) -> None:
        avail = self._avail_geometry()
        self.adjustSize()
        # 高度跟 sizeHint 走（译文区已按内容 fixed），不再设地板值——短内容小窗口
        w, h = self.width(), min(self.sizeHint().height(), int(avail.height() * 0.6))
        self.resize(w, h)
        x, y = _compute_placement(self._anchor.x(), self._anchor.y(), w, h, avail)
        self.move(x, y)

    def _fit_height(self) -> None:
        """译文区高度贴合内容（带上限），窗口随内容伸缩，长译文内部滚动。"""
        doc = self.result_view.document()
        doc.setTextWidth(self.result_view.viewport().width())  # 同步触发重新排版
        text_h = int(doc.size().height()) + 8
        max_grow = max(int(self._avail_geometry().height() * RESULT_GROW_RATIO), 120)
        # fixed 而非 minimum：sizeHint 不再被 QTextBrowser 默认值撑大，窗口才收得回去
        self.result_view.setFixedHeight(min(max(text_h, 60), max_grow))
        if self._dragged:
            self.adjustSize()  # 只按新尺寸重算窗口，左上角留在用户拖放的位置
        else:
            self._place_near_anchor()

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
            self._loading_timer.stop()
            self.result_view.setPlainText(piece)  # 替换 loading 占位
            self.result_view.moveCursor(QTextCursor.MoveOperation.End)  # setPlainText 会把光标重置到开头
            self._placeholder_active = False
        else:
            self.result_view.insertPlainText(piece)
        if at_bottom:
            sb.setValue(sb.maximum())
        # 流式期间窗口跟随长高，但按节流间隔收着长——不逐 chunk 重排抖动
        if not self._grow_timer.isActive():
            self._grow_timer.start()

    def on_done(self, text: str, task_id: int) -> None:
        if task_id != self._task_id:
            return
        self._loading_timer.stop()
        self._grow_timer.stop()  # 完成态直接重排，作废可能还挂着的节流重排
        self._translated = text
        self._terms = _parse_terms(text)
        p = self._p
        self.result_view.setHtml(_format_result(text, p, self._terms, fs=self._fs))
        # 译文到位，恢复 loading 期间禁用的按钮
        for b in (self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setEnabled(True)
        self._fit_height()
        self._set_status("")

    def on_error(self, message: str, task_id: int) -> None:
        if task_id != self._task_id:
            return
        self.show_message(f"翻译失败：{message}")

    def on_fallback_started(self, task_id: int) -> None:
        """主服务失败、备用服务接管：复位占位机制（首个备用 chunk 整体替换
        主服务可能已输出的半截译文），并提示用户正在切换。"""
        if task_id != self._task_id:
            return
        self._placeholder_active = True
        self._flash_status("主服务失败，切换备用服务重试…")

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

    def _on_anchor(self, url) -> None:
        """术语行 ☆ 链接：单条收藏 (word=术语, note=含义, context=所在原文)。"""
        s = url.toString()
        if not s.startswith("term:"):
            return
        try:
            word, meaning = self._terms[int(s[5:])]
        except (ValueError, IndexError):
            return
        try:
            database.upsert_word(word, note=meaning, context=self._source.strip()[:200])
            self._flash_status(f"已收藏术语：{word[:30]}")
        except Exception as e:
            self._set_status(f"收藏失败：{e}", error=True)

    def _copy(self) -> None:
        text = self._translated or self._source
        QApplication.clipboard().setText(text)
        self._flash_status("已复制" + ("译文" if self._translated else "原文"))

    def _retry(self) -> None:
        if self._source:
            self.show_translation(self._source, force=True)  # 重试强制重译，绕过缓存

    def _on_pin_toggled(self, on: bool) -> None:
        self._pinned = on
        self.btn_pin.setText("已钉住" if on else "钉住")

    # ---------------------------------------------------------------- 杂项

    def _tick_loading(self) -> None:
        """loading 占位的三点跳动动画；首个 chunk 到达（占位被替换）后自停。"""
        if not self._placeholder_active:
            self._loading_timer.stop()
            return
        self._loading_dots = (self._loading_dots + 1) % 4
        p = self._p
        self.result_view.setHtml(
            f"<div style='color:{p['text_dim']};'>翻译中{'.' * self._loading_dots}</div>")

    def _set_status(self, text: str, error: bool = False) -> None:
        p = self._p
        fs_small, _ = _derived_fs(self._fs)
        color = p["error"] if error else (p["accent"] if text else p["text_dim"])
        self.status_label.setText(html.escape(text))
        self.status_label.setVisible(bool(text))  # 空文本不占行高
        self.status_label.setStyleSheet(f"color: {color}; font-size: {fs_small}px;")

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

    # 无边框窗口的拖拽移动：按住窗口空白处（非文本/按钮区域）拖动。
    # 子控件会吃掉自己区域内的鼠标事件，不会干扰文本选择与按钮点击。
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self._dragged = True
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        if self._dragged:
            # 拖出屏外时拉回：按窗口中心找屏，夹进该屏可用区
            geo = self.frameGeometry()
            screen = (QGuiApplication.screenAt(geo.center())
                      or QGuiApplication.primaryScreen())
            x, y = _clamp_into(geo.left(), geo.top(),
                               geo.width(), geo.height(), screen.availableGeometry())
            if (x, y) != (geo.left(), geo.top()):
                self.move(x, y)
        super().mouseReleaseEvent(event)

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
        self._loading_timer.stop()
        self._grow_timer.stop()
        self._tts.stop()
        super().hideEvent(event)


def _compute_placement(ax: int, ay: int, w: int, h: int, avail) -> tuple[int, int]:
    """锚点右下优先放置，右/底出屏翻转到锚点左侧/上方。纯几何，avail 为 QRect。"""
    x, y = ax + CURSOR_OFFSET, ay + CURSOR_OFFSET
    if x + w > avail.right():
        x = ax - w - CURSOR_OFFSET
    if y + h > avail.bottom():
        y = max(avail.top(), ay - h - CURSOR_OFFSET)
    return x, y


def _clamp_into(ax: int, ay: int, w: int, h: int, avail) -> tuple[int, int]:
    """把 (ax, ay) 左上角、w×h 的矩形夹回 avail 内；矩形本身超出时贴左上。纯几何。"""
    x = max(avail.left(), min(ax, avail.right() - w))
    y = max(avail.top(), min(ay, avail.bottom() - h))
    return x, y


def _format_result(text: str, p: dict, terms: list[tuple[str, str]] | None = None,
                   fs: int = 14) -> str:
    """译文完成后渲染：'【术语】' 段落做轻微强调，每行行首 ☆ 链接可单条收藏。

    fs 为用户正文字号，术语标题/正文从它推导——此前写死 12/13px，
    用户调大字号时术语区不跟随，同一屏两种尺度。"""
    accent, border, body_color = p["accent"], p["border"], p["text"]
    fs_small, fs_term = _derived_fs(fs)
    parts = _TERM_SPLIT.split(text)
    body = html.escape(parts[0].strip())
    if len(parts) > 1:
        if terms is None:
            terms = _parse_terms(text)
        rows = []
        for i, (word, meaning) in enumerate(terms):
            line = html.escape(f"{word} — {meaning}" if meaning else word)
            rows.append(
                f"<a href='term:{i}' style='color:{accent};text-decoration:none;'>☆</a> {line}"
            )
        terms_html = "<br/>".join(rows)
        body += (
            f"<div style='margin-top:10px;padding-top:8px;border-top:1px solid {border};'>"
            f"<span style='color:{accent};font-weight:600;font-size:{fs_small}px;'>术语</span>"
            f"<span style='color:{accent};font-size:{fs_small}px;'> · 点 ☆ 收藏</span>"
            f"<div style='white-space:pre-wrap;font-family:inherit;margin:4px 0 0;"
            f"color:{body_color};font-size:{fs_term}px;line-height:1.7;'>{terms_html}</div></div>"
        )
    return f"<div style='line-height:1.55;'>{body}</div>"
