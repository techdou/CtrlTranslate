"""popup 定位几何纯函数单测：锚点放置的边界翻转与拖拽 clamp。

QRect 是 QtCore 值类型，不需要 QApplication 即可构造。
avail 模拟屏可用区：1920×1040（1080p 扣 40px 任务栏）。
"""

from PySide6.QtCore import QRect

from app.ui.popup import CURSOR_OFFSET, _clamp_into, _compute_placement

AVAIL = QRect(0, 0, 1920, 1040)


def test_place_default_lower_right():
    """屏中间锚点：弹窗落在锚点右下方 CURSOR_OFFSET 处，不翻转。"""
    x, y = _compute_placement(500, 500, 480, 300, AVAIL)
    assert (x, y) == (500 + CURSOR_OFFSET, 500 + CURSOR_OFFSET)


def test_place_flips_left_near_right_edge():
    """锚点贴近右缘：右放不下 → 翻到锚点左侧，右边界不越屏。"""
    ax = AVAIL.right() - 100
    x, y = _compute_placement(ax, 500, 480, 300, AVAIL)
    assert x == ax - 480 - CURSOR_OFFSET
    assert x + 480 <= AVAIL.right()
    assert y == 500 + CURSOR_OFFSET  # 纵向不受影响


def test_place_flips_up_near_bottom():
    """锚点贴近底缘：下放不下 → 翻到锚点上方，底边不越屏。"""
    ay = AVAIL.bottom() - 60
    x, y = _compute_placement(500, ay, 480, 300, AVAIL)
    assert y == ay - 300 - CURSOR_OFFSET
    assert y + 300 <= AVAIL.bottom()
    assert x == 500 + CURSOR_OFFSET  # 横向不受影响


def test_place_flips_both_corner():
    """右下角锚点：横向纵向都翻转。"""
    ax, ay = AVAIL.right() - 50, AVAIL.bottom() - 50
    x, y = _compute_placement(ax, ay, 480, 300, AVAIL)
    assert x == ax - 480 - CURSOR_OFFSET
    assert y == ay - 300 - CURSOR_OFFSET


def test_place_top_guard_when_taller_than_screen():
    """弹窗比屏还高（极端小屏/巨内容）：翻上后不小于 avail.top()。"""
    x, y = _compute_placement(500, AVAIL.bottom() - 10, 480, 5000, AVAIL)
    assert y == AVAIL.top()
    assert x == 500 + CURSOR_OFFSET


def test_place_offset_unified():
    """翻转侧与正向偏移统一为同一常量（回归 18/12 双魔法数）。"""
    ax = AVAIL.right() - 10
    x, _ = _compute_placement(ax, 500, 480, 300, AVAIL)
    assert ax - x - 480 == CURSOR_OFFSET


def test_clamp_pulls_back_inside():
    """拖出右下屏外：拉回可用区右下角内侧。"""
    x, y = _clamp_into(1900, 1000, 480, 300, AVAIL)
    assert (x, y) == (AVAIL.right() - 480, AVAIL.bottom() - 300)


def test_clamp_pulls_back_top_left():
    """拖到负坐标（副屏方向）：拉回可用区左上角。"""
    x, y = _clamp_into(-300, -200, 480, 300, AVAIL)
    assert (x, y) == (AVAIL.left(), AVAIL.top())


def test_clamp_keeps_valid_position():
    """屏内合法位置：原样返回。"""
    x, y = _clamp_into(100, 100, 480, 300, AVAIL)
    assert (x, y) == (100, 100)


def test_clamp_oversized_rect_sticks_to_top_left():
    """矩形比可用区还大：贴左上，不出负坐标。"""
    x, y = _clamp_into(1900, 1000, 3000, 2000, AVAIL)
    assert (x, y) == (AVAIL.left(), AVAIL.top())
