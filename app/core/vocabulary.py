"""历史与生词本的业务封装 + 导出。"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from app.db import database

# 术语段解析（popup 渲染与 main 网页模式自动归档共用一份规则）
TERM_SECTION_RE = re.compile(r"^【术语】\s*$", re.MULTILINE)
TERM_SEPARATORS = [" — ", "—", " - ", " – ", "-"]


def parse_terms(text: str) -> list[tuple[str, str]]:
    """解析译文里的【术语】段：每行一条 (术语, 含义)。分隔符容错多种破折号。"""
    parts = TERM_SECTION_RE.split(text or "")
    if len(parts) < 2:
        return []
    out: list[tuple[str, str]] = []
    for raw in parts[1].splitlines():
        ln = raw.strip().lstrip("-·•* ").strip()  # LLM 偶尔加列表符号
        if not ln:
            continue
        for sep in TERM_SEPARATORS:
            word, _, meaning = ln.partition(sep)
            if meaning.strip():
                out.append((word.strip()[:500], meaning.strip()[:400]))
                break
        else:
            out.append((ln[:500], ""))  # 无分隔符：整行当术语
    return out


def archive_terms(terms: list[tuple[str, str]], context: str = "") -> int:
    """术语批量入生词本（同词幂等）；返回成功条数。单条失败不中断批量。"""
    ok = 0
    for word, meaning in terms:
        try:
            database.upsert_word(word, note=meaning, context=context[:200])
            ok += 1
        except Exception:
            pass
    return ok


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
