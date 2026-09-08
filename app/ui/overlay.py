"""屏幕截图遮罩：全屏压暗 + 拖拽框选 + Esc 取消。

每屏一个全屏无边框遮罩窗口（跨屏拖拽不合并，选区钳制在本屏内），
松开后从选区所在屏 QScreen.grabWindow(0)（物理像素）裁剪，长边超限等比
缩小后以 PNG bytes 发 selected 信号。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

logger = logging.getLogger("ctrltrans.overlay")

MIN_SELECT_PX = 10   # 选区最小边长（逻辑像素），小于此值视为误触取消
MAX_SIDE_PX = 1600   # 送识别的图片长边上限（OCR 足够，控制上传体积）

DIM_COLOR = QColor(0, 0, 0, 77)        # 30% 黑遮罩
ACCENT_COLOR = QColor(80, 170, 255)    # 选区亮边框


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


def downscale(image: QImage, max_side: int = MAX_SIDE_PX) -> QImage:
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


def png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


class _ScreenMask(QWidget):
    """单屏遮罩：压暗背景 + 自绘选区（亮边框、选区内不压暗）。"""

    def __init__(self, screen, overlay: "ScreenshotOverlay"):
        super().__init__(None)
        self._overlay = overlay
        self._screen = screen
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
        self._overlay._finish_selection(self._screen, rect)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
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
        p.end()


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
            mask = _ScreenMask(screen, self)
            mask.show()
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

    def _finish_selection(self, screen, local_rect: QRect) -> None:
        if self._done:
            return
        self._done = True
        try:
            image = screen.grabWindow(0)  # 全屏物理像素
            dpr = screen.devicePixelRatio()
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
            mask.close()
            mask.deleteLater()
        self._masks = []
