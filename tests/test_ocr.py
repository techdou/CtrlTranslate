"""OCR 截图翻译测试：两阶段流水线（视觉模型纯识别 → 文本链翻译）、
fake openai 流式集成、缓存/fallback 行为。"""

import sys
import time
import types
from unittest import mock

import pytest

from app.core.translator import (
    OCR_SYSTEM_PROMPT,
    Translator,
    build_ocr_messages,
)

CFG = {
    "provider": {"name": "zhipu", "base_url": "https://x/v1", "api_key": "sk-test", "model": "m1"},
    "translate": {"mode": "study", "timeout_s": 10},
    "ocr": {"enabled": True, "model": "glm-4v-flash", "hotkey": "alt+q"},
}

CFG_WITH_FB = {
    **CFG,
    "provider": {
        **CFG["provider"],
        "fallback": {"base_url": "https://fb/v1", "api_key": "k2", "model": "m2"},
    },
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

def test_build_ocr_messages_structure():
    msgs = build_ocr_messages("QUJD")
    assert msgs[0] == {"role": "system", "content": OCR_SYSTEM_PROMPT}
    content = msgs[1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["type"] == "text"
    img = content[1]
    assert img["type"] == "image_url"
    assert img["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_ocr_prompt_is_recognition_only():
    """识别指令不得要求翻译——识别/翻译混在一条指令里，OCR 专精模型会只回
    识别文本不翻译（真机实测，两阶段拆分的根因）。"""
    assert "不要翻译" in OCR_SYSTEM_PROMPT
    assert "识别" in OCR_SYSTEM_PROMPT


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
    """fake OpenAI：每次 _stream_once 都会 _make_client 新建客户端，
    因此聚合全部客户端的调用记录（holder["clients"]）；holder["client"]
    保留为最后一个（单请求场景的旧习惯用法）。"""
    holder = {"clients": []}
    mod = types.ModuleType("openai")

    def make(**kw):
        client = _Client(factory)
        holder["clients"].append(client)
        holder["client"] = client
        return client

    mod.OpenAI = make
    sys.modules["openai"] = mod
    return mock.patch.dict(sys.modules, {"openai": mod}), holder


def _all_calls(holder) -> list[dict]:
    return [c for cl in holder["clients"] for c in cl.chat.completions.calls]


def test_translate_image_two_stage_uses_right_models(qapp):
    """两阶段：第一发走 ocr.model 纯识别，第二发走 provider.model 翻译，
    finished 给出的是译文而非识别原文。"""
    patcher, holder = _install_fake_openai(lambda: _Stream([_Event("识"), _Event("别")]))
    with patcher:
            tr = Translator(lambda: CFG)
            final, err, ocr_texts = [], [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.ocr_text_ready.connect(lambda s, tid: ocr_texts.append(s))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(final) or bool(err)), f"no result; err={err}"
            assert final == ["识别"] and err == []
            assert ocr_texts == ["识别"]  # 第一阶段识别原文也回报（历史归档用）
            calls = _all_calls(holder)
            assert len(calls) == 2
            assert calls[0]["model"] == "glm-4v-flash"  # 阶段一：ocr.model
            assert calls[0]["messages"][0]["content"] == OCR_SYSTEM_PROMPT
            user = calls[0]["messages"][1]["content"]
            assert user[1]["image_url"]["url"].endswith("QUJD")
            assert calls[1]["model"] == "m1"  # 阶段二：provider.model 文本翻译
            assert calls[1]["stream"] is True


def test_translate_image_caches_translation_stage(qapp):
    """图片本身不缓存（每次截图不同），但识别出的文本走翻译缓存——
    同一段文字重复截图，第二只发识别请求、翻译直接命中。"""
    patcher, holder = _install_fake_openai(lambda: _Stream([_Event("流式")]))
    with patcher:
            tr = Translator(lambda: CFG)
            final, err = [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(final))
            tr.translate_image("QUJD")  # 同图重截
            assert _spin(qapp, lambda: len(final) >= 2)
            assert err == []
            assert len(_all_calls(holder)) == 3  # 2×(识别+翻译) - 1（第二次翻译命中缓存）


def test_translate_image_empty_ocr_reports_error(qapp):
    patcher, _holder = _install_fake_openai(lambda: _Stream([_Event("")]))
    with patcher:
            tr = Translator(lambda: CFG)
            final, err = [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(err))
            assert "识别" in err[0]
            assert final == []


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


def test_translate_image_stage1_failure_does_not_fallback(qapp):
    """识别阶段失败直接报错——备用服务是文本模型，接图必错，不做 fallback 重试。"""

    def exploding():
        raise RuntimeError("connection error")

    patcher, holder = _install_fake_openai(exploding)
    with patcher:
            tr = Translator(lambda: CFG_WITH_FB)
            final, err, fb = [], [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.fallback_started.connect(lambda tid: fb.append(tid))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(err))
            assert final == [] and fb == []  # 没走 fallback
            assert len(_all_calls(holder)) == 1  # 只请求了一次


def test_translate_image_stage2_failure_falls_back(qapp):
    """两阶段的收益之一：翻译阶段是纯文本请求，主服务失败可由备用服务接管。"""
    outcomes = [
        lambda: _Stream([_Event("原文")]),     # 阶段一：识别成功
        lambda: (_ for _ in ()).throw(RuntimeError("connection error")),  # 主服务翻译失败
        lambda: _Stream([_Event("译"), _Event("文")]),  # 备用服务翻译成功
    ]
    patcher, holder = _install_fake_openai(lambda: outcomes.pop(0)())
    with patcher:
            tr = Translator(lambda: CFG_WITH_FB)
            final, err, fb = [], [], []
            tr.finished.connect(lambda s, tid: final.append(s))
            tr.failed.connect(lambda s, tid: err.append(s))
            tr.fallback_started.connect(lambda tid: fb.append(tid))
            tr.translate_image("QUJD")
            assert _spin(qapp, lambda: bool(final) or bool(err))
            assert final == ["译文"] and err == []
            assert len(fb) == 1
            calls = _all_calls(holder)
            assert calls[2]["model"] == "m2"  # 第三发 = 备用服务
