"""Translator 测试：prompt 构建、错误文案、fake openai 流式集成（含竞态取消）。"""

import sys
import time
import types
from unittest import mock

import pytest

from app.core.translator import CONCISE_SYSTEM_PROMPT, STUDY_SYSTEM_PROMPT, Translator, build_messages

CFG = {
    "provider": {"name": "zhipu", "base_url": "https://x/v1", "api_key": "sk-test", "model": "m1"},
    "translate": {"mode": "study", "timeout_s": 10},
}


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """把翻译缓存隔离到内存 dict——防止测试写入真实 ~/.ctrltrans/data.db。"""
    from app.db import database

    store: dict[str, str] = {}
    monkeypatch.setattr(database, "get_cached_translation", lambda key: store.get(key))
    monkeypatch.setattr(database, "put_cached_translation",
                        lambda key, src, res: store.__setitem__(key, res))
    return store


# ---------------------------------------------------------------- prompt

def test_build_messages_study_mode():
    msgs = build_messages({"translate": {"mode": "study"}}, "hello world")
    assert msgs[0]["content"] == STUDY_SYSTEM_PROMPT
    assert msgs[1] == {"role": "user", "content": "hello world"}


def test_build_messages_concise_mode():
    msgs = build_messages({"translate": {"mode": "concise"}}, "hi")
    assert msgs[0]["content"] == CONCISE_SYSTEM_PROMPT


def test_build_messages_custom_prompt_overrides():
    msgs = build_messages({"translate": {"custom_prompt": "只翻译为法语：{text}"}}, "hi")
    assert msgs[0]["content"] == "只翻译为法语：{text}"


def test_friendly_error_classification():
    assert "401" in Translator._friendly_error(_exc("AuthenticationError", "401", code=401), "")
    assert "429" in Translator._friendly_error(_exc("RateLimit", "429", code=429), "")
    assert "超时" in Translator._friendly_error(_exc("APITimeoutError", "Request timed out"), "")
    assert "无法连接" in Translator._friendly_error(_exc("APIConnectionError", "connection error"), "https://x")
    assert "翻译失败" in Translator._friendly_error(_exc("Weird", "boom"), "")


def _exc(name: str, msg: str, code: int | None = None):
    cls = type(name, (Exception,), {})
    e = cls(msg)
    if code is not None:
        e.status_code = code
    return e


# ---------------------------------------------------------------- fake openai 流式集成

class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Event:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Stream:
    def __init__(self, events, close_hook=None):
        self._it = iter(events)
        self.closed = False
        self._close_hook = close_hook

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return next(self._it)

    def close(self):
        self.closed = True
        if self._close_hook:
            self._close_hook()


class _Completions:
    def __init__(self, stream_factory):
        self._factory = stream_factory
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._factory()


class _Client:
    def __init__(self, stream_factory):
        self.chat = types.SimpleNamespace(completions=_Completions(stream_factory))


