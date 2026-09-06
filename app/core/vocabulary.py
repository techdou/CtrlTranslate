"""历史与生词本的业务封装 + 导出。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.db import database


def record_history(cfg: dict, source_text: str, translated: str, source_app: str = "") -> None:
    if cfg.get("general", {}).get("history_enabled", True):
        try:
            database.add_history(source_text, translated, source_app)
        except Exception:
            pass  # 记录失败不影响主流程


def export_rows_csv(rows: list[dict[str, Any]], path: Path, columns: list[str]) -> None:
    """通用 CSV 导出，UTF-8 BOM 方便 Excel 直接打开。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            flat = {
                c: r.get(c).replace("\n", " ") if isinstance(r.get(c), str) else r.get(c)
                for c in columns
            }
            writer.writerow(flat)


HISTORY_COLUMNS = ["created_at", "source_text", "translated", "source_app"]
VOCAB_COLUMNS = ["created_at", "word", "note", "context"]


def export_history_csv(path: Path, search: str = "", source_app: str = "") -> int:
    rows = database.list_history(search=search, source_app=source_app)
    export_rows_csv(rows, path, HISTORY_COLUMNS)
    return len(rows)


def export_vocabulary_csv(path: Path, search: str = "") -> int:
    """Anki 友好格式：front=word，back=note（可用逗号分隔字段直接导入）。"""
    rows = database.list_words(search=search)
    export_rows_csv(rows, path, VOCAB_COLUMNS)
    return len(rows)
