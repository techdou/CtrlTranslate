"""生成应用图标：圆角方块 + "译" 字，输出 icon.png / icon.ico 到 assets/。

独立脚本，仅开发期运行。ico 采用 PNG-in-ICO（Vista+ 原生支持），手工拼
容器头，避免引入 Pillow。
"""

import struct
from pathlib import Path

OUT = (Path(__file__).resolve().parent.parent / "assets")


def render(size: int) -> bytes:
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap

    src = QImage(256, 256, QImage.Format_ARGB32)
    src.fill(Qt.transparent)
    p = QPainter(src)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#2E6F69"), 2))
    p.setBrush(QBrush(QColor("#4DB6AC")))
    p.drawRoundedRect(QRectF(10, 10, 236, 236), 56, 56)
    p.setPen(QColor("#0E2B28"))
    f = QFont("Microsoft YaHei UI")
    f.setWeight(QFont.Weight.Black)
    f.setPixelSize(132)
    p.setFont(f)
    p.drawText(src.rect(), Qt.AlignmentFlag.AlignCenter, "译")
    p.end()

    pm = QPixmap.fromImage(src)
    if size != 256:
        pm = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    buf = pm.toImage()
    out = buf.toPNG() if hasattr(buf, "toPNG") else None
    if out is None:
        # Qt6 无 toPNG；借 QBuffer
        from PySide6.QtCore import QBuffer, QIODevice

        b = QBuffer()
        b.open(QIODevice.OpenModeFlag.WriteOnly)
        buf.save(b, "PNG")
        out = bytes(b.data())
    return out


def write_ico(pngs: list[tuple[int, bytes]], path: Path) -> None:
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b""
    offset = 6 + 16 * len(pngs)
    blobs = b""
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def main() -> None:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([])  # QPainter 字体渲染需要
    OUT.mkdir(parents=True, exist_ok=True)

    png256 = render(256)
    (OUT / "icon.png").write_bytes(png256)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [(s, render(s)) for s in sizes]
    write_ico(pngs, OUT / "icon.ico")
    print(f"icons written to {OUT}: icon.png, icon.ico ({len(sizes)} sizes)")


if __name__ == "__main__":
    main()
