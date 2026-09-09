"""渲染 UI 截图（状态留档 / 优化前后对比）。

用真实窗口（默认 windows 平台，非 offscreen）show → grab → close：
中文字体、控件 sizeHint、窗口真实高度全部可信（offscreen 下 QSS 指定的
中文字体不可用会渲染豆腐块，且布局尺寸失真）。
窗口只闪现几十毫秒，不抢焦点（不 activateWindow）。

生成：docs/screenshots/{popup,settings,library}_{dark,light}*.png
"""

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / "docs" / "screenshots"

DEMO_SOURCE = (
    "The gradient descent algorithm minimizes the loss function iteratively "
    "by computing first-order derivatives of the objective with respect to "
    "each parameter, which is often referred to as backpropagation in the "
    "context of neural network training."
)
DEMO_RESULT = (
    "梯度下降算法通过计算目标函数对每个参数的一阶导数，迭代地最小化损失函数；"
    "在神经网络训练的语境下，这一过程通常被称为反向传播。\n"
    "【术语】\n"
    "gradient descent — 梯度下降：一阶迭代优化算法\n"
    "backpropagation — 反向传播：链式法则自动计算梯度的方法\n"
    "first-order derivatives — 一阶导数"
)

SNAP_POS = QPoint(80, 80)  # 统一落位，避免依赖鼠标位置


def make_popup(qapp, theme: str):
    from app.config import DEFAULT_CONFIG
    from app.core.translator import Translator
    from app.core.tts import TTSService
    from app.ui.popup import TranslatePopup

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["popup"]["theme"] = theme
    tr = Translator(lambda: cfg)
    tts = TTSService(lambda: cfg)
    pop = TranslatePopup(lambda: cfg, tts, tr)
    return pop


def _snap(widget, qapp, path: Path) -> None:
    widget.move(SNAP_POS)
    widget.show()
    qapp.processEvents()
    QTimer.singleShot(60, lambda: (_save(widget, path), widget.close()))
    deadline = __import__("time").monotonic() + 3
    while widget.isVisible() and __import__("time").monotonic() < deadline:
        qapp.processEvents()
        __import__("time").sleep(0.02)


def _save(widget, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(path))


def snap_popup(qapp, theme: str, name: str) -> None:
    pop = make_popup(qapp, theme)
    pop._translator.translate = lambda text, use_cache=True: (setattr(pop, "_task_id", 99) or 99)  # mock：不发真实请求

    if name.endswith("loading"):
        pop.show_translation(DEMO_SOURCE, "clipboard")  # 走真实产品路径（含 120 字符截断）
    elif name.endswith("done"):
        tid = pop.show_translation(DEMO_SOURCE, "clipboard")
        pop.on_chunk("梯度下降算法", tid)
        pop.on_done(DEMO_RESULT, tid)
    elif name.endswith("error"):
        pop.show_message("翻译失败：API Key 无效或没有权限（401），请检查 Key 与所选服务商是否匹配")
    elif name.endswith("pinned"):
        tid = pop.show_translation(DEMO_SOURCE, "clipboard")
        pop.on_done(DEMO_RESULT, tid)
        pop.btn_pin.setChecked(True)
    # done/error/pinned 不再固定 resize——让 _fit_height 的自适应尺寸原样入图
    pop.move(SNAP_POS)
    pop.show()
    qapp.processEvents()
    _save_later(pop, qapp, OUT / f"popup_{theme}_{name}.png")


def _save_later(widget, qapp, path: Path) -> None:
    import time

    QTimer.singleShot(60, lambda: (_save(widget, path), widget.close()))
    deadline = time.monotonic() + 3
    while widget.isVisible() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def snap_settings(qapp, theme: str, page: int) -> None:
    from app.config import DEFAULT_CONFIG
    from app.core.translator import Translator
    from app.ui.settings import SettingsDialog

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["popup"]["theme"] = theme
    dlg = SettingsDialog(cfg, Translator(lambda: cfg))
    dlg.nav.setCurrentRow(page)
    _snap(dlg, qapp, OUT / f"settings_{theme}_p{page}.png")


def snap_library(qapp, theme: str) -> None:
    import app.db.database as db
    from app.ui.library import LibraryWindow

    tmpdb = Path(tempfile.mkdtemp()) / "snap.db"
    db._initialized.discard(str(tmpdb.resolve()))
    db.add_history(DEMO_SOURCE, DEMO_RESULT.split("【术语】")[0].strip(), "chrome.exe", tmpdb)
    db.add_history("Overfitting occurs when a model learns noise instead of signal.",
                   "过拟合：模型学到噪声而非信号。", "Acrobat.exe", tmpdb)
    db.upsert_word("entropy", note="熵 — 信息不确定性的度量", context="Shannon entropy", db_path=tmpdb)
    db.upsert_word("overfitting", note="过拟合", context="noise learning", db_path=tmpdb)
    db.upsert_word("backpropagation", note="反向传播", context="chain rule", db_path=tmpdb)

    orig = db.DB_PATH
    db.DB_PATH = tmpdb
    try:
        win = LibraryWindow(theme=theme)
        _snap(win, qapp, OUT / f"library_{theme}.png")
        win2 = LibraryWindow(theme=theme)
        win2.ed_search.setText("不存在的关键词zzz")
        win2.refresh()
        _snap(win2, qapp, OUT / f"library_{theme}_empty.png")
    finally:
        db.DB_PATH = orig


def main() -> None:
    from app.ui.theme import build_qss, palette

    qapp = QApplication([])
    # 弹窗按钮的 hover/primary 规则已收敛到全局 QSS（与 main.py 同构），
    # 不挂全局样式表的话弹窗截图会缺按钮态样式
    qapp.setStyleSheet(build_qss(palette("dark")))
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        qapp.setStyleSheet(build_qss(palette(theme)))
        snap_popup(qapp, theme, "loading")
        snap_popup(qapp, theme, "done")
        snap_popup(qapp, theme, "error")
        snap_popup(qapp, theme, "pinned")
        snap_settings(qapp, theme, 0)
        snap_library(qapp, theme)
    print("saved to", OUT)


if __name__ == "__main__":
    main()
