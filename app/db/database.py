"""SQLite 存储：翻译历史 + 生词本。

低频读写，每次操作短连接，避免跨线程共享连接的问题。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

DB_PATH = DATA_DIR / "data.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text TEXT NOT NULL,
    translated TEXT,
    source_app TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS vocabulary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL UNIQUE,
    note TEXT,
    context TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS translation_cache (
    key TEXT PRIMARY KEY,
    source_text TEXT,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);
"""

CACHE_MAX_ROWS = 500  # 粗 LRU：超限删最旧


_init_lock = threading.Lock()
_initialized: set[str] = set()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    key = str(db_path.resolve())
    if key not in _initialized:  # 懒建表，幂等，任意入口先调也不会炸
        with _init_lock:
            if key not in _initialized:
                conn.executescript(_SCHEMA)
                _initialized.add(key)
    return conn


@contextmanager
def _conn(db_path: Path | None = None):
    """事务语义（成功提交/异常回滚）+ 确保关闭。

    sqlite3.Connection 自带的 __exit__ 只管事务不关连接；直接 `with _connect()`
    的连接要等 GC 才释放，Windows 下会锁住 db 文件（清库/导出时撞 WinError 32）。
    """
    conn = _connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.executescript(_SCHEMA)


# ---------- 历史 ----------

def add_history(source_text: str, translated: str, source_app: str = "", db_path: Path | None = None) -> int:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO history (source_text, translated, source_app) VALUES (?, ?, ?)",
            (source_text, translated, source_app),
        )
        return cur.lastrowid


def list_history(
    limit: int = 200, offset: int = 0, search: str = "", source_app: str = "",
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM history"
    conds: list[str] = []
    args: list[Any] = []
    if search:
        conds.append("(source_text LIKE ? OR IFNULL(translated,'') LIKE ?)")
        like = f"%{search}%"
        args += [like, like]
    if source_app:
        conds.append("source_app = ?")
        args.append(source_app)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_history(row_id: int, db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM history WHERE id = ?", (row_id,))


def list_source_apps(db_path: Path | None = None) -> list[str]:
    """历史里出现过的来源应用（去重，新→旧），供过滤下拉用。"""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_app FROM history "
            "WHERE source_app != '' ORDER BY id DESC"
        ).fetchall()
        return [r["source_app"] for r in rows]


def clear_history(db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM history")


# ---------- 备份（全量导出 / 合并导入） ----------

def export_all_history(db_path: Path | None = None) -> list[dict[str, Any]]:
    """全量历史（list_history 有分页上限，备份需要不带 limit 的读取）。"""
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT source_text, translated, source_app, created_at FROM history ORDER BY id"
        ).fetchall()]


def import_history(rows: list[dict[str, Any]], db_path: Path | None = None) -> tuple[int, int]:
    """合并导入历史（显式保留 created_at）。按 (source_text, translated, created_at)
    去重——同一份快照恢复两次不会翻倍。返回 (导入数, 跳过数)。"""
    with _conn(db_path) as conn:
        existing = {
            (r["source_text"], r["translated"], r["created_at"])
            for r in conn.execute(
                "SELECT source_text, translated, created_at FROM history"
            ).fetchall()
        }
        imported = skipped = 0
        for r in rows:
            key = (str(r.get("source_text") or ""), str(r.get("translated") or ""),
                   str(r.get("created_at") or ""))
            if not key[0] or key in existing:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO history (source_text, translated, source_app, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key[0], key[1], str(r.get("source_app") or ""), key[2] or None),
            )
            existing.add(key)
            imported += 1
        return (imported, skipped)


def import_vocabulary(rows: list[dict[str, Any]], db_path: Path | None = None) -> int:
    """合并导入生词本：新词按快照的 created_at 原样入库；已存在的词只在本地
    note/context 为空时补全，不动本地首次收藏时间。返回新增词数。"""
    with _conn(db_path) as conn:
        existing = {
            r["word"] for r in conn.execute("SELECT word FROM vocabulary").fetchall()
        }
        imported = 0
        for r in rows:
            word = str(r.get("word") or "").strip()
            if not word:
                continue
            note = str(r.get("note") or "")
            context = str(r.get("context") or "")
            created_at = str(r.get("created_at") or "") or None
            if word in existing:
                conn.execute(
                    "UPDATE vocabulary SET "
                    "note = CASE WHEN note = '' AND ? != '' THEN ? ELSE note END, "
                    "context = CASE WHEN context = '' AND ? != '' THEN ? ELSE context END "
                    "WHERE word = ?",
                    (note, note, context, context, word),
                )
                continue
            conn.execute(
                "INSERT INTO vocabulary (word, note, context, created_at) VALUES (?, ?, ?, ?)",
                (word, note, context, created_at),
            )
            existing.add(word)
            imported += 1
        return imported


# ---------- 翻译缓存 ----------

def get_cached_translation(key: str, db_path: Path | None = None) -> str | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT result FROM translation_cache WHERE key = ?", (key,)
        ).fetchone()
        return row["result"] if row else None


def put_cached_translation(key: str, source_text: str, result: str,
                           db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO translation_cache (key, source_text, result) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET result = excluded.result, created_at = datetime('now','localtime')",
            (key, source_text, result),
        )
        # 粗 LRU：超限按 created_at 删最旧（命中会刷新 created_at；rowid 处理同秒并列）
        conn.execute(
            "DELETE FROM translation_cache WHERE key IN ("
            "  SELECT key FROM translation_cache "
            "  ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (CACHE_MAX_ROWS,),
        )


def clear_translation_cache(db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM translation_cache")


# ---------- 生词本 ----------

def upsert_word(word: str, note: str = "", context: str = "", db_path: Path | None = None) -> None:
    """收藏单词：已存在时更新笔记与上下文（保留首次收藏时间）。"""
    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vocabulary (word, note, context) VALUES (?, ?, ?)
            ON CONFLICT(word) DO UPDATE SET
                note = CASE WHEN excluded.note != '' THEN excluded.note ELSE vocabulary.note END,
                context = CASE WHEN excluded.context != '' THEN excluded.context ELSE vocabulary.context END
            """,
            (word.strip(), note, context),
        )


def list_words(search: str = "", db_path: Path | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM vocabulary"
    args: list[Any] = []
    if search:
        sql += " WHERE word LIKE ? OR IFNULL(note,'') LIKE ?"
        like = f"%{search}%"
        args += [like, like]
    sql += " ORDER BY id DESC"
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_word(word_id: int, db_path: Path | None = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM vocabulary WHERE id = ?", (word_id,))


def word_exists(word: str, db_path: Path | None = None) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM vocabulary WHERE word = ?", (word.strip(),)).fetchone()
        return row is not None
