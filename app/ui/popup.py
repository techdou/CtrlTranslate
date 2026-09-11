"""翻译弹窗：无边框置顶，锚定取词时的鼠标位置（流式重排不追实时鼠标），流式整块渲染，失焦自动关闭。

交互契约：
- show_translation(source) 后由 Translator 信号驱动 on_chunk/on_done/on_error
- Esc 关闭；点击其他应用（本应用整体失活）自动关闭；「钉住」后不自动关
- 按钮：读原文 / 读译文 / 收藏 / 复制 / 重试（关闭由 Esc 与失焦覆盖，不设按钮）
- 朗读中的按钮变为「停止」，再点即停
- 错误态：错误文案进正文区（可选中复制），无关按钮隐藏
动效契约（时长/曲线全部取 theme.MOTION 令牌，禁写裸数字）：
- 出现 = show_animated()：淡入 + 上浮 8px；消失 = close_animated()：淡出后 hide
- 流式长高 = _fit_height() → _animate_height_to()：围绕锚点平滑生长（翻转侧向上长）
- on_done 两段式：纯译文先平滑长高，术语块延迟追加再小幅长高
- loading = 骨架条呼吸（motion.breathe 正弦）；错误 = 窗口微 shake 一次
"""

from __future__ import annotations

import html
import logging
import math

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.core.vocabulary import TERM_SECTION_RE, parse_terms
from app.db import database
from app.ui.motion import animate, breathe
from app.ui.theme import MOTION, RADIUS, SHADOW, palette

logger = logging.getLogger("ctrltrans.popup")

SOURCE_PREVIEW_LIMIT = 120
# 弹窗离锚点的偏移 + SHADOW.margin 一起构成视觉间距（此前无阴影边距时是 24）
CURSOR_OFFSET = 12
RESULT_GROW_RATIO = 0.45  # 译文区高度上限 = 锚点屏可用高度的比例；小屏 120px 兜底
WINDOW_MAX_AVAIL_RATIO = 0.6  # 整窗高度上限 = 锚点屏可用高度的比例
GROW_THROTTLE_MS = 300  # 流式输出期间窗口跟随长高的最小间隔，防逐 chunk 抖动


def _derived_fs(fs: int) -> tuple[int, int]:
    """由用户正文字号推导辅助区字号：(小号, 术语号)。

    状态行/术语标题用小号（fs-2, 下限 12），术语正文略大（fs-1, 下限 13）
    ——术语是读外刊最需要看清的部分，不能比状态行还小。"""
    return max(fs - 2, 12), max(fs - 1, 13)


