"""全局主题：深/浅两套 QSS。克制用色：单一强调色，无渐变，阴影只用于浮层。

颜色令牌（深浅两套）+ 动效/阴影/圆角令牌（主题共享，QSS 不支持 transition，
动效时长与曲线由 Python 侧动画代码消费，统一从 MOTION 取值禁写裸数字）。
"""

from __future__ import annotations

# ---- 动效令牌（深浅主题共享）----
MOTION = {
    "dur_in": 120,          # 窗口/控件出现（ms）
    "dur_out": 90,          # 消失要快于出现，拖泥带水的退出最伤手感
    "dur_grow": 180,        # 流式高度生长
    "dur_micro": 150,       # flash 提示、状态切换等微反馈
    "dur_fade_status": 200, # flash 状态淡出
    "dur_mask_in": 80,      # 截图遮罩淡入上限——慢了耽误截图手感
    "breathe_ms": 1200,     # loading 骨架呼吸周期
    "ease_std": "OutCubic",     # 出现/生长标准曲线
    "slide_in": 8,          # 出现时的上浮位移 px
    "shake_px": 4,          # 错误抖动幅度 px
    "shake_ms": 160,        # 错误抖动时长
}

# ---- 浮层阴影令牌（只用于弹窗/遮罩这类浮层）----
SHADOW = {"blur": 28, "alpha": 70, "dy": 4, "margin": 20}

# ---- 圆角令牌（此前 6/10px 散落各处字面量）：xs=骨架条等微元素，sm=常规控件，md=浮层窗口 ----
RADIUS = {"xs": 4, "sm": 6, "md": 10}

DARK = {
    "name": "dark",
    "bg": "#1C1D22",
    "panel": "#24252C",
    "panel2": "#2B2D35",
    "text": "#E8E6E3",
    "text_dim": "#9A9AA3",
    "accent": "#4DB6AC",
    "accent_hover": "#5BC4BA",
    "accent_text": "#0E2B28",
    "border": "#35373F",
    "error": "#E57373",
    "source_bg": "#202127",
}

LIGHT = {
    "name": "light",
    "bg": "#F6F5F1",
    "panel": "#FFFFFF",
    "panel2": "#EFEEE9",
    "text": "#26272B",
    "text_dim": "#66676F",
    "accent": "#0F766E",
    "accent_hover": "#0D5F59",
    "accent_text": "#FFFFFF",
    "border": "#DFDED8",
    "error": "#C62828",
    "source_bg": "#F1F0EB",
}

THEMES = {"dark": DARK, "light": LIGHT}


def palette(name: str) -> dict:
    return THEMES.get(name, DARK)


