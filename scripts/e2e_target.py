"""e2e 测试靶窗口：一个带预置英文的 QTextEdit，启动后自动全选。

独立进程、无标签纠缠，Qt6 的 UIA TextPattern 支持成熟，
作为「被取词应用」比系统记事本（标签模式单进程）更可控。
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QTextEdit

TEXT = "The gradient descent algorithm minimizes the loss function iteratively."

app = QApplication(sys.argv)
app.setApplicationName("CtrlTranslateE2ETarget")
editor = QTextEdit()
editor.setWindowTitle("E2E target - do not touch")
editor.setPlainText(TEXT)
editor.resize(700, 220)
editor.show()
editor.raise_()
editor.activateWindow()
QTimer.singleShot(1200, editor.selectAll)  # 自动全选，无需模拟 Ctrl+A
sys.exit(app.exec())
