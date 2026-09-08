"""WebDAV 备份测试：快照导出导入（幂等/时间戳保留）、WebDAV 客户端（fake httpx2）、错误文案。"""

import sys
import types
from unittest import mock

import pytest

from app.core import backup
from app.db import database

WCFG = ("https://dav.example.com/dav/", "user@example.com", "app-password", "CtrlTranslate")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """隔离的数据库（monkeypatch 模块级 DB_PATH，服务层函数走默认路径）。"""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)
    return db_path


# ---------------------------------------------------------------- 快照 roundtrip

def test_snapshot_roundtrip_preserves_rows_and_timestamps(db):
    database.add_history("hello", "你好", "Zotero", db_path=db)
    database.upsert_word("bandwidth", note="带宽", context="net paper", db_path=db)
    # created_at 由 DB 默认值生成，导入时必须原样保留
    snap = backup.build_snapshot(db)

    other = db.parent / "other.db"
    database.init_db(other)
    stats = backup.apply_snapshot(snap, db_path=other)

    assert stats["history_imported"] == 1 and stats["vocabulary_imported"] == 1
    hist = database.list_history(db_path=other)
    assert hist[0]["source_text"] == "hello" and hist[0]["source_app"] == "Zotero"
    assert hist[0]["created_at"] == snap["history"][0]["created_at"]
    words = database.list_words(db_path=other)
    assert words[0]["word"] == "bandwidth"
    assert words[0]["created_at"] == snap["vocabulary"][0]["created_at"]


def test_apply_snapshot_idempotent(db):
    database.add_history("a", "甲", "", db_path=db)
    database.upsert_word("w1", note="", context="", db_path=db)
    snap = backup.build_snapshot(db)

    database.clear_history(db_path=db)
    other = db.parent / "restored.db"

    first = backup.apply_snapshot(snap, db_path=other)
    second = backup.apply_snapshot(snap, db_path=other)  # 同一快照恢复两次
    assert first["history_imported"] == 1
    assert second["history_imported"] == 0 and second["history_skipped"] == 1  # 不翻倍
    assert second["vocabulary_imported"] == 0
    assert len(database.list_history(db_path=other)) == 1


def test_apply_snapshot_partial_merge(db):
    """快照与本地各有独有条目 + 一条同源重复（相同 created_at）：只补新，不动旧。
    去重键含 created_at——历史上同一句话不同时间翻译本就是两条记录。"""
    database.import_history([
        {"source_text": "dup", "translated": "重复", "source_app": "", "created_at": "2026-09-01 10:00:00"},
        {"source_text": "local-only", "translated": "本地独有", "source_app": "", "created_at": "2026-09-01 11:00:00"},
    ], db_path=db)
    snap = {
        "version": 1, "exported_at": "2026-09-09 00:00:00", "app_version": "test",
        "history": [
            {"source_text": "dup", "translated": "重复", "source_app": "", "created_at": "2026-09-01 10:00:00"},
            {"source_text": "remote-only", "translated": "远端独有", "source_app": "", "created_at": "2026-09-02 10:00:00"},
        ],
        "vocabulary": [],
    }
    stats = backup.apply_snapshot(snap, db_path=db)
    assert stats["history_imported"] == 1 and stats["history_skipped"] == 1
    assert len(database.list_history(db_path=db)) == 3


def test_import_vocabulary_fills_empty_note_keeps_existing(db):
    database.upsert_word("keep", note="本地笔记", context="", db_path=db)
    snap = {"version": 1, "exported_at": "", "app_version": "",
            "history": [],
            "vocabulary": [
                {"word": "keep", "note": "远端笔记", "context": "远端上下文", "created_at": "2026-09-01 00:00:00"},
                {"word": "new", "note": "新词", "context": "", "created_at": "2026-09-02 00:00:00"},
            ]}
    stats = backup.apply_snapshot(snap, db_path=db)
    assert stats["vocabulary_imported"] == 1  # 只有 new 是新增
    words = {w["word"]: w for w in database.list_words(db_path=db)}
    assert words["keep"]["note"] == "本地笔记"      # 非空笔记不被覆盖
    assert words["keep"]["context"] == "远端上下文"  # 空上下文被补全
    assert words["new"]["created_at"] == "2026-09-02 00:00:00"


def test_snapshot_json_roundtrip():
    snap = {"version": 1, "exported_at": "t", "app_version": "v", "history": [], "vocabulary": []}
    assert backup.snapshot_from_json(backup.snapshot_to_json(snap)) == snap


def test_snapshot_from_json_rejects_bad_version():
    with pytest.raises(ValueError):
        backup.snapshot_from_json('{"version": 99}')
    with pytest.raises(ValueError):
        backup.apply_snapshot({"version": 2})


def test_build_snapshot_excludes_cache(db):
    database.put_cached_translation("k", "src", "result", db_path=db)
    snap = backup.build_snapshot(db)
    assert "cache" not in snap and "translation_cache" not in snap


# ---------------------------------------------------------------- fake httpx2

class _FakeHTTPStatusError(Exception):
    def __init__(self, resp):
        super().__init__(f"HTTP {resp.status_code}")
        self.response = resp


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPStatusError(self)


