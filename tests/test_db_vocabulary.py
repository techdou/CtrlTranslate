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


def test_history_delete_single(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    id1 = database.add_history("hello", "你好", "chrome.exe", db)
    id2 = database.add_history("world", "世界", "acrobat.exe", db)
    database.delete_history(id1, db)
    rows = database.list_history(db_path=db)
    assert [r["id"] for r in rows] == [id2]
    database.delete_history(99999, db)  # 不存在的 id：静默无错
    assert len(database.list_history(db_path=db)) == 1


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


def test_history_source_app_filter(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    database.add_history("hello", "你好", "chrome.exe", db)
    database.add_history("world", "世界", "acrobat.exe", db)
    database.add_history("pdf text", "译文", "", db)
    assert len(database.list_history(source_app="chrome.exe", db_path=db)) == 1
    assert len(database.list_history(source_app="nonexist.exe", db_path=db)) == 0
    assert len(database.list_history(search="hello", source_app="chrome.exe", db_path=db)) == 1
    assert len(database.list_history(search="hello", source_app="acrobat.exe", db_path=db)) == 0
    assert database.list_source_apps(db) == ["acrobat.exe", "chrome.exe"]  # 新→旧，空串排除


def test_translation_cache_roundtrip_and_clear(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    assert database.get_cached_translation("k1", db) is None
    database.put_cached_translation("k1", "hello", "你好", db)
    assert database.get_cached_translation("k1", db) == "你好"
    database.put_cached_translation("k1", "hello", "你好v2", db)  # 重复写入=更新
    assert database.get_cached_translation("k1", db) == "你好v2"
    database.clear_translation_cache(db)
    assert database.get_cached_translation("k1", db) is None


def test_translation_cache_lru_trim(tmp_path: Path):
    db = tmp_path / "t.db"
    database.init_db(db)
    for i in range(database.CACHE_MAX_ROWS + 5):
        database.put_cached_translation(f"k{i:04d}", f"s{i}", f"r{i}", db)
    assert database.get_cached_translation("k0000", db) is None  # 最老被修剪
    assert database.get_cached_translation("k0004", db) is None
    assert database.get_cached_translation(f"k{database.CACHE_MAX_ROWS + 4:04d}", db) == \
        f"r{database.CACHE_MAX_ROWS + 4}"  # 最新保留
