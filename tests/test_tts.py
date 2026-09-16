"""TTS 纯函数单测：语速换算 + 自定义引擎请求组装 + 语言检测。"""

import pytest

from app.core.tts import _custom_request, _detect_lang, _rate_to_speed
from app.config import DATA_DIR

CUSTOM = {
    "base_url": "https://tts.example/v1",
    "model": "some-tts-model",
    "voice": "alloy",
    "api_key": "sk-tts",
}


# ---------------------------------------------------------------- 语速换算

def test_rate_to_speed_basic():
    assert _rate_to_speed("+0%") == 1.0
    assert _rate_to_speed("+15%") == 1.15
    assert _rate_to_speed("-50%") == 0.5


def test_rate_to_speed_clamped_and_invalid():
    assert _rate_to_speed("+1000%") == 4.0   # 上限截断
    assert _rate_to_speed("-100%") == 0.25   # 下限截断
    assert _rate_to_speed("bad") == 1.0
    assert _rate_to_speed(None) == 1.0


# ---------------------------------------------------------------- 语言检测

def test_detect_lang():
    assert _detect_lang("你好，今天天气不错") == "zh"
    assert _detect_lang("Hello, how are you today") == "en"
    assert _detect_lang("神经网络 neural network 的训练") == "zh"   # 混排含汉字即中文
    assert _detect_lang("attention is all you need") == "en"
    assert _detect_lang("") == "en"  # 空文本按英文，调用方已挡掉空串


# ---------------------------------------------------------------- 请求组装

def test_custom_request_full_config():
    req = _custom_request(CUSTOM, "+0%", "hello")
    assert req is not None
    (base_url, api_key, kwargs), cache = req
    assert base_url == CUSTOM["base_url"]
    assert api_key == CUSTOM["api_key"]
    assert kwargs == {
        "model": CUSTOM["model"], "input": "hello",
        "response_format": "mp3", "voice": "alloy",
    }  # speed==1.0 不传
    assert cache.parent == DATA_DIR / "tts_cache" and cache.suffix == ".mp3"


def test_custom_request_speed_and_voice_optional():
    req = _custom_request(dict(CUSTOM, voice=""), "+30%", "hi")
    (base_url, _key, kwargs), _cache = req
    assert "voice" not in kwargs        # 音色留空不传，走服务端默认
    assert kwargs["speed"] == 1.3      # 语速非默认才传


def test_custom_request_incomplete_returns_none():
    for missing in ("base_url", "model", "api_key"):
        c = dict(CUSTOM)
        c[missing] = ""
        assert _custom_request(c, "+0%", "hi") is None
    assert _custom_request({}, "+0%", "hi") is None


def test_custom_request_cache_key_separates_inputs():
    r1 = _custom_request(CUSTOM, "+0%", "a")
    r2 = _custom_request(CUSTOM, "+0%", "b")
    r3 = _custom_request(dict(CUSTOM, model="other"), "+0%", "a")
    assert r1[1] != r2[1]   # 文本不同
    assert r1[1] != r3[1]   # 模型不同


# ---------------------------------------------------------------- SAPI 中断旗标

@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_stop_sets_sapi_stop_flag(qapp):
    """stop() 只置旗标（worker 线程内消费）——主线程不再跨套间调 COM Skip。"""
    from app.core.tts import TTSService

    svc = TTSService(lambda: {})
    assert svc._sapi_stop is False
    svc.stop()
    assert svc._sapi_stop is True


def test_speak_clears_stale_stop_flag(qapp):
    """新播报不被上一轮的中断请求立即掐断：speak 先 stop 再清旗标。"""
    from app.core.tts import TTSService

    svc = TTSService(lambda: {"tts": {"enabled": False}})   # disabled：不起线程
    svc._sapi_stop = True
    svc.speak("hello")
    assert svc._sapi_stop is False


# ---------------------------------------------------------------- 缓存 LRU 清理

def test_cleanup_tts_cache_deletes_oldest_beyond_cap(monkeypatch, tmp_path):
    """目录总量超上限时按 mtime 从旧到新删，最新文件保留。"""
    from app.core import tts as tts_mod

    monkeypatch.setattr(tts_mod, "TTS_CACHE_DIR", tmp_path)
    cap = 10 * 1024
    # 三个 4KB 文件共 12KB > 10KB：最旧的应被删（mtime 逐个 +100s）
    for i in range(3):
        p = tmp_path / f"a{i}.mp3"
        p.write_bytes(b"x" * 4096)
        stamp = 1_000_000_000 + i * 100
        import os

        os.utime(p, (stamp, stamp))

    freed = tts_mod.cleanup_tts_cache(max_bytes=cap)
    remaining = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert remaining == ["a1.mp3", "a2.mp3"]     # 最旧的 a0 被删
    assert freed == 4096
    # 未超限时零动作
    assert tts_mod.cleanup_tts_cache(max_bytes=cap) == 0


def test_cleanup_tts_cache_under_cap_noop(monkeypatch, tmp_path):
    from app.core import tts as tts_mod

    monkeypatch.setattr(tts_mod, "TTS_CACHE_DIR", tmp_path)
    (tmp_path / "only.mp3").write_bytes(b"x" * 100)
    assert tts_mod.cleanup_tts_cache(max_bytes=1024) == 0
    assert (tmp_path / "only.mp3").exists()
    # 目录不存在也不炸（glob 空目录）
    monkeypatch.setattr(tts_mod, "TTS_CACHE_DIR", tmp_path / "nope")
    assert tts_mod.cleanup_tts_cache(max_bytes=1024) == 0
