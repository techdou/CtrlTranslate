"""主题令牌与动效 helper 单测。

令牌：深浅 palette key 结构一致、动效/阴影/圆角令牌齐全（动画代码按 key 取值，
缺 key 会 AttributeError，这里挡在单测层）。
motion.breathe：正弦映射的连续性（loop 边界无跳变）用数学性质验证，不需要 QApplication。
"""

import math

from app.ui.theme import DARK, LIGHT, MOTION, RADIUS, SHADOW, build_qss

_PALLETE_KEYS = {"name", "bg", "panel", "panel2", "text", "text_dim", "accent",
                 "accent_hover", "accent_text", "border", "error", "source_bg"}


def test_palette_keys_consistent():
    """深浅两套 key 结构必须一致，缺 key 会让 QSS f-string KeyError。"""
    assert set(DARK) == set(LIGHT) == _PALLETE_KEYS


def test_motion_tokens_complete():
    needed = {"dur_in", "dur_out", "dur_grow", "dur_micro", "dur_fade_status",
              "breathe_ms", "ease_std", "slide_in", "shake_px", "shake_ms",
              "dur_mask_in"}
    assert needed <= set(MOTION)
    # 消失要快于出现（拖泥带水的退出最伤手感）
    assert MOTION["dur_out"] < MOTION["dur_in"]


def test_shadow_radius_tokens_complete():
    assert set(SHADOW) == {"blur", "alpha", "dy", "margin"}
    assert set(RADIUS) == {"xs", "sm", "md"}


def test_radius_tokens_used_in_qss():
    """圆角令牌必须真被 QSS 消费（此前定义了没人用，是带测试的死代码）。"""
    qss = build_qss(DARK)
    assert f"border-radius: {RADIUS['sm']}px" in qss


def test_qss_contains_menu_and_disabled_rules():
    """QMenu/QMessageBox 必须在 QSS 内（否则深色主题下托盘菜单是原生白）；
    disabled 必须视觉塌陷（transparent 底），不只是字变灰。"""
    qss = build_qss(DARK)
    assert "QMenu::item:selected" in qss
    assert "QMessageBox" in qss
    for p in (DARK, LIGHT):
        q = build_qss(p)
        assert "QPushButton:disabled" in q
        assert "background: transparent" in q.split("QPushButton:disabled")[1].split("}")[0]


def test_qss_font_order_western_first():
    """西文字形走 Segoe UI（拉丁字符清晰度），中文按字符回退到雅黑。"""
    qss = build_qss(DARK)
    fam = qss.split("font-family:")[1].split(";")[0]
    assert fam.index("Segoe UI") < fam.index("Microsoft YaHei UI")


def test_breathe_sine_mapping_continuous_at_loop_boundary():
    """v=0 与 v=1（loop 边界）映射值相同且都在 low —— 呼吸循环无跳变。

    QVariantAnimation 回调需要事件循环，这里直接验 breathe 用的同款公式
    low + (high-low)*sin(v*pi) 的数学性质（边界值、峰值、单调段）。"""
    low, high = 0.45, 0.8

    def mapping(v: float) -> float:
        return low + (high - low) * math.sin(v * math.pi)

    assert mapping(0.0) == low
    assert abs(mapping(1.0) - low) < 1e-9  # sin(pi) 浮点上非精确 0
    assert abs(mapping(0.5) - high) < 1e-9
    assert mapping(0.25) > mapping(0.1)  # 前半程上升