@pytest.fixture()
def qapp():
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _spin(qapp, cond, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return cond()


def _install_fake_openai(stream_factory):
    mod = types.ModuleType("openai")
    mod.OpenAI = lambda **kw: _Client(stream_factory)
    sys.modules["openai"] = mod
    return mock.patch.dict(sys.modules, {"openai": mod})


def test_streaming_translates_and_emits_chunks(qapp):
    chunks = ["你", "好", "，", "世界"]
    with _install_fake_openai(lambda: _Stream([_Event(c) for c in chunks])):
        tr = Translator(lambda: CFG)
        got, final, err = [], [], []
        tr.chunk.connect(lambda s, tid: got.append(s))
        tr.finished.connect(lambda s, tid: final.append(s))
        tr.failed.connect(lambda s, tid: err.append(s))
        tr.translate("hello")
        assert _spin(qapp, lambda: bool(final)), f"no finish; err={err}"
        assert final[0] == "你好，世界"
        assert "".join(got) == "你好，世界"
        assert err == []


def test_new_request_cancels_old(qapp):
    """旧请求的 chunk 在新请求发出后应被丢弃，旧流被 close。"""
    gate = {"release": False}
    closed = {"old": False}

    def old_factory():
        def on_close():
            closed["old"] = True
        events = [_Event("旧1")]

        class _GatedStream(_Stream):
            def __next__(self):
                while not gate["release"]:
                    time.sleep(0.005)  # 阻塞在事件边界，模拟慢流
                return super().__next__()

        s = _GatedStream(events + [_Event("旧2")], on_close)
        return s

    def new_factory():
        return _Stream([_Event("新")])

    factories = {"old": old_factory, "new": new_factory, "seq": 0}

    def client_factory(**kw):
        factories["seq"] += 1
        return _Client(lambda: (old_factory if factories["seq"] == 1 else new_factory)())

    mod = types.ModuleType("openai")
    mod.OpenAI = client_factory
    with mock.patch.dict(sys.modules, {"openai": mod}):
        tr = Translator(lambda: CFG)
        seen = []
        tr.chunk.connect(lambda s, tid: seen.append((s, tid)))
        tr.finished.connect(lambda s, tid: seen.append((("FIN", s), tid)))

        t1 = tr.translate("old")
        _spin(qapp, lambda: any(s == "旧1" for s, _ in seen))
        t2 = tr.translate("new")  # 作废 t1
        _spin(qapp, lambda: any(s == "新" for s, _ in seen))
        gate["release"] = True

        assert t2 != t1
        # t2 的结果完整到达；t1 不会再有新事件
        assert any(s == "新" for s, _ in seen)
        _spin(qapp, lambda: False, timeout_s=0.3)  # 给旧线程时间投递（如果有 bug）
        t1_events = [x for x in seen if x[1] == t1]
        assert all(s == "旧1" for s, _ in t1_events), f"old task leaked: {t1_events}"


def test_missing_api_key_fails_fast(qapp):
    bad_cfg = {"provider": {"name": "zhipu", "base_url": "https://x", "api_key": "", "model": "m"},
               "translate": {}}
    tr = Translator(lambda: bad_cfg)
    err = []
    tr.failed.connect(lambda s, tid: err.append(s))
    tr.translate("hi")
    assert _spin(qapp, lambda: bool(err))
    assert "API Key" in err[0]


# ---------------------------------------------------------------- fallback 主备切换

CFG_FB = {
    "provider": {
        "name": "zhipu", "base_url": "https://main/v1", "api_key": "sk-main", "model": "m1",
        "fallback": {"base_url": "https://fb/v1", "model": "m2", "api_key": "sk-fb"},
    },
    "translate": {"mode": "concise", "timeout_s": 5},
}


def _conn_error(msg="connection error"):
    cls = type("APIConnectionError", (Exception,), {})
    return cls(msg)


def _raise(e: Exception):
    raise e


def _install_routed_openai(routes: dict):
    """routes: {base_url: callable} —— create 时调用，返回 _Stream 或抛异常。"""

    def openai_ctor(**kw):
        behavior = routes[kw.get("base_url")]

        class _Completions:
            def create(self, **_kw):
                return behavior()

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

    mod = types.ModuleType("openai")
    mod.OpenAI = openai_ctor
    return mock.patch.dict(sys.modules, {"openai": mod})


def test_fallback_used_when_primary_fails(qapp):
    routes = {
        "https://main/v1": lambda: _raise(_conn_error()),
        "https://fb/v1": lambda: _Stream([_Event("备"), _Event("用")]),
    }
    with _install_routed_openai(routes):
        tr = Translator(lambda: CFG_FB)
        got, final, err, fb = [], [], [], []
        tr.chunk.connect(lambda s, tid: got.append(s))
        tr.finished.connect(lambda s, tid: final.append(s))
        tr.failed.connect(lambda s, tid: err.append(s))
        tr.fallback_started.connect(lambda tid: fb.append(tid))
        tr.translate("hello")
        assert _spin(qapp, lambda: bool(final)), f"no finish; err={err}"
        assert final[0] == "备用"
        assert "".join(got) == "备用"  # 主服务半截输出不混入
        assert len(fb) == 1
        assert err == []


def test_no_fallback_reports_primary_error(qapp):
    cfg = {"provider": dict(CFG_FB["provider"], fallback={}),
           "translate": CFG_FB["translate"]}
    routes = {"https://main/v1": lambda: _raise(_conn_error())}
    with _install_routed_openai(routes):
        tr = Translator(lambda: cfg)
        final, err, fb = [], [], []
        tr.finished.connect(lambda s, tid: final.append(s))
        tr.failed.connect(lambda s, tid: err.append(s))
        tr.fallback_started.connect(lambda tid: fb.append(tid))
        tr.translate("hello")
        assert _spin(qapp, lambda: bool(err))
        assert "无法连接" in err[0]
        assert final == [] and fb == []


def test_fallback_also_fails_reports_both(qapp):
    routes = {
        "https://main/v1": lambda: _raise(_conn_error("main down")),
        "https://fb/v1": lambda: _raise(_conn_error("fb down")),
    }
    with _install_routed_openai(routes):
        tr = Translator(lambda: CFG_FB)
        final, err = [], []
        tr.finished.connect(lambda s, tid: final.append(s))
        tr.failed.connect(lambda s, tid: err.append(s))
        tr.translate("hello")
        assert _spin(qapp, lambda: bool(err))
        assert "主服务失败" in err[0] and "备用服务也失败" in err[0]
        assert final == []


def test_incomplete_fallback_config_ignored(qapp):
    """备用三件套缺一（如没填 Key）→ 视为未配置，只报主服务错误。"""
    cfg = {"provider": dict(CFG_FB["provider"],
                            fallback={"base_url": "https://fb/v1", "model": "m2", "api_key": ""}),
           "translate": CFG_FB["translate"]}
    calls = []

    def fb_should_not_be_called():
        calls.append("fb")
        return _Stream([_Event("x")])

    routes = {
        "https://main/v1": lambda: _raise(_conn_error()),
        "https://fb/v1": fb_should_not_be_called,
    }
    with _install_routed_openai(routes):
        tr = Translator(lambda: cfg)
        err, fb = [], []
        tr.failed.connect(lambda s, tid: err.append(s))
        tr.fallback_started.connect(lambda tid: fb.append(tid))
        tr.translate("hello")
        assert _spin(qapp, lambda: bool(err))
        assert "无法连接" in err[0]
        assert fb == [] and calls == []


# ---------------------------------------------------------------- test_connection（设置页）

class _TestResp:
    """非流式 create 的最小返回结构（resp.choices[0].message.content）。"""

    def __init__(self, content):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


def test_test_connection_uses_explicit_endpoint_not_saved_cfg(qapp):
    """契约：显式 endpoint（设置页表单当前值）优先于已保存配置。"""
    seen = []

    def openai_ctor(**kw):
        seen.append(kw.get("base_url"))

        class _Completions:
            def create(self, **_kw):
                return _TestResp("ok")

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

    mod = types.ModuleType("openai")
    mod.OpenAI = openai_ctor
    with mock.patch.dict(sys.modules, {"openai": mod}):
        tr = Translator(lambda: CFG)  # 已保存 cfg 指向 https://x/v1
        results = []
        tr.test_connection(
            lambda m: results.append(("ok", m)),
            lambda m: results.append(("fail", m)),
            endpoint=("https://form-value/v1", "sk-form", "m-form"),
        )
        assert _spin(qapp, lambda: bool(results)), "no test result"
        assert seen == ["https://form-value/v1"]  # 用表单值，不是已保存的 https://x/v1
        assert results[0][0] == "ok" and "ok" in results[0][1]


def test_test_connection_failure_via_signal(qapp):
    """失败路径：错误文案经 test_result 信号投递，on_fail 在主线程收到。"""

    def openai_ctor(**kw):
        def create(**_kw):
            raise _conn_error("connection error")

        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    mod = types.ModuleType("openai")
    mod.OpenAI = openai_ctor
    with mock.patch.dict(sys.modules, {"openai": mod}):
        tr = Translator(lambda: CFG)
        results = []
        tr.test_connection(
            lambda m: results.append(("ok", m)),
            lambda m: results.append(("fail", m)),
            endpoint=("https://x/v1", "sk", "m"),
        )
        assert _spin(qapp, lambda: bool(results))
        assert results[0][0] == "fail"
        assert "无法连接" in results[0][1]


def test_dispatch_test_result_invokes_callback():
    calls = []
    Translator._dispatch_test_result(calls.append, "msg", True)
    assert calls == ["msg"]


# ---------------------------------------------------------------- 翻译缓存

def test_translation_cache_hit_and_force_refresh(qapp, _isolate_cache):
    """同文本第二次翻译命中缓存不发请求；use_cache=False 绕过强制重译。"""
    calls = {"n": 0}

    def openai_ctor(**kw):
        calls["n"] += 1

        class _Completions:
            def create(self, **_kw):
                return _Stream([_Event("译")])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

    mod = types.ModuleType("openai")
    mod.OpenAI = openai_ctor
    with mock.patch.dict(sys.modules, {"openai": mod}):
        tr = Translator(lambda: CFG)
        finals = []
        tr.finished.connect(lambda s, tid: finals.append(s))

        tr.translate("hello")  # 第一次：真请求并写缓存
        assert _spin(qapp, lambda: bool(finals))
        assert calls["n"] == 1 and finals[0] == "译"

        finals.clear()
        tr.translate("hello")  # 第二次：命中缓存
        assert _spin(qapp, lambda: bool(finals))
        assert calls["n"] == 1

        finals.clear()
        tr.translate("hello", use_cache=False)  # 重试路径：绕过
        assert _spin(qapp, lambda: bool(finals))
        assert calls["n"] == 2


def test_cache_key_changes_with_translation_settings():
    from app.core.translator import cache_key

    base = {"provider": {"model": "m1"}, "translate": {"mode": "study", "custom_prompt": ""}}
    assert cache_key(base, "txt") == cache_key(dict(base), "txt")
    assert cache_key(base, "txt") != cache_key(base, "txt2")
    other_model = {"provider": {"model": "m2"}, "translate": base["translate"]}
    assert cache_key(base, "txt") != cache_key(other_model, "txt")
    other_mode = {"provider": {"model": "m1"}, "translate": {"mode": "concise", "custom_prompt": ""}}
    assert cache_key(base, "txt") != cache_key(other_mode, "txt")
