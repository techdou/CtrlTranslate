"""版本发布检查：读 GitHub releases（匿名 API），私有仓 / 无发布 / 断网优雅失败。"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("ctrltrans.update")

RELEASES_LATEST = "https://api.github.com/repos/techdou/CtrlTranslate/releases/latest"
RELEASES_PAGE = "https://github.com/techdou/CtrlTranslate/releases/latest"


def fetch_latest(timeout_s: float = 8.0) -> tuple[str, str]:
    """返回 (最新版本号, 发布页 URL)。失败抛异常，由调用方决定提示或静默。"""
    req = urllib.request.Request(
        RELEASES_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CtrlTranslate"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("release 缺少 tag_name")
    return tag.lstrip("vV"), data.get("html_url") or RELEASES_PAGE


def is_newer(latest: str, current: str) -> bool:
    """语义化版本比较（'v' 前缀与不等长补零都容忍）。"""

    def norm(v: str) -> list[int]:
        out: list[int] = []
        for part in v.strip().lstrip("vV").split("."):
            try:
                out.append(int(part))
            except ValueError:
                break
        return out or [0]

    a, b = norm(latest), norm(current)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return a > b


class UpdateChecker(QObject):
    """后台线程查 releases，结果经信号回主线程（不发 UI 不安全调用）。"""

    done = Signal(str, str)  # (最新版本, 发布页 URL)
    failed = Signal(str)     # 错误摘要（私有仓 404 / 断网等）

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            tag, url = fetch_latest()
            self.done.emit(tag, url)
        except Exception as e:  # noqa: BLE001 —— 任何网络/解析失败都走优雅提示
            logger.info("update check failed: %s", e)
            self.failed.emit(str(e)[:120])
