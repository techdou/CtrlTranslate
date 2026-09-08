"""WebDAV 备份/恢复：快照导出导入纯函数 + WebDAV 客户端 + 后台服务。

定位是"单向上传快照的备份/恢复"，不做双向同步；恢复用合并导入
（幂等去重，永不删本地数据）。远端固定 <remote_dir>/backup.json 单文件
（坚果云等网盘网页端自带历史版本可兜底回滚）。

绝不能引导用户把 SQLite db 文件放进同步盘目录——运行中持续写库，
同步盘上传半成品 db 即损坏；备份必须是程序导出的 JSON 快照。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading

from PySide6.QtCore import QObject, Signal

from app import __version__
from app.db import database

logger = logging.getLogger("ctrltrans.backup")

SNAPSHOT_VERSION = 1
REMOTE_FILENAME = "backup.json"

WebDavConfig = tuple[str, str, str, str]  # (url, username, password, remote_dir)


# ---------------------------------------------------------------- 快照（纯函数）

def build_snapshot(db_path=None) -> dict:
    """从本地库导出备份快照（历史 + 生词本；翻译缓存不备份——可重建）。"""
    return {
        "version": SNAPSHOT_VERSION,
        "exported_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": __version__,
        "history": database.export_all_history(db_path),
        "vocabulary": database.list_words(db_path=db_path),
    }


def snapshot_to_json(snap: dict) -> str:
    return json.dumps(snap, ensure_ascii=False)


def snapshot_from_json(text: str) -> dict:
    snap = json.loads(text)
    if not isinstance(snap, dict) or snap.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"无法识别的备份文件（version={snap.get('version') if isinstance(snap, dict) else '?'}）")
    return snap


def apply_snapshot(snap: dict, db_path=None) -> dict:
    """合并导入快照到本地库，返回统计。幂等：同一快照恢复两次不会翻倍。"""
    if not isinstance(snap, dict) or snap.get("version") != SNAPSHOT_VERSION:
        raise ValueError("无法识别的备份文件")
    imported_h, skipped_h = database.import_history(snap.get("history") or [], db_path)
    imported_v = database.import_vocabulary(snap.get("vocabulary") or [], db_path)
    return {
        "history_imported": imported_h,
        "history_skipped": skipped_h,
        "vocabulary_imported": imported_v,
        "exported_at": snap.get("exported_at", ""),
    }


def snapshot_summary(snap: dict) -> str:
    """恢复确认框用的摘要文案。"""
    return (
        f"备份时间 {snap.get('exported_at', '?')}（v{snap.get('app_version', '?')} 制作）："
        f"{len(snap.get('history') or [])} 条历史、{len(snap.get('vocabulary') or [])} 个生词。"
    )


# ---------------------------------------------------------------- WebDAV 客户端

def _remote_url(wcfg: WebDavConfig, filename: str = "") -> str:
    url, _user, _pw, remote_dir = wcfg
    base = url.rstrip("/")
    directory = remote_dir.strip("/")
    return f"{base}/{directory}/{filename}" if filename else f"{base}/{directory}"


def _dav_client(wcfg: WebDavConfig, timeout: float, proxy: str = ""):
    import httpx2

    url, user, pw, _dir = wcfg
    kwargs: dict = {"auth": (user, pw), "timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx2.Client(**kwargs)


def webdav_test(wcfg: WebDavConfig, proxy: str = "", timeout: float = 15) -> str:
    """探测连接与认证。PROPFIND 远端目录；405（服务器不许 PROPFIND）退回 GET
    文件本身——200/404 都能证明地址与账号密码正确。成功返回描述文案。"""
    with _dav_client(wcfg, timeout, proxy) as client:
        r = client.request("PROPFIND", _remote_url(wcfg), headers={"Depth": "0"})
        if r.status_code in (200, 207):
            return "连接成功，远端目录可访问"
        if r.status_code == 404:
            return "连接成功（远端目录尚不存在，首次备份时自动创建）"
        if r.status_code == 405:
            r2 = client.get(_remote_url(wcfg, REMOTE_FILENAME))
            if r2.status_code in (200, 404):
                return "连接成功（服务器不支持 PROPFIND，已用文件探测验证）"
            raise _status_error(r2, "GET")
        raise _status_error(r, "PROPFIND")


def _status_error(r, op: str) -> Exception:
    """把非 2xx 响应归一成带 status_code 属性的异常，供文案分类。"""
    from httpx2 import HTTPStatusError

    try:
        r.raise_for_status()
    except HTTPStatusError as e:
        return e
    return RuntimeError(f"{op} 失败：HTTP {r.status_code}")


def webdav_mkdir(wcfg: WebDavConfig, proxy: str = "", timeout: float = 15) -> None:
    """确保远端目录存在（MKCOL）；405 = 已存在，视为成功。"""
    _client = _dav_client(wcfg, timeout, proxy)
    with _client:
        r = _client.request("MKCOL", _remote_url(wcfg))
        if r.status_code not in (200, 201, 204, 405):
            raise _status_error(r, "MKCOL")


def webdav_put(wcfg: WebDavConfig, data: bytes, proxy: str = "", timeout: float = 30) -> None:
    _client = _dav_client(wcfg, timeout, proxy)
    with _client:
        r = _client.put(_remote_url(wcfg, REMOTE_FILENAME), content=data)
        if r.status_code not in (200, 201, 204):
            raise _status_error(r, "PUT")


def webdav_get(wcfg: WebDavConfig, proxy: str = "", timeout: float = 30) -> bytes:
    _client = _dav_client(wcfg, timeout, proxy)
    with _client:
        r = _client.get(_remote_url(wcfg, REMOTE_FILENAME))
        if r.status_code == 404:
            raise FileNotFoundError("远端没有备份文件——先点「立即备份」上传一次")
        if r.status_code != 200:
            raise _status_error(r, "GET")
        return r.content


def friendly_webdav_error(e: Exception) -> str:
    """分类中文文案，仿 translator._friendly_error。"""
    code = getattr(getattr(e, "response", None), "status_code", None)
    msg = str(e).replace("\n", " ")[:200]
    if code == 401:
        return "认证失败（401）：账号或密码不对——坚果云请填「应用密码」，不是登录密码"
    if code == 403:
        return "无权限访问该目录（403）：检查账号权限或换个远端目录名"
    if code == 404:
        return "远端路径不存在（404）：检查地址与远端目录名"
    if code == 409:
        return "远端目录不存在且创建失败（409）：检查地址路径"
    low = msg.lower()
    if "timeout" in low or "timed out" in low:
        return "连接超时：检查地址是否正确、网络是否可达（坚果云国内直连，一般无需代理）"
    if "connect" in low or "resolve" in low or "name" in low and "service" in low:
        return "无法连接到 WebDAV 服务器：地址需以 http(s):// 开头，注意网络连通性"
    return f"备份失败：{msg[:160]}"


# ---------------------------------------------------------------- 后台服务

class BackupService(QObject):
    """后台线程执行备份动作，结果经信号回主线程（仿 UpdateChecker/Translator）。
    服务由 main 持有、生命周期独立于设置对话框。"""

    action_result = Signal(str, bool, str)  # (动作名, 是否成功, 消息)
    snapshot_ready = Signal(dict)           # fetch_snapshot 成功拿到远端快照

    def test_now(self, wcfg: WebDavConfig, proxy: str = "") -> None:
        threading.Thread(target=self._run_action, args=("test", wcfg, proxy), daemon=True).start()

    def backup_now(self, wcfg: WebDavConfig, proxy: str = "") -> None:
        threading.Thread(target=self._run_action, args=("backup", wcfg, proxy), daemon=True).start()

    def fetch_snapshot(self, wcfg: WebDavConfig, proxy: str = "") -> None:
        threading.Thread(target=self._run_action, args=("restore_fetch", wcfg, proxy), daemon=True).start()

    # ---- 内部 ----

    def _run_action(self, action: str, wcfg: WebDavConfig, proxy: str) -> None:
        try:
            if action == "test":
                self.action_result.emit(action, True, webdav_test(wcfg, proxy))
            elif action == "backup":
                snap = build_snapshot()
                data = snapshot_to_json(snap).encode("utf-8")
                webdav_mkdir(wcfg, proxy)
                webdav_put(wcfg, data, proxy)
                logger.info("backup uploaded: %d history, %d words",
                            len(snap["history"]), len(snap["vocabulary"]))
                self.action_result.emit(
                    action, True,
                    f"已备份 {len(snap['history'])} 条历史、{len(snap['vocabulary'])} 个生词"
                    f"（{snap['exported_at']}）",
                )
            elif action == "restore_fetch":
                data = webdav_get(wcfg, proxy)
                snap = snapshot_from_json(data.decode("utf-8"))
                self.snapshot_ready.emit(snap)
        except FileNotFoundError as e:
            self.action_result.emit(action, False, str(e))
        except Exception as e:
            logger.warning("%s failed: %s", action, e)
            self.action_result.emit(action, False, friendly_webdav_error(e))
