"""WebAI 引擎真实 E2E：连 DeepSeek 网页跑文字翻译 + 贴图识别（手动跑，不进 CI）。

前置：
  - 先退出正在运行的 CtrlTranslate（含托盘）——两者共用 profile 目录
    ~/.ctrltrans/webview，Chromium 同目录双进程会锁冲突
  - 首跑会弹登录窗口，登录一次后 cookie 长期有效

用法：
  .venv/Scripts/python scripts/webai_e2e.py             # 文字翻译
  .venv/Scripts/python scripts/webai_e2e.py --image     # 追加贴图识别场景
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROMPT = "请将下面的英文翻译成中文，只输出译文：Knowledge is power."


def main() -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="store_true", help="追加贴图识别场景")
    opts = parser.parse_args()

    from app.core.webai import WebAIEngine

    # 直接用引擎默认 profile（~/.ctrltrans/webview 正式路径），保持与真实运行一致；
    # 首跑会弹出登录窗口，登录一次后 cookie 永久有效

    eng = WebAIEngine()
    state = {"scene": 1, "done": False, "retries": 0}

    def on_finished(text: str, _tid: int) -> None:
        print(f"[finished#{state['scene']}] {text!r}")
        if state["scene"] == 1 and opts.image:
            state["scene"] = 2
            QTimer.singleShot(1500, start_image)
        else:
            state["done"] = True
            QTimer.singleShot(800, app.quit)

    def on_failed(msg: str, _tid: int) -> None:
        print(f"[failed] {msg}")
        if "未登录" in msg and state["retries"] < 60:
            state["retries"] += 1
            QTimer.singleShot(5000, retry)  # 等用户在弹出窗口里完成登录
            return
        state["done"] = True
        QTimer.singleShot(800, app.quit)

    def retry() -> None:
        print(f"[e2e] 重试（{state['retries']}/60，登录后自动继续）…")
        if state["scene"] == 1:
            eng.submit_text(PROMPT)
        else:
            png = (ROOT / "assets" / "icon.png").read_bytes()
            eng.submit_image(png, "这张图片里是什么？一句话回答。")

    def start_image() -> None:
        print("[e2e] --- 场景2：贴图识别（窗口会短暂弹出接收 Ctrl+V）---")
        png = (ROOT / "assets" / "icon.png").read_bytes()
        eng.submit_image(png, "这张图片里是什么？一句话回答。")

    eng.chunk.connect(lambda s: print(f"[chunk] {s!r}"))
    eng.finished.connect(on_finished)
    eng.failed.connect(on_failed)
    eng.login_required.connect(lambda: print("[login_required] 请在弹出窗口登录 DeepSeek（登录后自动继续）…"))

    print("[e2e] --- 场景1：文字翻译 ---")
    eng.submit_text(PROMPT)
    QTimer.singleShot(600_000, app.quit)  # 总兜底超时（预留登录等待时间）
    code = app.exec()
    print(f"[e2e] 结束（scene={state['scene']}, finished={state['done']}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