class TranslatePopup(QWidget):
    ocr_retry_requested = Signal()      # OCR 重试：截图 bytes 在 main 手里，交还重走截图链
    engine_toggle_requested = Signal()  # 切换翻译引擎（API↔网页）：配置写入归 main 单入口

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
        self._auto_close_timer.timeout.connect(self.close_animated)
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
        self._placeholder_active = False  # loading 占位生效中，首块 chunk 需替换而非追加
        self._source_expanded = False  # 原文预览展开全文中
        self._source_prefix = "原文"   # 当前预览的标题行（含取词方式后缀）
        # 动效句柄：动画对象必须被持有（无引用会被 GC 中途停止）
        self._show_anim = None    # 出现：淡入+上浮
        self._close_anim = None   # 消失：淡出
        self._grow_anim = None    # 高度平滑生长
        self._breathe_anim = None  # loading 骨架呼吸
        self._shake_anim = None   # 错误抖动
        self._status_fade = None  # flash 状态淡入/淡出
        # on_done 两段式：纯译文先长高，术语块 dur_grow 后追加
        self._terms_timer = QTimer(self)
        self._terms_timer.setSingleShot(True)
        self._terms_timer.timeout.connect(self._append_terms)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # 透明窗口本体：QSS 圆角真正裁剪窗口形状，四周留 SHADOW.margin 给阴影绘制
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._build_ui()
        self._apply_style()
        QApplication.instance().installEventFilter(self)
        if hasattr(tts, "state_changed"):
            tts.state_changed.connect(self._on_tts_state)

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        # 窗口本体透明，内容全部放进 shell（圆角 + 边框 + 阴影都在 shell 上）
        root = QVBoxLayout(self)
        root.setContentsMargins(SHADOW["margin"], SHADOW["margin"],
                                SHADOW["margin"], SHADOW["margin"])
        root.setSpacing(0)
        self._shell = QWidget(self)
        self._shell.setObjectName("shell")
        shadow = QGraphicsDropShadowEffect(self._shell)
        shadow.setBlurRadius(SHADOW["blur"])
        shadow.setOffset(0, SHADOW["dy"])
        shadow.setColor(QColor(0, 0, 0, SHADOW["alpha"]))
        self._shell.setGraphicsEffect(shadow)
        root.addWidget(self._shell)

        box = QVBoxLayout(self._shell)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        # 只读可滚动的原文预览：超长原文可展开看全文（QLabel 不可滚，120 字后无处看全）
        self.source_label = QTextBrowser()
        self.source_label.setObjectName("sourcePreview")
        self.source_label.setOpenExternalLinks(False)
        self.source_label.setOpenLinks(False)
        self.source_label.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.source_label.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.source_label.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.source_label.setFrameShape(QTextBrowser.NoFrame)
        self.source_label.setReadOnly(True)
        head.addWidget(self.source_label, 1)

        self.btn_expand = QPushButton("全文")
        self.btn_expand.setVisible(False)  # 仅原文超预览上限时出现
        self.btn_expand.clicked.connect(self._toggle_source_expand)
        head.addWidget(self.btn_expand, 0, Qt.AlignTop)

        self.btn_pin = QPushButton("钉住")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("钉住后弹窗不再因点击其他程序而关闭")
        self.btn_pin.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.btn_pin.setMinimumWidth(64)  # 容纳「已钉住」三字；Fixed 策略防隐藏原文后吃满整行
        self.btn_pin.toggled.connect(self._on_pin_toggled)
        head.addWidget(self.btn_pin, 0, Qt.AlignTop | Qt.AlignRight)
        box.addLayout(head)

        self.result_view = QTextBrowser()
        self.result_view.setOpenExternalLinks(False)
        self.result_view.setOpenLinks(False)  # 术语 ☆ 链接只发 anchorClicked，不做导航
        self.result_view.anchorClicked.connect(self._on_anchor)
        self.result_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.result_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result_view.setFrameShape(QTextBrowser.NoFrame)
        box.addWidget(self.result_view, 1)

        # loading 骨架：与 result_view 同位互斥显示（首 token 前的"正在成形"感）
        self._loading_widget = QWidget()
        self._loading_widget.setVisible(False)
        sk = QVBoxLayout(self._loading_widget)
        sk.setContentsMargins(4, 10, 4, 2)
        sk.setSpacing(10)
        self._skeleton_rows: list[tuple[QLabel, float]] = []
        for ratio in (0.92, 0.78, 0.55):  # 宽度递减，模拟译文段落的形态
            bar = QLabel()
            bar.setObjectName("skBar")
            bar.setFixedHeight(12)
            sk.addWidget(bar)
            self._skeleton_rows.append((bar, ratio))
        self._loading_hint = QLabel("翻译中")
        self._loading_hint.setObjectName("skHint")
        sk.addWidget(self._loading_hint)
        box.addWidget(self._loading_widget, 1)

        self.status_label = QLabel()
        self.status_label.setVisible(False)  # 空状态不占行高，窗口高度贴合内容
        self._status_effect = QGraphicsOpacityEffect(self.status_label)
        self._status_effect.setOpacity(1.0)
        self.status_label.setGraphicsEffect(self._status_effect)
        box.addWidget(self.status_label)

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
        # 引擎切换（右端，与左侧"本条译文操作"分离）：显示当前引擎，点击切换，
        # 下次划词生效。轻量版（无 WebEngine 组件）隐藏
        self.btn_engine = QPushButton()
        self.btn_engine.setObjectName("engineToggle")
        self.btn_engine.clicked.connect(self.engine_toggle_requested.emit)
        # find_spec 只查存在性不加载 DLL——import 本体在部分环境（CI offscreen）
        # 会因 Chromium 卸载崩溃把进程退出码改写成非零，测试全绿也判失败
        self._webengine_available = True
        try:
            import importlib.util

            import PySide6  # noqa: F401（父包，find_spec 解析子模块的前提）

            if importlib.util.find_spec("PySide6.QtWebEngineCore") is None:
                raise ImportError
        except ImportError:
            self._webengine_available = False
            self.btn_engine.hide()
        btns.addWidget(self.btn_engine)
        box.addLayout(btns)
        self.refresh_engine_button()

        self.btn_speak_source.clicked.connect(
            lambda: self._toggle_speak("en", self.btn_speak_source))
        self.btn_speak_trans.clicked.connect(
            lambda: self._toggle_speak("zh", self.btn_speak_trans))
        self.btn_star.clicked.connect(self._star)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_retry.clicked.connect(self._retry)

    def refresh_engine_button(self) -> None:
        """按当前配置刷新引擎钮文案（main 切换配置后回调 + 每次弹窗展示时）。"""
        if not self._webengine_available:
            return
        webai_on = bool(self._cfg_getter().get("webai", {}).get("enabled"))
        self.btn_engine.setText("网页" if webai_on else "API")
        self.btn_engine.setToolTip(
            f"翻译引擎：{'网页版（免费额度）' if webai_on else 'API 模式'}——点击切换，下次划词生效")

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
            TranslatePopup {{ background: transparent; }}
            #shell {{
                background: {p['bg']};
                border: 1px solid {p['border']};
                border-radius: {RADIUS['md']}px;
            }}
            QLabel#sourcePreview {{
                background: {p['source_bg']};
                border-radius: {RADIUS['sm']}px;
                padding: 8px;
                font-size: {fs_small}px;
                color: {p['text_dim']};
            }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#skBar {{
                background: {p['border']};
                border-radius: {RADIUS['xs']}px;
            }}
            QLabel#skHint {{ color: {p['text_dim']}; }}
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

    def show_animated(self) -> None:
        """三条展示路径的统一入口：淡入 + 从下方 8px 上浮。

        用 setWindowOpacity 而非 QGraphicsOpacityEffect——后者对带
        StaysOnTop 的顶层窗口在 Windows 上有渲染回退风险。"""
        if self._close_anim is not None:
            self._close_anim.stop()
            self._close_anim = None
        if self._show_anim is not None:
            self._show_anim.stop()
        base_x, base_y = self.x(), self.y()
        slide = MOTION["slide_in"]
        target = self._target_opacity

        def _apply(v: float) -> None:
            self.setWindowOpacity(v * target)
            self.move(base_x, base_y + round(slide * (1 - v)))

        self.setWindowOpacity(0.0)
        self.move(base_x, base_y + slide)
        self.show()
        self.raise_()
        self.activateWindow()
        self._show_anim = animate(
            _apply, 0.0, 1.0, MOTION["dur_in"], MOTION["ease_std"],
            done=lambda: self.setWindowOpacity(target))

    def close_animated(self) -> None:
        """全部关闭路径的统一出口：淡出后才 hide()。

        动画期间被再次 show_animated 会 stop 本动画；连续触发则从当前
        透明度续淡。stop() 不触发 finished，done=hide 不会误执行。"""
        if not self.isVisible():
            return
        if self._show_anim is not None:
            self._show_anim.stop()
            self._show_anim = None
        if self._close_anim is not None:
            self._close_anim.stop()
        start = self.windowOpacity()
        if start <= 0.01:  # 已近乎不可见（如被中断在半途），直接收
            self.hide()
            return
        self._close_anim = animate(
            self.setWindowOpacity, start, 0.0, MOTION["dur_out"], "OutQuad",
            done=self.hide)

    def _shake(self) -> None:
        """错误反馈：窗口水平微抖一次（衰减正弦），只用于错误态。"""
        if not self.isVisible():
            return
        if self._shake_anim is not None:
            self._shake_anim.stop()
        base_x, base_y = self.x(), self.y()
        px = MOTION["shake_px"]

        def _apply(v: float) -> None:
            # sin(v·3π)·(1-v)：先右后左各一次，幅度衰减到 0
            self.move(base_x + round(px * math.sin(v * 3 * math.pi) * (1 - v)), base_y)

        self._shake_anim = animate(_apply, 0.0, 1.0, MOTION["shake_ms"], "Linear")

    def _show_skeleton(self) -> None:
        """loading 骨架：宽度递减的圆角灰条 + 呼吸（正弦透明度循环）。"""
        inner = self.width() - 2 * (SHADOW["margin"] + 14) - 8
        for bar, ratio in self._skeleton_rows:
            bar.setFixedWidth(max(120, int(inner * ratio)))
        self.result_view.setVisible(False)
        self._loading_widget.setVisible(True)
        if self._breathe_anim is not None:  # 上一轮 loading 的呼吸先停（其闭包指向将被替换的 effect）
            self._breathe_anim.stop()
            self._breathe_anim = None
        self._loading_widget.setGraphicsEffect(QGraphicsOpacityEffect(self._loading_widget))
        self._loading_widget.graphicsEffect().setOpacity(0.55)
        self._breathe_anim = breathe(
            self._loading_widget.graphicsEffect().setOpacity,
            MOTION["breathe_ms"], 0.55, 0.95)

    def _hide_skeleton(self) -> None:
        if self._breathe_anim is not None:
            self._breathe_anim.stop()
            self._breathe_anim = None
        self._loading_widget.setVisible(False)
        self._loading_widget.setGraphicsEffect(None)
        self.result_view.setVisible(True)

    def _set_source_preview(self, source: str, prefix: str) -> None:
        """折叠态原文预览：超上限截断并显示「全文」按钮。"""
        self._source_expanded = False
        self._source_prefix = prefix
        truncated = len(source) > SOURCE_PREVIEW_LIMIT
        self.btn_expand.setText("全文")
        self.btn_expand.setVisible(truncated)
        text = source[:SOURCE_PREVIEW_LIMIT] + ("…" if truncated else "")
        self.source_label.setPlainText(f"{prefix}\n{text}")
        self.source_label.setMaximumHeight(64)

    def _toggle_source_expand(self) -> None:
        """「全文/收起」：展开后内部滚动，窗口随内容重排。"""
        self._source_expanded = not self._source_expanded
        if self._source_expanded:
            self.btn_expand.setText("收起")
            self.source_label.setPlainText(f"{self._source_prefix}\n{self._source}")
            self.source_label.setMaximumHeight(160)
        else:
            self._set_source_preview(self._source, self._source_prefix)
        self.adjustSize()
        self._fit_height()

    def _reset_for_show(self) -> None:
        """三种展示入口的公共复位：作废回调、解除拖拽冻结、记录本次锚点。

        锚点固定后，译文流式/完成时的重排都围绕取词瞬间的鼠标位置，
        不再跟踪实时鼠标——避免用户移向按钮时弹窗跳位。"""
        self._task_id = -1
        self._dragged = False
        self._anchor = QCursor.pos()
        for attr in ("_grow_anim", "_shake_anim"):  # 在途位移动画清场，防与新定位互抢
            anim = getattr(self, attr)
            if anim is not None:
                anim.stop()
                setattr(self, attr, None)

    def show_translation(self, source: str, method: str = "", force: bool = False,
                         engine=None, request: bool = True, payload: str | None = None,
                         raw: bool = False) -> int:
        """开始一次新的翻译展示。force=True 绕过缓存强制重译（重试入口）。
        engine=None 用默认 API 翻译器；传 WebAIEngine 则由网页引擎承接。
        request=False 只展示不发请求（OCR：图片任务由调用方发起后 adopt_task 挂回）。
        payload=实际发给引擎的内容（默认 source）——网页模式 source=原文仅预览，
        payload=带翻译指令的完整 prompt，重试时重发 payload 而非 source。
        raw=True：payload 是完整指令（术语解释模板），API 引擎不套翻译 system。"""
        cfg = self._cfg_getter()
        self._source = source
        self._translated = ""
        self._terms = []
        self._terms_timer.stop()
        self._reset_for_show()
        self._placeholder_active = True  # loading 占位在正文区，首块 chunk 需替换而非追加
        via = " · 取词：UIA" if method == "uia" else ""
        self._set_source_preview(source, f"原文{via}")
        self.source_label.setVisible(True)
        # 上一次译文残留的 fixed 高度先复位，loading 态窗口收敛到骨架高度
        self.result_view.setFixedHeight(60)
        # loading 骨架占正文位（视线落点），首个 chunk 整体替换
        self._loading_dots = 0
        self._loading_hint.setText("翻译中")
        self._loading_timer.start()
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)
        # 译文未出：读译文/复制拿到空文本，收藏会写入无译文生词——译文到位后再启用
        for b in (self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setEnabled(False)

        self.refresh_engine_button()  # 弹窗每次出现读最新引擎配置
        self._place_near_anchor()
        self._show_skeleton()  # 定位后再算骨架条宽（width 已定准）
        self.show_animated()
        logger.info("popup shown: winId=%s visible=%s", self.winId(), self.isVisible())
        self._start_auto_close(cfg)

        self._engine = engine    # 重试走同一引擎，网页模式重试不漂移回 API
        self._method = method    # OCR 重试分流依据
        self._payload = payload  # 重试时重发的内容（网页模式=完整 prompt）
        self._raw = raw          # 术语解释等 raw 指令态：重试沿用
        if request:
            tid = (engine or self._translator).translate(
                payload if payload is not None else source,
                use_cache=not force, raw=raw)
            if tid == -1:
                # 引擎忙被拒（调用方应预检，此处兜底防骨架屏永转）
                self._task_id = -1
                self.show_message("引擎正忙，请稍候重试")
                return -1
            self._task_id = tid
        else:
            self._task_id = -1  # 待 adopt_task 挂回真实任务
        return self._task_id

    def adopt_task(self, task_id: int) -> None:
        """外部发起的任务接管本弹窗（OCR：文本任务号与图片任务号对齐）。"""
        self._task_id = task_id

    def show_result(self, source: str, translated: str) -> None:
        """历史/生词本回看：直接展示已有译文，不发翻译请求（重试可重新发起）。"""
        cfg = self._cfg_getter()
        self._source = source
        self._translated = translated
        self._terms_timer.stop()
        self._reset_for_show()
        self._placeholder_active = False
        # 历史条目的翻译引擎/方式未知：清空，重试回退默认 API 文本行为
        self._engine = None
        self._method = ""
        self._payload = None
        self._raw = False
        self._set_source_preview(source, "原文")
        self.source_label.setVisible(True)
        self._hide_skeleton()
        self.result_view.setFixedHeight(60)  # 清掉上次残留的 fixed 高度，再由 _fit_height 按内容定
        self._terms = parse_terms(translated or "")
        self.result_view.setHtml(_format_result(translated or "（无译文）", self._p, self._terms, fs=self._fs))
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star,
                  self.btn_copy, self.btn_retry):
            b.setVisible(True)
            b.setEnabled(True)

        self._fit_height()
        self.show_animated()
        self._start_auto_close(cfg)

    def show_message(self, message: str, error: bool = True) -> None:
        """不发起翻译，仅弹出一条提示（如取词/翻译失败）。错误态带一次微抖。"""
        p = self._p
        self._reset_for_show()  # 含 _dragged 复位：错误提示也要弹回鼠标旁，而非上次拖放的旧位置
        self._source = ""
        self._translated = ""
        self._terms = []
        self._terms_timer.stop()
        self._placeholder_active = False
        self.source_label.setPlainText("")
        self.source_label.setVisible(False)  # 空文本时 padding+底色仍会渲染，整块隐藏
        self.btn_expand.setVisible(False)
        self._loading_timer.stop()
        self._hide_skeleton()
        self.result_view.setFixedHeight(60)  # 同 show_translation：清掉上次残留的 fixed 高度
        # 错误是此刻唯一重要的信息：进正文区、可选中复制；无关按钮隐藏
        self.result_view.setHtml(f"<div style='color:{p['error']};'>{html.escape(message)}</div>")
        self._set_status("")
        for b in (self.btn_speak_source, self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setVisible(False)
        self.btn_retry.setVisible(True)

        self._fit_height()
        self.show_animated()
        logger.info("popup showing message: %s", message[:60])
        if error:  # 抖动等出现动画播完再开始，避免两个位移动画打架
            QTimer.singleShot(MOTION["dur_in"], self._shake)

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
        """译文区高度贴合内容（带上限），窗口平滑长高，长译文内部滚动。

        每帧重算 placement：正常侧往下长高（y 不动），翻转到锚点上方时
        顶边随高度增长上移——"围绕锚点长高"而不是只改右下角。"""
        doc = self.result_view.document()
        doc.setTextWidth(self.result_view.viewport().width())  # 同步触发重新排版
        text_h = int(doc.size().height()) + 8
        max_grow = max(int(self._avail_geometry().height() * RESULT_GROW_RATIO), 120)
        # chrome = 窗口高度 − 译文区高度（head/按钮/边距/阴影边距之和，布局常量）。
        # 必须在 setFixedHeight 之前用旧布局值算：setFixedHeight 后 height() 已同步
        # 变化，再算就成 target = 当前高度（自相抵消，动画永不触发——曾踩过）。
        chrome = self.height() - self.result_view.height()
        content_h = min(max(text_h, 60), max_grow)
        # fixed 而非 minimum：sizeHint 不再被 QTextBrowser 默认值撑大，窗口才收得回去
        self.result_view.setFixedHeight(content_h)
        target = min(content_h + chrome,
                     int(self._avail_geometry().height() * WINDOW_MAX_AVAIL_RATIO))
        self._animate_height_to(target, chrome)

    def _animate_height_to(self, target_h: int, chrome: int) -> None:
        """窗口高度 → target_h 平滑过渡；拖拽态只长高不挪位。

        chrome 由调用方在改 result_view 高度前算好传入（动画每帧用
        h - chrome 反推译文区高度）；每帧手动设置不等布局激活，视觉逐帧
        正确；动画中途来了新目标直接从当前值重启。"""
        if self._grow_anim is not None:
            self._grow_anim.stop()
            self._grow_anim = None
        w = self.width()
        avail = self._avail_geometry() if self._anchor else None
        ax = self._anchor.x() if self._anchor else 0
        ay = self._anchor.y() if self._anchor else 0
        dragged = self._dragged

        def _apply(v: float) -> None:
            h = int(round(v))
            self.resize(w, h)
            self.result_view.setFixedHeight(max(h - chrome, 20))
            if not dragged and avail is not None:
                x, y = _compute_placement(ax, ay, w, h, avail)
                self.move(x, y)

        start = float(self.height())
        if not self.isVisible() or abs(target_h - start) <= 2:
            _apply(float(target_h))  # 首帧/微调不动画，直接就位
            return
        self._grow_anim = animate(
            _apply, start, float(target_h), MOTION["dur_grow"], MOTION["ease_std"])

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
        replaced = self._placeholder_active  # 占位被本块替换时，块尾不再追加（否则首块显示两遍）
        if replaced:
            self._loading_timer.stop()
            self._hide_skeleton()
            self.result_view.setPlainText(piece)  # 替换 loading 占位
            self.result_view.moveCursor(QTextCursor.MoveOperation.End)  # setPlainText 会把光标重置到开头
            self._placeholder_active = False
        sb = self.result_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 8
        if not replaced:
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
        self._hide_skeleton()    # 缓存命中时无 on_chunk，占位可能还挂着
        self._translated = text
        self._terms = parse_terms(text)
        # 两段式：先渲染纯译文平滑长高；有术语时延迟 dur_grow 再追加——
        # 一次"纯文本→富文本+术语块"的视觉大跳拆成两次柔和的小动作
        body = html.escape(TERM_SECTION_RE.split(text)[0].strip())
        self.result_view.setHtml(body)
        for b in (self.btn_speak_trans, self.btn_star, self.btn_copy):
            b.setEnabled(True)
        self._fit_height()
        self._set_status("")
        if self._terms:
            self._terms_timer.start(MOTION["dur_grow"])

    def _append_terms(self) -> None:
        """on_done 第二段：追加术语块（带 ☆ 收藏链接）再小幅长高。"""
        if not self._terms or not self.isVisible():
            return
        self.result_view.setHtml(
            _format_result(self._translated, self._p, self._terms, fs=self._fs))
        self._fit_height()

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
        if not self._source:
            return
        if getattr(self, "_method", "") == "ocr":
            # OCR 的截图 bytes 不在 popup 手里——交还 main 重走截图链
            # （旧实现把"屏幕截图 OCR"五个字当文本翻译，结果荒谬）
            self.ocr_retry_requested.emit()
            return
        # 重试强制重译，绕过缓存；引擎/payload/raw 跟随首次发起时的选择
        self.show_translation(self._source, force=True,
                              engine=getattr(self, "_engine", None),
                              payload=getattr(self, "_payload", None),
                              raw=getattr(self, "_raw", False))

    def _on_pin_toggled(self, on: bool) -> None:
        self._pinned = on
        self.btn_pin.setText("已钉住" if on else "钉住")

    # ---------------------------------------------------------------- 杂项

    def _tick_loading(self) -> None:
        """骨架下方提示文字的三点跳动；首个 chunk 到达（占位被替换）后自停。"""
        if not self._placeholder_active:
            self._loading_timer.stop()
            return
        self._loading_dots = (self._loading_dots + 1) % 4
        self._loading_hint.setText(f"翻译中{'.' * self._loading_dots}")

    def _set_status(self, text: str, error: bool = False) -> None:
        p = self._p
        fs_small, _ = _derived_fs(self._fs)
        color = p["error"] if error else (p["accent"] if text else p["text_dim"])
        was_visible = self.status_label.isVisible()
        self.status_label.setText(html.escape(text))
        if self._status_fade is not None:
            self._status_fade.stop()
            self._status_fade = None
        if text:
            self.status_label.setStyleSheet(f"color: {color}; font-size: {fs_small}px;")
            self.status_label.setVisible(True)  # 空文本不占行高；有文本立刻占位再淡入
            if not was_visible:  # 从无到有：淡入（已可见的内容刷新不重放动画）
                self._status_effect.setOpacity(0.0)
                self._status_fade = animate(
                    self._status_effect.setOpacity, 0.0, 1.0,
                    MOTION["dur_micro"], MOTION["ease_std"])
            else:
                self._status_effect.setOpacity(1.0)  # 可能停在淡出半途，拉回全显
        elif was_visible:  # 从有到无：淡出后再释放行高
            self._status_fade = animate(
                self._status_effect.setOpacity, self._status_effect.opacity(), 0.0,
                MOTION["dur_fade_status"], "OutQuad",
                done=self.status_label.hide)

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
            self.close_animated()
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
            self.close_animated()
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:
        self._auto_close_timer.stop()
        self._status_timer.stop()
        self._loading_timer.stop()
        self._grow_timer.stop()
        self._terms_timer.stop()
        self._tts.stop()
        # 在途动画全部终止（stop 不触发 finished，无误回调），句柄清空
        for attr in ("_show_anim", "_close_anim", "_grow_anim",
                     "_breathe_anim", "_shake_anim", "_status_fade"):
            anim = getattr(self, attr)
            if anim is not None:
                anim.stop()
                setattr(self, attr, None)
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
    parts = TERM_SECTION_RE.split(text)
    body = html.escape(parts[0].strip())
    if len(parts) > 1:
        if terms is None:
            terms = parse_terms(text)
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