def build_qss(p: dict) -> str:
    return f"""
    QWidget {{
        background: {p['bg']};
        color: {p['text']};
        font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    }}
    QMainWindow, QDialog {{
        background: {p['bg']};
    }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        border-radius: {RADIUS['sm']}px;
        padding: 5px 8px;
        selection-background-color: {p['accent']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
        border-color: {p['accent']};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        selection-background-color: {p['panel2']};
    }}
    QPushButton {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        border-radius: {RADIUS['sm']}px;
        padding: 6px 16px;
    }}
    QPushButton:hover {{ border-color: {p['accent']}; color: {p['accent']}; }}
    QPushButton:focus {{ border: 2px solid {p['accent']}; padding: 5px 15px; }}
    QPushButton:pressed {{ background: {p['panel2']}; }}
    QPushButton:disabled {{
        color: {p['text_dim']};
        background: transparent;
        border-color: {p['border']};
    }}
    QPushButton:checked {{ border-color: {p['accent']}; color: {p['accent']}; }}
    QPushButton#danger {{
        color: {p['error']};
        border-color: {p['error']};
    }}
    QPushButton#danger:hover {{
        background: {p['error']};
        color: {p['bg']};
        border-color: {p['error']};
    }}
    QPushButton#danger:focus {{ border: 2px solid {p['error']}; padding: 5px 15px; }}
    QPushButton#primary {{
        background: {p['accent']};
        color: {p['accent_text']};
        border: none;
        font-weight: 600;
    }}
    QPushButton#primary:hover {{ background: {p['accent_hover']}; }}
    QPushButton#primary:focus {{ border: 2px solid {p['accent_text']}; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {p['border']};
        border-radius: 3px;
        background: {p['panel']};
    }}
    QCheckBox::indicator:checked {{
        background: {p['accent']};
        border-color: {p['accent']};
    }}
    QRadioButton {{ spacing: 8px; }}
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {p['border']};
        border-radius: 7px;
        background: {p['panel']};
    }}
    QRadioButton::indicator:checked {{
        border: 3px solid {p['accent']};
        background: {p['panel']};
    }}
    QSlider::groove:horizontal {{
        height: 4px; background: {p['border']}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {p['accent']}; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -5px 0;
        border-radius: 7px; background: {p['accent']};
    }}
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {p['border']}; border-radius: 4px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p['text_dim']}; }}
    QScrollBar:horizontal {{
        background: transparent; height: 8px; margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {p['border']}; border-radius: 4px; min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {p['text_dim']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: {RADIUS['sm']}px; top: -1px; }}
    QTabBar::tab {{
        padding: 7px 18px;
        border: 1px solid {p['border']};
        border-bottom: none;
        border-top-left-radius: {RADIUS['sm']}px; border-top-right-radius: {RADIUS['sm']}px;
        background: {p['panel2']};
        margin-right: 3px;
    }}
    QTabBar::tab:selected {{ background: {p['panel']}; color: {p['accent']}; }}
    QListWidget {{
        background: {p['panel2']};
        border: none;
        border-right: 1px solid {p['border']};
        outline: none;
    }}
    QListWidget::item {{ padding: 10px 14px; }}
    QListWidget::item:selected {{
        background: {p['panel']}; color: {p['accent']};
    }}
    /* 设置页卡片分组：容器 panel 底 + 圆角；卡片内 label 透明，行容器用
       #rowWrap 精确透明（"QFrame#card > QWidget" 会命中 QLineEdit 等输入控件
       ——QSS 类选择器命中子类，且 id 特异性压过无 id 规则，教训记牢） */
    QFrame#card {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        border-radius: {RADIUS['md']}px;
    }}
    QFrame#card QLabel {{ background: transparent; border: none; }}
    QWidget#rowWrap {{ background: transparent; }}
    QLabel#cardTitle {{
        font-weight: 600; font-size: 14px;
        padding-bottom: 2px;
    }}
    QTableWidget {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        gridline-color: {p['border']};
        selection-background-color: {p['panel2']};
    }}
    QTableWidget::item:hover {{ background: {p['panel2']}; }}
    QHeaderView::section {{
        background: {p['panel2']};
        border: none;
        border-bottom: 1px solid {p['border']};
        padding: 6px;
    }}
    QLabel#dim {{ color: {p['text_dim']}; }}
    QLabel#sectionTitle {{ font-size: 17px; font-weight: 600; padding-bottom: 4px; }}
    QScrollArea {{ border: none; }}
    QMenu {{
        background: {p['panel']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 6px 0;
    }}
    QMenu::item {{
        padding: 7px 26px 7px 18px;
        border-radius: 4px;
        margin: 0 6px;
    }}
    QMenu::item:selected {{ background: {p['panel2']}; color: {p['accent']}; }}
    QMenu::item:disabled {{ color: {p['text_dim']}; }}
    QMenu::separator {{ height: 1px; background: {p['border']}; margin: 6px 10px; }}
    QMessageBox {{ background: {p['bg']}; }}
    QMessageBox QLabel {{ background: transparent; }}
    QMessageBox QPushButton {{ min-width: 72px; }}
    QToolTip {{
        background: {p['panel']};
        color: {p['text']};
        border: 1px solid {p['border']};
        padding: 4px;
    }}
    """
