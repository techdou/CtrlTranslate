"""屏幕截图遮罩：预抓全屏快照铺底 + 压暗 + 拖拽框选 + Esc 取消。

热键触发瞬间对每屏 QScreen.grabWindow(0) 抓一张静态快照：遮罩窗口把
快照铺满屏再叠加 30% 压暗——框选时看到的就是将截到的画面（所见即所得，
遮罩窗口本身不需要半透明），松开后直接从快照裁剪，遮罩绝不会混入成图。
每屏一个全屏无边框遮罩窗口（跨屏拖拽不合并，选区钳制在本屏内），
长边超限等比缩小后以 PNG bytes 发 selected 信号。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPixmap, QPen
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.motion import animate
from app.ui.theme import MOTION, palette

logger = logging.getLogger("ctrltrans.overlay")

MIN_SELECT_PX = 10   # 选区最小边长（逻辑像素），小于此值视为误触取消
MAX_SIDE_PX = 1600   # 送识别的图片长边上限（OCR 足够，控制上传体积）
CORNER_ARM_PX = 12   # 选区四角 L 角标的臂长

DIM_COLOR = QColor(0, 0, 0, 77)  # 30% 黑遮罩（功能性中性色，不随主题）
# 选区边框固定用暗场强调色：遮罩是压暗场景，浅色主题的 teal(#0F766E) 在暗底上
# 偏暗看不清，深浅主题统一用 dark palette 的亮 teal(#4DB6AC)——遮罩永远暗场配色
ACCENT_COLOR = QColor(palette("dark")["accent"])


def to_physical_rect(x: int, y: int, w: int, h: int, dpr: float,
                     img_w: int, img_h: int) -> tuple[int, int, int, int]:
    """遮罩窗口内的逻辑像素选区 → 截图的物理像素裁剪区 (px, py, pw, ph)。

    四舍五入后向内收缩并钳制到截图范围内——DPR 非整倍数（125%/150%）时
    round 误差可能把裁剪区顶出边界，越界 QImage.copy 会炸。
    """
    px = round(x * dpr)
    py = round(y * dpr)
    pw = round((x + w) * dpr) - px
    ph = round((y + h) * dpr) - py
    px = max(0, min(px, img_w - 1))
    py = max(0, min(py, img_h - 1))
    pw = max(1, min(pw, img_w - px))
    ph = max(1, min(ph, img_h - py))
    return (px, py, pw, ph)


def downscale(image: QImage | QPixmap, max_side: int = MAX_SIDE_PX) -> QImage | QPixmap:
    """长边超限等比缩小（保持宽高比；只缩不放）。"""
    side = max(image.width(), image.height())
    if side <= max_side:
        return image
    scale = max_side / side
    return image.scaled(
        max(1, round(image.width() * scale)),
        max(1, round(image.height() * scale)),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def png_bytes(image: QImage | QPixmap) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


class _ScreenMask(QWidget):
    """单屏遮罩：预抓快照铺底 + 压暗 + 自绘选区（亮边框、选区内不压暗）。"""

    def __init__(self, screen, shot: QPixmap, overlay: "ScreenshotOverlay"):
        super().__init__(None)
        self._overlay = overlay
        self._screen = screen
        self._shot = shot  # 触发瞬间的全屏快照（物理像素），也是最终裁剪源
        self._origin: QPoint | None = None
        self._selection = QRect()  # 本窗口 local 坐标

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen.geometry())

    # ---- 事件 ----

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._overlay.cancel()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is None:
            return
        # 按下后 Qt 自动 grab 鼠标：拖出本屏也持续收到事件，钳制回窗口范围
        pos = event.position().toPoint()
        pos.setX(max(0, min(pos.x(), self.width() - 1)))
        pos.setY(max(0, min(pos.y(), self.height() - 1)))
        self._selection = QRect(self._origin, pos).normalized()
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        rect = self._selection.normalized()
        self._origin = None
        if rect.width() < MIN_SELECT_PX or rect.height() < MIN_SELECT_PX:
            self._overlay.cancel()  # 误触/单击 → 取消
            return
        self._overlay._finish_selection(self, rect)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._shot)  # 快照铺底：框选时看到的就是将截到的画面
        if self._selection.isNull():
            p.fillRect(self.rect(), DIM_COLOR)
        else:
            # 选区四周压暗、选区内透亮
            s = self._selection
            p.fillRect(QRect(0, 0, self.width(), s.top()), DIM_COLOR)
            p.fillRect(QRect(0, s.bottom() + 1, self.width(), self.height() - s.bottom() - 1), DIM_COLOR)
            p.fillRect(QRect(0, s.top(), s.left(), s.height()), DIM_COLOR)
            p.fillRect(QRect(s.right() + 1, s.top(), self.width() - s.right() - 1, s.height()), DIM_COLOR)
            p.setPen(QPen(ACCENT_COLOR, 2))
            p.drawRect(s.adjusted(0, 0, -1, -1))
            self._draw_corner_arms(p, s)
            self._draw_size_badge(p, s)
        p.end()

    def _draw_corner_arms(self, p: QPainter, s: QRect) -> None:
        """四角 L 形角标：截图工具的通用语言，比细框更醒目地锚定选区边界。"""
        arm = CORNER_ARM_PX
        for cx, cy, dx, dy in (
            (s.left(), s.top(), 1, 1), (s.right(), s.top(), -1, 1),
            (s.left(), s.bottom(), 1, -1), (s.right(), s.bottom(), -1, -1),
        ):
            p.drawLine(cx, cy, cx + dx * arm, cy)
            p.drawLine(cx, cy, cx, cy + dy * arm)

    def _draw_size_badge(self, p: QPainter, s: QRect) -> None:
        """右下角尺寸徽标，物理像素（用户截图认知里的真实分辨率）。"""
        dpr = self._screen.devicePixelRatio()
        text = f"{round(s.width() * dpr)} × {round(s.height() * dpr)}"
        font = p.font()
        font.setPixelSize(13)
        p.setFont(font)
        fm = p.fontMetrics()
        bw = fm.horizontalAdvance(text) + 12
        bh = fm.height() + 6
        bx = max(s.left(), s.right() - bw)  # 窄选区时徽标宽过选区，钳在选区左缘内
        by = s.bottom() + 6
        if by + bh > self.height():  # 选区贴屏底：徽标挪进选区内侧
            by = s.bottom() - bh - 6
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(bx, by, bw, bh, 4, 4)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRect(bx, by, bw, bh), Qt.AlignmentFlag.AlignCenter, text)


class ScreenshotOverlay(QObject):
    """多屏遮罩协调器：show() 盖住所有屏，选完/取消后自动关闭。用完即弃。"""

    selected = Signal(bytes)   # PNG bytes（已缩放）
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._masks: list[_ScreenMask] = []
        self._done = False

    def show(self) -> None:
        screens = QApplication.screens()
        if not screens:  # 理论上不会发生
            self.cancel()
            return
        for screen in screens:
            # 触发瞬间先抓快照：既作遮罩铺底又作最终裁剪源——若等选完再抓，
            # 遮罩自己还在屏上，会把压暗层一起拍进图里
            shot = screen.grabWindow(0)
            if shot.isNull():
                logger.error("screen grab failed: %s", screen.name())
                self.cancel()
                return
            mask = _ScreenMask(screen, shot, self)
            # 快速淡入给"入场感"，80ms 上限不耽误截图手感；句柄挂 mask 防止被 GC 中断
            mask.setWindowOpacity(0.0)
            mask.show()
            mask._fade_in = animate(
                mask.setWindowOpacity, 0.0, 1.0, MOTION["dur_mask_in"], "OutQuad")
            self._masks.append(mask)
        # 键盘焦点给鼠标所在屏的遮罩，Esc 才有人接
        target = QApplication.screenAt(QCursor.pos()) or screens[0]
        for mask in self._masks:
            if mask._screen is target:
                mask.activateWindow()
                break

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._close_all()
        self.cancelled.emit()

    # ---- 内部 ----

    def _finish_selection(self, mask: _ScreenMask, local_rect: QRect) -> None:
        if self._done:
            return
        self._done = True
        try:
            image = mask._shot  # 预抓快照（物理像素），遮罩已铺底展示，不能再抓屏
            dpr = mask._screen.devicePixelRatio()
            px, py, pw, ph = to_physical_rect(
                local_rect.x(), local_rect.y(), local_rect.width(), local_rect.height(),
                dpr, image.width(), image.height(),
            )
            part = downscale(image.copy(px, py, pw, ph))
            self._close_all()
            self.selected.emit(png_bytes(part))
        except Exception:
            logger.exception("screenshot selection failed")
            self._close_all()
            self.cancelled.emit()

    def _close_all(self) -> None:
        for mask in self._masks:
            fade = getattr(mask, "_fade_in", None)  # 淡入 80ms 内取消时先停动画，
            if fade is not None:                    # 否则回调打在已删除的 C++ 对象上刷 RuntimeError
                fade.stop()
            mask.close()
            mask.deleteLater()
        self._masks = []
