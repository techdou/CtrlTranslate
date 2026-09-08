"""OCR 截图翻译测试：vision 消息构建、fake openai 流式集成、缓存/fallback 排除。"""

import sys
import time
import types
from unittest import mock

import pytest

from app.core.translator import (
    VISION_SYSTEM_PROMPT,
    Translator,
    build_vision_messages,
)

CFG = {
    "provider": {"name": "zhipu", "base_url": "https://x/v1", "api_key": "sk-test", "model": "m1"},
    "translate": {"mode": "study", "timeout_s": 10},
    "ocr": {"enabled": True, "model": "glm-4v-flash", "hotkey": "alt+q"},
}


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    from app.db import database

    store: dict[str, str] = {}
    monkeypatch.setattr(database, "get_cached_translation", lambda key: store.get(key))
    monkeypatch.setattr(database, "put_cached_translation",
                        lambda key, src, res: store.__setitem__(key, res))
    return store


# ---------------------------------------------------------------- 消息构建

def test_build_vision_messages_structure():
    msgs = build_vision_messages("QUJD")
    assert msgs[0] == {"role": "system", "content": VISION_SYSTEM_PROMPT}
    content = msgs[1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["type"] == "text"
    img = content[1]
    assert img["type"] == "image_url"
    assert img["image_url"]["url"] == "data:image/png;base64,QUJD"


# ---------------------------------------------------------------- fake openai 集成

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
    def __init__(self, events):
        self._it = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return next(self._it)

    def close(self):
        self.closed = True


class _Completions:
    def __init__(self, factory):
        self._factory = factory
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._factory()


class _Client:
    def __init__(self, factory):
        self.chat = types.SimpleNamespace(completions=_Completions(factory))


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _spin(qapp, cond, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return cond()


def _install_fake_openai(factory):
    holder = {}
    mod = types.ModuleType("openai")

    def make(**kw):
        client = _Client(factory)
        holder["client"] = client
        return client

    mod.OpenAI = make
    sys.modules["openai"] = mod
    return mock.patch.dict(sys.modules, {"openai": mod}), holder


def test_translate_image_streams_and_uses_vision_model(qapp):
    patcher, holder = _install_fake_openai(lambda: _Stream([_Event("识"), _Event("别")]))
    with patcher:
            tr = Translator(lambda: CFG)
            final, err = [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(final) or bool(err)), f"no result; err={err}"
            assert final == ["识别"] and err == []
            call = holder["client"].chat.completions.calls[0]
            assert call["model"] == "glm-4v-flash"  # ocr.model，不是 provider.model
            assert call["stream"] is True
            user = call["messages"][1]["content"]
            assert user[1]["image_url"]["url"].endswith("QUJD")


def test_translate_image_ignores_cache(qapp):
    """截图内容每次都不同，绝不能命中/写入文本翻译缓存。"""
    store_snapshot = {}

    from app.db import database

    def fake_get(key):
        store_snapshot["get_called"] = True
        return "不该出现的缓存结果"

    with mock.patch.object(database, "get_cached_translation", fake_get):
        patcher, _holder = _install_fake_openai(lambda: _Stream([_Event("流式")]))
        with patcher:
                tr = Translator(lambda: CFG)
                final, err = [], []
                tr.finished.connect(lambda s, tid: final.append(s))
                tr.failed.connect(lambda s, tid: err.append(s))
                tr.translate_image("QUJD")
                assert _spin(qapp, lambda: bool(final) or bool(err))
                assert final == ["流式"]
                assert not store_snapshot.get("get_called")  # 根本没查缓存


def test_translate_image_no_model_reports_config_error(qapp):
    cfg = {**CFG, "ocr": {**CFG["ocr"], "model": ""}}
    tr = Translator(lambda: cfg)
    final, err = [], []
    tr.finished.connect(lambda s, tid: final.append(s))
    tr.failed.connect(lambda s, tid: err.append(s))
    tr.translate_image("QUJD")
    assert _spin(qapp, lambda: bool(err))
    assert "OCR" in err[0] or "识别模型" in err[0]
    assert final == []


def test_translate_image_failure_does_not_fallback(qapp):
    """OCR 失败直接报错——备用服务是文本模型，接图必错，不做 fallback 重试。"""

    def exploding():
        raise RuntimeError("connection error")

    patcher, holder = _install_fake_openai(exploding)
    with patcher:
            tr = Translator(lambda: CFG)
            final, err, fb = [], [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.fallback_started.connect(lambda tid: fb.append(tid))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(err))
            assert final == [] and fb == []  # 没走 fallback
            assert len(holder["client"].chat.completions.calls) == 1  # 只请求了一次
