from pathlib import Path

from app.core import vocabulary
from app.db import database


def test_history_crud(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    database.add_history("hello", "你好", "chrome.exe", db)
    database.add_history("world", "世界", "acrobat.exe", db)
    assert len(database.list_history(db_path=db)) == 2
    assert len(database.list_history(search="hello", db_path=db)) == 1
    assert len(database.list_history(search="你好", db_path=db)) == 1
    database.clear_history(db)
    assert database.list_history(db_path=db) == []


def test_vocabulary_upsert_keeps_first_note(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    database.upsert_word("entropy", note="熵", context="ctx1", db_path=db)
    database.upsert_word("entropy", note="", context="ctx2", db_path=db)
    rows = database.list_words(db_path=db)
    assert len(rows) == 1
    assert rows[0]["note"] == "熵"
    assert rows[0]["context"] == "ctx2"


def test_vocabulary_upsert_updates_note_when_given(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    database.upsert_word("GAN", note="旧解释", db_path=db)
    database.upsert_word("GAN", note="生成对抗网络", db_path=db)
    assert database.list_words(db_path=db)[0]["note"] == "生成对抗网络"


def test_vocabulary_delete(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    database.upsert_word("dropout", db_path=db)
    row = database.list_words(db_path=db)[0]
    database.delete_word(row["id"], db)
    assert database.list_words(db_path=db) == []


def test_export_csv(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("app.db.database.DB_PATH", db)  # 业务层 export 走默认路径
    database.init_db(db)
    database.upsert_word("entropy", note="熵\n多行笔记", context="c", db_path=db)

    out = tmp_path / "out" / "vocab.csv"
    n = vocabulary.export_vocabulary_csv(out)
    assert n == 1
    content = out.read_text(encoding="utf-8-sig")
    assert "entropy" in content and "多行笔记" in content
    assert "\n多行" not in content.splitlines()[1]  # 换行被压平，CSV 行数不乱
