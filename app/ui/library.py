"""历史记录 + 生词本窗口：搜索、删除、导出 CSV；双击条目回看原文与译文。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import vocabulary
from app.db import database
from app.ui.theme import build_qss, palette

logger = logging.getLogger("ctrltrans.library")


_ROW_DATA_ROLE = Qt.UserRole + 1  # 第一列额外挂整行 dict，双击回看用


class LibraryWindow(QMainWindow):
    def __init__(self, theme: str = "dark", popup=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("历史与生词本 · CtrlTranslate")
        self.resize(860, 560)
        self.theme = theme
        self._popup = popup  # 双击回看时复用翻译弹窗（朗读/复制/重译按钮现成）

        self.tabs = QTabWidget()
        self.tab_history = self._build_table(["时间", "原文", "译文", "来源"])
        self.tab_vocab = self._build_table(["时间", "词/原文", "笔记", "上下文"])
        self.tabs.addTab(self.tab_history, "翻译历史")
        self.tabs.addTab(self.tab_vocab, "生词本")
        self.tab_history.cellDoubleClicked.connect(
            lambda r, _c: self._open_row(self.tab_history, r, "history"))
        self.tab_vocab.cellDoubleClicked.connect(
            lambda r, _c: self._open_row(self.tab_vocab, r, "vocab"))

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索…（回车刷新）")
        self.ed_search.returnPressed.connect(self.refresh)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self._export)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_clear = QPushButton("清空历史")
        self.btn_clear.clicked.connect(self._clear_history)

        bar = QHBoxLayout()
        bar.addWidget(self.ed_search, 1)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_delete)
        bar.addWidget(self.btn_clear)

        wrap = QVBoxLayout()
        wrap.addLayout(bar)
        wrap.addWidget(self.tabs, 1)
        central = QWidget()
        central.setLayout(wrap)
        self.setCentralWidget(central)

        self.btn_clear.setVisible(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.statusBar().showMessage("双击条目回看原文与译文（可朗读 / 复制 / 重译）")
        self._apply_theme()
        self.refresh()

    # ---------------------------------------------------------------- 构建

    def _build_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setWordWrap(False)  # 长文本截断入 tooltip，行高统一利于扫读
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(32)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setStretchLastSection(False)
        for col in range(1, len(headers)):  # 内容列弹性分宽，不再被固定宽度挤爆
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self._apply_theme()

    def _apply_theme(self) -> None:
        self.setStyleSheet(build_qss(palette(self.theme)))

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        search = self.ed_search.text().strip()
        if self.tabs.currentIndex() == 0:
            rows = database.list_history(search=search)
            self._fill(self.tab_history, rows, ["created_at", "source_text", "translated", "source_app"])
            self._show_placeholder(self.tab_history, rows, "暂无翻译历史 · 划词翻译后自动保存")
        else:
            rows = database.list_words(search=search)
            self._fill(self.tab_vocab, rows, ["created_at", "word", "note", "context"])
            self._show_placeholder(self.tab_vocab, rows, "生词本为空 · 在翻译弹窗点「收藏」加入")

    def _fill(self, table: QTableWidget, rows: list[dict], cols: list[str]) -> None:
        p = palette(self.theme)
        table.setRowCount(0)
        for r, row in enumerate(rows):
            table.insertRow(r)
            for c, key in enumerate(cols):
                raw = str(row.get(key) or "")
                shown = raw[:60] + "…" if len(raw) > 60 else raw
                item = QTableWidgetItem(shown)
                item.setToolTip(raw)  # 悬停看全文
                if c == 0:
                    item.setForeground(QColor(p["text_dim"]))  # 时间列用主题次级色
                    item.setData(Qt.UserRole, row.get("id"))  # 删除操作用
                    item.setData(_ROW_DATA_ROLE, row)          # 双击回看用
                table.setItem(r, c, item)

    def _show_placeholder(self, table: QTableWidget, rows: list[dict], text: str) -> None:
        if rows:
            if getattr(table, "_placeholder", None):
                table._placeholder.deleteLater()
                table._placeholder = None
            return
        from PySide6.QtWidgets import QLabel

        p = palette(self.theme)
        lbl = QLabel(text, table)
        lbl.setStyleSheet(f"color: {p['text_dim']}; font-size: 13px; background: transparent;")
        lbl.setGeometry(table.width() // 2 - 140, table.height() // 2 - 20, 280, 24)
        lbl.setVisible(True)
        table._placeholder = lbl

    def _on_tab_changed(self, index: int) -> None:
        self.btn_clear.setVisible(index == 0)
        self.btn_delete.setVisible(index == 1)
        self.refresh()

    # ---------------------------------------------------------------- 操作

    def _open_row(self, table: QTableWidget, row: int, kind: str) -> None:
        """双击行：弹出原文与译文回看（不发翻译请求）。"""
        if self._popup is None:
            return
        item = table.item(row, 0)
        if item is None:
            return
        data = item.data(_ROW_DATA_ROLE) or {}
        if kind == "history":
            self._popup.show_result(data.get("source_text") or "", data.get("translated") or "")
        else:
            self._popup.show_result(data.get("word") or "", data.get("note") or "")

    def _export(self) -> None:
        default = "生词本.csv" if self.tabs.currentIndex() == 1 else "翻译历史.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default, "CSV (*.csv)")
        if not path:
            return
        try:
            if self.tabs.currentIndex() == 1:
                n = vocabulary.export_vocabulary_csv(_p(path), search=self.ed_search.text().strip())
            else:
                n = vocabulary.export_history_csv(_p(path), search=self.ed_search.text().strip())
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条到\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _delete_selected(self) -> None:
        rows = {i.row() for i in self.tab_vocab.selectedIndexes()}
        if not rows:
            return
        if QMessageBox.question(self, "删除", f"删除选中的 {len(rows)} 个词条？") != QMessageBox.Yes:
            return
        for r in sorted(rows, reverse=True):
            row_id = self.tab_vocab.item(r, 0).data(Qt.UserRole)
            if row_id is not None:
                database.delete_word(row_id)
        self.refresh()

    def _clear_history(self) -> None:
        if QMessageBox.question(
            self, "清空历史", "确定清空全部翻译历史？（生词本不受影响）"
        ) != QMessageBox.Yes:
            return
        database.clear_history()
        self.refresh()


def _p(path: str):
    from pathlib import Path

    return Path(path)