class _FakeClient:
    """记录所有请求；按 (method, url) 查 responses 路由表返回。"""
    all_instances: list["_FakeClient"] = []
    responses: dict[tuple[str, str], _Resp] = {}

    def __init__(self, **kw):
        self.kwargs = kw
        self.requests: list[tuple[str, str]] = []
        _FakeClient.all_instances.append(self)

    def request(self, method, url, headers=None):
        self.requests.append((method, url))
        return _FakeClient.responses.get((method, url), _Resp(404))

    def put(self, url, content=None):
        self.requests.append(("PUT", url))
        self.__dict__.setdefault("bodies", []).append(content)
        return _FakeClient.responses.get(("PUT", url), _Resp(201))

    def get(self, url):
        self.requests.append(("GET", url))
        return _FakeClient.responses.get(("GET", url), _Resp(404))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_httpx2(monkeypatch):
    _FakeClient.all_instances = []
    _FakeClient.responses = {}
    mod = types.ModuleType("httpx2")
    mod.Client = _FakeClient
    mod.HTTPStatusError = _FakeHTTPStatusError
    monkeypatch.setitem(sys.modules, "httpx2", mod)
    return _FakeClient


def _url(filename=""):
    return backup._remote_url(WCFG, filename)


def test_webdav_put_sends_snapshot(fake_httpx2):
    backup.webdav_put(WCFG, b"{}", proxy="")
    client = fake_httpx2.all_instances[-1]
    assert ("PUT", _url("backup.json")) in client.requests
    assert client.__dict__["bodies"] == [b"{}"]


def test_webdav_put_raises_on_error(fake_httpx2):
    fake_httpx2.responses[("PUT", _url("backup.json"))] = _Resp(502)
    with pytest.raises(_FakeHTTPStatusError):
        backup.webdav_put(WCFG, b"x")


def test_webdav_get_404_raises_filenotfound(fake_httpx2):
    with pytest.raises(FileNotFoundError, match="备份"):
        backup.webdav_get(WCFG)


def test_webdav_mkdir_tolerates_existing(fake_httpx2):
    fake_httpx2.responses[("MKCOL", _url())] = _Resp(405)  # 已存在
    backup.webdav_mkdir(WCFG)  # 不抛
    client = fake_httpx2.all_instances[-1]
    assert ("MKCOL", _url()) in client.requests


def test_webdav_test_propfind_ok(fake_httpx2):
    fake_httpx2.responses[("PROPFIND", _url())] = _Resp(207)
    assert "连接成功" in backup.webdav_test(WCFG)


def test_webdav_test_404_dir_means_creatable(fake_httpx2):
    fake_httpx2.responses[("PROPFIND", _url())] = _Resp(404)
    assert "首次备份时自动创建" in backup.webdav_test(WCFG)


def test_webdav_test_405_falls_back_to_get(fake_httpx2):
    fake_httpx2.responses[("PROPFIND", _url())] = _Resp(405)
    fake_httpx2.responses[("GET", _url("backup.json"))] = _Resp(200)
    assert "PROPFIND" in backup.webdav_test(WCFG)


def test_webdav_test_401_surfaces_auth_error(fake_httpx2):
    fake_httpx2.responses[("PROPFIND", _url())] = _Resp(401)
    with pytest.raises(Exception) as ei:
        backup.webdav_test(WCFG)
    assert "应用密码" in backup.friendly_webdav_error(ei.value)


def test_webdav_client_passes_auth_and_proxy(fake_httpx2):
    backup.webdav_test(WCFG, proxy="http://127.0.0.1:7890")
    assert fake_httpx2.all_instances[-1].kwargs["auth"] == ("user@example.com", "app-password")
    assert fake_httpx2.all_instances[-1].kwargs["proxy"] == "http://127.0.0.1:7890"


def test_friendly_webdav_error_timeout():
    assert "超时" in backup.friendly_webdav_error(Exception("Request timed out"))
    assert "连接" in backup.friendly_webdav_error(Exception("Connection refused"))


# ---------------------------------------------------------------- BackupService 集成

@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _spin(qapp, cond, timeout_s=5.0):
    import time

    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return cond()


def test_backup_service_end_to_end(qapp, db, fake_httpx2):
    fake_httpx2.responses[("MKCOL", _url())] = _Resp(201)  # 目录可创建
    database.add_history("hello", "你好", "", db_path=db)
    database.upsert_word("term", note="术语", context="", db_path=db)

    results = []
    service = backup.BackupService()
    service.action_result.connect(lambda action, ok, msg: results.append((action, ok, msg)))
    service.backup_now(WCFG, "")
    assert _spin(qapp, lambda: bool(results)), "backup action never finished"
    assert results[0][0] == "backup" and results[0][1] is True
    assert "1 条历史" in results[0][2] and "1 个生词" in results[0][2]
    # 上传顺序：先建目录再 PUT 文件
    put_client = [c for c in fake_httpx2.all_instances if ("PUT", _url("backup.json")) in c.requests]
    assert put_client, "no PUT performed"
    body = put_client[-1].__dict__["bodies"][0].decode("utf-8")
    assert '"version": 1' in body and '"hello"' in body
