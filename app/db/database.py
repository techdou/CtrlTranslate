"""SQLite 存储：翻译历史 + 生词本。

低频读写，每次操作短连接，避免跨线程共享连接的问题。
"""

from __future__ import annotations

import sqlite3
import threading
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
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);
"""


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


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


# ---------- 历史 ----------

def add_history(source_text: str, translated: str, source_app: str = "", db_path: Path | None = None) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO history (source_text, translated, source_app) VALUES (?, ?, ?)",
            (source_text, translated, source_app),
        )
        return cur.lastrowid


def list_history(
    limit: int = 200, offset: int = 0, search: str = "", db_path: Path | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM history"
    args: list[Any] = []
    if search:
        sql += " WHERE source_text LIKE ? OR IFNULL(translated,'') LIKE ?"
        like = f"%{search}%"
        args += [like, like]
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def clear_history(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM history")


# ---------- 生词本 ----------

def upsert_word(word: str, note: str = "", context: str = "", db_path: Path | None = None) -> None:
    """收藏单词：已存在时更新笔记与上下文（保留首次收藏时间）。"""
    with _connect(db_path) as conn:
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
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_word(word_id: int, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM vocabulary WHERE id = ?", (word_id,))


def word_exists(word: str, db_path: Path | None = None) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM vocabulary WHERE word = ?", (word.strip(),)).fetchone()
        return row is not None
