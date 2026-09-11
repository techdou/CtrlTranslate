"""历史记录 + 生词本窗口：搜索、删除、导出 CSV；双击条目回看原文与译文。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from app.ui.icons import get_icon
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
        self.ed_search.setClearButtonEnabled(True)
        self._search_action = self.ed_search.addAction(
            get_icon("search", palette(theme)["text_dim"]),
            QLineEdit.ActionPosition.LeadingPosition)
        self.ed_search.returnPressed.connect(self.refresh)
        self.cb_source = QComboBox()
        self.cb_source.addItem("全部来源", "")
        self.cb_source.currentIndexChanged.connect(lambda _i: self.refresh())
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self._export)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_clear = QPushButton("清空历史")
        self.btn_clear.setObjectName("danger")  # 全量删除，用 error 色与普通操作拉开
        self.btn_clear.clicked.connect(self._clear_history)

        bar = QHBoxLayout()
        bar.addWidget(self.ed_search, 1)
        bar.addWidget(self.cb_source)
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
        self.tab_history.installEventFilter(self)  # 空态占位随窗口 resize 重新居中
        self.tab_vocab.installEventFilter(self)
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
        table.verticalHeader().setDefaultSectionSize(40)  # 行高给呼吸感，扫读不挤
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
        p = palette(self.theme)
        self.setStyleSheet(build_qss(p))
        self._search_action.setIcon(get_icon("search", p["text_dim"]))

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        search = self.ed_search.text().strip()
        if self.tabs.currentIndex() == 0:
            self._sync_source_apps()
            source = self.cb_source.currentData() or ""
            rows = database.list_history(search=search, source_app=source)
            self._fill(self.tab_history, rows, ["created_at", "source_text", "translated", "source_app"])
            # 搜索无命中与库真空是两种状态，文案不能混用（否则误以为历史被清空）
            empty = f"没有匹配「{search}」的记录" if search else "暂无翻译历史 · 划词翻译后自动保存"
            self._show_placeholder(self.tab_history, rows, empty)
        else:
            rows = database.list_words(search=search)
            self._fill(self.tab_vocab, rows, ["created_at", "word", "note", "context"])
            empty = f"没有匹配「{search}」的记录" if search else "生词本为空 · 在翻译弹窗点「收藏」加入"
            self._show_placeholder(self.tab_vocab, rows, empty)

    def _sync_source_apps(self) -> None:
        """来源下拉重建（保留当前选择）；只在历史 Tab 显示。"""
        selected = self.cb_source.currentData() or ""
        self.cb_source.blockSignals(True)
        self.cb_source.clear()
        self.cb_source.addItem("全部来源", "")
        for app in database.list_source_apps():
            self.cb_source.addItem(app, app)
        idx = self.cb_source.findData(selected)
        self.cb_source.setCurrentIndex(max(0, idx))
        self.cb_source.blockSignals(False)

    def _fill(self, table: QTableWidget, rows: list[dict], cols: list[str]) -> None:
        p = palette(self.theme)
        table.setRowCount(0)
        for r, row in enumerate(rows):
            table.insertRow(r)
            for c, key in enumerate(cols):
                raw = str(row.get(key) or "")
                shown = _fmt_time(raw) if c == 0 else raw[:60] + "…" if len(raw) > 60 else raw
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
        p = palette(self.theme)
        # 空态 = 大图标 + 指引文案的纵向容器（单个灰字太单薄），居中、不挡表格交互
        box = QWidget(table)
        box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        icon_name = "history" if table is self.tab_history else "book-open"
        ic = QLabel()
        ic.setPixmap(get_icon(icon_name, p["text_dim"], 36).pixmap(36, 36))
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {p['text_dim']}; font-size: 13px; background: transparent;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ic)
        lay.addWidget(lbl)
        box.adjustSize()  # 尺寸按内容自适应，居中计算才准
        _center_in_table(table, box)
        box.setVisible(True)
        table._placeholder = box

    def eventFilter(self, obj, event) -> bool:
        # 窗口/表格尺寸变化后，空态占位重新居中（一次性 setGeometry 会跑偏）
        if event.type() == QEvent.Resize:
            for table in (self.tab_history, self.tab_vocab):
                lbl = getattr(table, "_placeholder", None)
                if lbl is not None:
                    _center_in_table(table, lbl)
        return super().eventFilter(obj, event)

    def _on_tab_changed(self, index: int) -> None:
        self.btn_clear.setVisible(index == 0)  # 全量清空只作用于历史；单条删除两个 Tab 都可用
        self.cb_source.setVisible(index == 0)  # 来源过滤只对历史有意义
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
            on_history = self.tabs.currentIndex() == 0
            source = (self.cb_source.currentData() or "") if on_history else ""
            if on_history:
                n = vocabulary.export_history_csv(
                    _p(path), search=self.ed_search.text().strip(), source_app=source)
            else:
                n = vocabulary.export_vocabulary_csv(_p(path), search=self.ed_search.text().strip())
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条到\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _delete_selected(self) -> None:
        on_history = self.tabs.currentIndex() == 0
        table = self.tab_history if on_history else self.tab_vocab
        rows = {i.row() for i in table.selectedIndexes()}
        if not rows:
            return
        unit = "条历史" if on_history else "个词条"
        if QMessageBox.question(self, "删除", f"删除选中的 {len(rows)} {unit}？") != QMessageBox.Yes:
            return
        delete = database.delete_history if on_history else database.delete_word
        for r in sorted(rows, reverse=True):
            row_id = table.item(r, 0).data(Qt.UserRole)
            if row_id is not None:
                delete(row_id)
        self.refresh()

    def _clear_history(self) -> None:
        if QMessageBox.question(
            self, "清空历史", "确定清空全部翻译历史？（生词本不受影响）"
        ) != QMessageBox.Yes:
            return
        database.clear_history()
        self.refresh()


def _fmt_time(raw: str) -> str:
    """时间列短格式：当年省掉年份与秒，跨年保留日期；完整时间仍在 tooltip。"""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if dt.year == datetime.now().year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def _center_in_table(table: QTableWidget, lbl) -> None:
    lbl.move(max(0, (table.width() - lbl.width()) // 2),
             max(0, (table.height() - lbl.height()) // 2))


def _p(path: str):
    from pathlib import Path

    return Path(path)
