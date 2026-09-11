"""自定义控件：ToggleSwitch 滑动开关、IconButton 纯图标按钮。

动效统一走 motion.animate()，时长/曲线取 theme.MOTION 令牌（禁写裸数字）。
"""

from __future__ import annotations

from PySide6.QtCore import Property, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QPushButton, QSizePolicy

from app.ui.icons import get_icon
from app.ui.motion import animate
from app.ui.theme import MOTION


class ToggleSwitch(QAbstractButton):
    """滑动开关：启用类设置（功能总闸）的现代化形态。

    接口对齐 QCheckBox（setChecked/isChecked/toggled），调用方无感替换。
    纯自绘：accent 色轨道 + 白色滑块，切换时滑块位移动画。
    文案不放控件内——配合右侧 QLabel 使用，保持控件尺寸紧凑。
    """

    def __init__(self, checked: bool = False, *, accent: str, track_off: str,
                 parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        # Fixed：表单布局会拉伸 field 控件，开关必须保住 40x22 的紧凑尺寸
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._accent = QColor(accent)
        self._track_off = QColor(track_off)
        self._pos = 1.0 if checked else 0.0  # 滑块归位比例：0=左(关) 1=右(开)
        self._anim = None
        self.toggled.connect(self._animate_to)

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # 滑块位置暴露为 Qt Property 纯属惯例；动画仍用 animate() 喂 float
    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, v: float) -> None:
        self._pos = v
        self.update()

    pos = Property(float, _get_pos, _set_pos)

    def _animate_to(self, on: bool) -> None:
        if self._anim is not None:
            self._anim.stop()
        self._anim = animate(self._set_pos, self._pos, 1.0 if on else 0.0,
                             MOTION["dur_micro"], MOTION["ease_std"])

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if not self.isEnabled():
            p.setOpacity(0.5)
        # 轨道：开=accent，关=边框灰
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._accent if self.isChecked() else self._track_off)
        p.drawRoundedRect(0, 2, w, h - 4, (h - 4) / 2, (h - 4) / 2)
        # 滑块：白色圆，随 _pos 左右滑动；关态在 accent 轨道上用深一点的心
        d = h - 10
        x = 4 + self._pos * (w - 8 - d)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(int(x), 5, d, d)
        p.end()

    def hitButton(self, pos) -> bool:  # 整个矩形都可点（默认实现按形状，自绘必须覆写）
        return self.rect().contains(pos)


class IconButton(QPushButton):
    """纯图标按钮：图标着色 hover 平滑过渡 + 按压内缩回弹。

    形状（边框/圆角/背景）仍由 QSS 管；图标像素 QSS 碰不到，着色归这里。
    hover 过渡插值只渲染约 10 帧小尺寸 SVG，经 get_icon 缓存后开销可忽略。
    primary 等变体的图标前景色由调用方按 QSS 配色传入（如 accent_text）。
    """

    def __init__(self, icon_name: str, tooltip: str, *, fg: str, hover: str,
                 size: int = 16, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._base_fg = fg   # set_colors 维护的常态色；状态色（停止/实心星）不覆盖它
        self._fg = fg        # 当前生效的前景色（状态图标可能改成 accent）
        self._hover = hover
        self._size = size
        self._color_anim = None
        self._bounce_anim = None
        self.setToolTip(tooltip)
        self.setFixedSize(size + 18, size + 14)  # 图标 + 紧凑留白，视觉比文字按钮轻
        self._apply_icon(fg)

    # ---- 对外 ----

    def set_colors(self, fg: str, hover: str) -> None:
        """主题/字号刷新后重着色（popup._apply_style 统一调）。"""
        self._base_fg, self._hover = fg, hover
        self._fg = fg
        self._apply_icon(fg)

    def set_icon(self, name: str, color: str | None = None) -> None:
        """状态图标切换（朗读→停止、收藏→实心星）；不传色则回到基准色。"""
        self._icon_name = name
        self._fg = color if color is not None else self._base_fg
        self._apply_icon(self._fg)

    def bounce(self, scale_to: float = 1.3) -> None:
        """一次性弹跳反馈（收藏成功）：放大后回弹。"""
        if self._bounce_anim is not None:
            self._bounce_anim.stop()
        base = self._size
        self._bounce_anim = animate(
            lambda v: self.setIconSize(QSize(round(v), round(v))),
            base * scale_to, float(base), MOTION["dur_micro"], "OutBack")

    # ---- 内部 ----

    def _apply_icon(self, color: str) -> None:
        self.setIcon(get_icon(self._icon_name, color, self._size))
        if self.iconSize().isEmpty():
            self.setIconSize(QSize(self._size, self._size))

    def enterEvent(self, event) -> None:
        self._tween_color(self._fg, self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._tween_color(self._hover, self._fg)
        super().leaveEvent(event)

    def _tween_color(self, c_from: str, c_to: str) -> None:
        if self._color_anim is not None:
            self._color_anim.stop()
        a, b = QColor(c_from), QColor(c_to)

        def _mix(v: float) -> None:
            c = QColor.fromRgbF(
                a.redF() + (b.redF() - a.redF()) * v,
                a.greenF() + (b.greenF() - a.greenF()) * v,
                a.blueF() + (b.blueF() - a.blueF()) * v)
            self._apply_icon(c.name())

        self._color_anim = animate(_mix, 0.0, 1.0, MOTION["dur_micro"], MOTION["ease_std"])

    # 按压内缩、松开回弹：只动 iconSize 不动几何，布局不跳
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            s = round(self._size * 0.85)
            self.setIconSize(QSize(s, s))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self._bounce_anim is not None:
                self._bounce_anim.stop()
            base = self._size
            self._bounce_anim = animate(
                lambda v: self.setIconSize(QSize(round(v), round(v))),
                self._size * 0.85, float(base), MOTION["dur_micro"], "OutBack")
