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
