"""截图遮罩测试：DPI 换算纯函数、图片缩放/PNG 编码、Esc 取消。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.ui.overlay import downscale, png_bytes, to_physical_rect

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- DPI 换算

def test_to_physical_rect_dpr_1():
    assert to_physical_rect(10, 20, 300, 200, 1.0, 1920, 1080) == (10, 20, 300, 200)


def test_to_physical_rect_dpr_150():
    assert to_physical_rect(10, 20, 300, 200, 1.5, 2880, 1620) == (15, 30, 450, 300)


def test_to_physical_rect_clamps_out_of_bounds():
    # 125% DPR 下 round 后可能越界：右下角贴边选区必须被钳回图内
    px, py, pw, ph = to_physical_rect(3838, 2158, 2, 2, 1.25, 4800, 2700)
    assert 0 <= px and px + pw <= 4800
    assert 0 <= py and py + ph <= 2700
    assert pw >= 1 and ph >= 1


def test_to_physical_rect_tiny_selection_never_empty():
    px, py, pw, ph = to_physical_rect(0, 0, 1, 1, 1.0, 100, 100)
    assert (pw, ph) == (1, 1)


# ---------------------------------------------------------------- 图片处理

def _solid_image(w, h, color="#FF0000"):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def test_downscale_shrinks_only():
    big = downscale(_solid_image(2000, 1000))
    assert (big.width(), big.height()) == (1600, 800)
    small = downscale(_solid_image(800, 400))
    assert (small.width(), small.height()) == (800, 400)  # 只缩不放


def test_png_bytes_roundtrip():
    img = _solid_image(31, 17, "#00FF00")
    data = png_bytes(img)
    back = QImage.fromData(data, "PNG")
    assert not back.isNull()
    assert (back.width(), back.height()) == (31, 17)
    assert back.pixelColor(5, 5) == QColor("#00FF00")


# ---------------------------------------------------------------- 遮罩交互

def test_overlay_esc_cancels():
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtTest import QTest

    from app.ui.overlay import ScreenshotOverlay

    overlay = ScreenshotOverlay()
    got = []
    overlay.cancelled.connect(lambda: got.append(True))
    overlay.show()
    QTest.keyClick(overlay._masks[0], _Qt.Key.Key_Escape)
    assert got == [True]
    assert overlay._masks == []


def test_overlay_tiny_drag_cancels():
    """小于最小边长的框选视为误触 → 取消而不是发空图。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    from app.ui.overlay import MIN_SELECT_PX, ScreenshotOverlay

    overlay = ScreenshotOverlay()
    cancelled = []
    overlay.cancelled.connect(lambda: cancelled.append(True))
    overlay.show()
    mask = overlay._masks[0]
    QTest.mousePress(mask, Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
    QTest.mouseMove(mask, pos=QPoint(55, 55))  # 5px < MIN_SELECT_PX
    QTest.mouseRelease(mask, Qt.MouseButton.LeftButton, pos=QPoint(55, 55))
    assert cancelled == [True]
    assert overlay._masks == []
    _ = MIN_SELECT_PX


# ---------------------------------------------------------------- 预抓快照方案

def test_overlay_selection_crops_pregrabbed_shot():
    """成图必须裁自预抓快照，而不是选完再抓屏。

    回归：旧实现松开鼠标时才 grabWindow——遮罩还盖在屏上，压暗层（甚至
    遮罩底色）被一起拍进图里，OCR 拿到的是脏图。替换快照为纯色测试图后
    框选，返回图应全是该纯色；若实现错误（二次抓屏），抓到的是真实屏幕。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest

    from app.ui.overlay import ScreenshotOverlay

    overlay = ScreenshotOverlay()
    got = []
    overlay.selected.connect(lambda png: got.append(png))
    overlay.show()
    mask = overlay._masks[0]
    shot = QPixmap(400, 300)
    shot.fill(QColor("#FF00FF"))
    mask._shot = shot  # 模拟"触发瞬间的屏幕"是一块品红

    # 强制同步渲染一次 paintEvent：快照铺底 + 压暗叠加必须可渲染
    rendered = mask.grab().toImage()
    assert rendered.pixelColor(5, 5) != QColor("#FF00FF")  # 选区外叠了 30% 压暗
    r = rendered.pixelColor(5, 5).red()
    assert 100 < r < 200  # 比原色 255 明显暗，但不是黑墙（旧 bug：不透明底色墙）

    QTest.mousePress(mask, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(mask, pos=QPoint(200, 200))
    QTest.mouseRelease(mask, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
    assert len(got) == 1

    img = QImage.fromData(got[0], "PNG")
    assert not img.isNull()
    assert img.pixelColor(2, 2) == QColor("#FF00FF")
    assert img.pixelColor(img.width() - 3, img.height() - 3) == QColor("#FF00FF")
    assert overlay._masks == []
