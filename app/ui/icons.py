"""内嵌 SVG 图标系统：Lucide 风格线框图标，按主题色着色渲染。

图标以 SVG 源码内嵌为字符串——不引外部资源文件，避开 PyInstaller 打包
后的资源路径坑。QSvgRenderer 不支持 currentColor 关键字，body 里的
currentColor 占位在渲染前做字符串替换，这是最稳的着色方式。

渲染结果按 (图标名, 颜色, 尺寸) 缓存；2x 物理像素渲染 + devicePixelRatio
保证高分屏（125%/150% 缩放）下图标边缘不糊。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 24x24 viewBox 线框图标（Lucide，ISC 许可）。默认 fill=none stroke=currentColor；
# 实心变体在 body 内自行覆盖 fill/stroke。
_ICONS: dict[str, str] = {
    # ---- 弹窗操作 ----
    "volume-2": (
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
        '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
        '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>'
    ),
    "stop": '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
    "star": (
        '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 '
        '5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'
    ),
    "star-filled": (
        '<polygon fill="currentColor" stroke="none" points="12 2 15.09 8.26 22 9.27 17 14.14 '
        '18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'
    ),
    "copy": (
        '<rect x="9" y="9" width="13" height="13" rx="2"/>'
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    ),
    "rotate-cw": (
        '<polyline points="23 4 23 10 17 10"/>'
        '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'
    ),
    "pin": (
        '<line x1="12" y1="17" x2="12" y2="22"/>'
        '<path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1'
        'a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9'
        'A2 2 0 0 0 5 15.24V17z"/>'
    ),
    "pin-filled": (
        '<line x1="12" y1="17" x2="12" y2="22"/>'
        '<path fill="currentColor" stroke="none" d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79'
        'l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76'
        'a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17z"/>'
    ),
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "chevron-up": '<polyline points="18 15 12 9 6 15"/>',
    # ---- 设置页侧栏 ----
    "languages": (
        '<path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/>'
        '<path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/>'
    ),
    "mouse-pointer-click": (
        '<path d="m9 9 5 12 1.8-5.2L21 14Z"/><path d="M7.2 2.2 8 5.1"/>'
        '<path d="m5.1 8-2.9-.8"/><path d="M14 4.1 12 6"/><path d="m6 12-1.9 2"/>'
    ),
    "file-text": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/>'
        '<line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'
    ),
    "palette": (
        '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/>'
        '<circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/>'
        '<circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/>'
        '<circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/>'
        '<path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688'
        "0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125"
        "a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554"
        'C21.965 6.012 17.461 2 12 2z"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="5" rx="9" ry="3"/>'
        '<path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>'
    ),
    "cloud-upload": (
        '<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/>'
        '<path d="M12 12v9"/><path d="m16 16-4-4-4 4"/>'
    ),
    # ---- 历史/生词本 ----
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "book-open": (
        '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>'
        '<path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>'
    ),
    "history": (
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
        '<path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>'
    ),
    "check": '<polyline points="20 6 9 17 4 12"/>',
}

_cache: dict[tuple[str, str, int], QIcon] = {}


def get_icon(name: str, color: str | QColor, size: int = 16) -> QIcon:
    """取着色后的图标。color 接受 '#RRGGBB' 或 QColor；size 为逻辑像素边长。"""
    qcolor = QColor(color) if isinstance(color, str) else color
    key = (name, qcolor.name(), size)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{_ICONS[name]}</svg>'
    ).replace("currentColor", qcolor.name())
    dpr = 2  # 超采样渲染，交给 devicePixelRatio 缩回逻辑尺寸
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    result = QIcon(pm)
    _cache[key] = result
    return result


def available() -> tuple[str, ...]:
    return tuple(_ICONS)
