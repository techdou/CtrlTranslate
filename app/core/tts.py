"""TTS 播报：edge-tts 在线合成（音质好）→ Windows SAPI 离线兜底，自动降级。

引擎三种：edge-tts（免费在线）、custom（OpenAI 兼容 /audio/speech，自定义
API 地址 + 模型 + 音色）、SAPI（Windows 系统语音）。auto = edge 失败转 SAPI；
custom 失败同样转 SAPI。

线程模型：合成在后台线程（asyncio.run 独立事件循环）；播放用 QMediaPlayer，
必须主线程操作，经 play_file 信号投递。合成结果按内容哈希缓存 mp3，
重复播报零等待零请求。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from app.config import DATA_DIR, get_proxy

logger = logging.getLogger("ctrltrans.tts")

TTS_CACHE_DIR = DATA_DIR / "tts_cache"
SAPI_TEXT_LIMIT = 800
TTS_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 合成缓存目录上限，超限删最旧


def cleanup_tts_cache(max_bytes: int = TTS_CACHE_MAX_BYTES) -> int:
    """按 mtime 从旧到新删除合成缓存，目录总量压回 max_bytes 内。返回释放字节数。

    每条唯一文本一个 mp3，OCR 长译文日积月累无上限——长年使用磁盘膨胀。
    """
    try:
        entries = []
        total = 0
        for p in TTS_CACHE_DIR.glob("*.mp3"):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= max_bytes:
            return 0
        freed = 0
        for _mtime, size, p in sorted(entries):  # 最旧先删
            if total <= max_bytes:
                break
            try:
                p.unlink()
                total -= size
                freed += size
            except OSError:
                pass
        if freed:
            logger.info("tts cache cleanup: freed %.1f MB", freed / 1024 / 1024)
        return freed
    except OSError:
        return 0


class TTSService(QObject):
    play_requested = Signal(str, float)          # (mp3 路径, 音量 0..1) —— 主线程播放
    state_changed = Signal(str)                  # "playing" / "idle" / "error:<msg>"

    def __init__(self, cfg_getter, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self.play_requested.connect(self._play_file)
        self._sapi_stop = False  # SAPI 异步朗读的中断旗标（worker 线程内消费）
        # 启动即后台清一次缓存目录（LRU 上限）——目录扫描别卡主线程
        threading.Thread(target=cleanup_tts_cache, daemon=True).start()

    # ---------------------------------------------------------------- API

    def speak(self, text: str, lang: str = "auto") -> None:
        """播报文本。lang 仅保留兼容旧调用；音色由 _detect_lang 按内容自选。"""
        text = (text or "").strip()
        if not text:
            return
        self.stop()
        self._sapi_stop = False  # 清掉上一轮的中断请求，新播报不被立即掐断
        cfg = self._cfg_getter().get("tts", {})
        if not cfg.get("enabled", True):
            return
        engine = cfg.get("engine", "auto")
        threading.Thread(target=self._run, args=(text, lang, engine, cfg), daemon=True).start()

    def stop(self) -> None:
        self._player.stop()
        # SAPI 的 Skip 只在 worker 线程内调（同套间）——主线程跨套间调 COM
        # 方法在对方阻塞时会连带冻结主线程
        self._sapi_stop = True

    # ---------------------------------------------------------------- 合成与播放

    def _run(self, text: str, lang: str, engine: str, cfg: dict) -> None:
        # 音色按内容实际语言选，不信任调用方传入的 lang（读中文原文不该用英文音色）
        detected = _detect_lang(text)
        if engine == "custom":
            if self._custom_synth(text, cfg):
                return  # 播放由 play_requested 信号接管
            self._sapi_speak(text, detected)  # 配置不全 / 合成失败 → 系统语音兜底
            return
        if engine in ("auto", "edge"):
            voice = cfg.get("voice_zh" if detected == "zh" else "voice_en", "")
            rate = cfg.get("rate", "+0%")
            volume = cfg.get("volume", "+0%")
            if voice and self._edge_synth(text, voice, rate, volume, get_proxy(cfg)):
                return  # 播放由 play_requested 信号接管
            if engine == "edge":
                self.state_changed.emit("error:edge-tts 合成失败（可在设置里切换 TTS 引擎为系统语音）")
                return
        self._sapi_speak(text, detected)

    def _custom_synth(self, text: str, cfg: dict) -> bool:
        """OpenAI 兼容 /audio/speech 合成 mp3；失败返回 False（调用方降级 SAPI）。"""
        req = _custom_request(cfg.get("custom", {}), cfg.get("rate", "+0%"), text)
        if req is None:
            logger.warning("自定义 TTS 未配置完整（地址/模型/Key），降级 SAPI")
            return False
        (base_url, api_key, kwargs), cache = req
        try:
            if not cache.exists():
                from openai import OpenAI

                proxy = get_proxy(cfg)
                kw: dict = {"base_url": base_url, "api_key": api_key,
                            "timeout": 60, "max_retries": 1}
                if proxy:
                    import httpx2

                    kw["http_client"] = httpx2.Client(proxy=proxy)
                client = OpenAI(**kw)
                resp = client.audio.speech.create(**kwargs)
                tmp = cache.with_suffix(".tmp")
                tmp.write_bytes(resp.content)
                tmp.replace(cache)
            vol = _parse_volume(cfg.get("volume", "+0%"))
            self.play_requested.emit(str(cache), vol)
            return True
        except Exception as e:
            logger.warning("自定义 TTS 合成失败，降级 SAPI：%s", e)
            return False

    def _edge_synth(self, text: str, voice: str, rate: str, volume: str,
                    proxy: str = "") -> bool:
        cache_key = hashlib.md5(f"{text}|{voice}|{rate}|{volume}".encode()).hexdigest()
        cache = TTS_CACHE_DIR / f"{cache_key}.mp3"
        try:
            if not cache.exists():
                import edge_tts

                TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")

                async def _save() -> None:
                    # aiohttp 仅支持 http(s) 代理；socks 代理会抛错走 SAPI 降级
                    kw = {"proxy": proxy} if proxy else {}
                    await edge_tts.Communicate(
                        text, voice, rate=rate, volume=volume, **kw
                    ).save(str(tmp))

                asyncio.run(_save())
                tmp.replace(cache)
            vol = _parse_volume(volume)
            self.play_requested.emit(str(cache), vol)
            return True
        except Exception as e:
            logger.warning("edge-tts 失败，降级 SAPI：%s", e)
            return False

    def _play_file(self, path: str, volume: float) -> None:
        self._audio_out.setVolume(volume)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self.state_changed.emit("playing")

    def _on_playback_state(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self.state_changed.emit("idle")

    # ---------------------------------------------------------------- SAPI 兜底

    def _sapi_speak(self, text: str, lang: str) -> None:
        """SAPI 离线兜底：worker 线程内初始化 COM、异步朗读 + 轮询中断旗标。

        两个历史缺陷一并修掉：裸线程不 CoInitialize 时 Dispatch 必败（兜底
        从未真正生效过）；stop() 曾从主线程跨套间调 voice.Skip——对方 STA
        阻塞在朗读里时主线程会被连带冻结。
        """
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                voice = win32com.client.Dispatch("SAPI.SpVoice")
                _select_sapi_voice(voice, lang)
                self.state_changed.emit("playing")
                voice.Speak(text[:SAPI_TEXT_LIMIT], 1)  # SVSFlagsAsync：立即返回
                while voice.Status.RunningState == 2:   # SRSEIsSpeaking
                    if self._sapi_stop:
                        voice.Skip("Sentence", 10_000_000)  # 同套间调用，立即打断
                        break
                    time.sleep(0.05)
                self.state_changed.emit("idle")
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            logger.warning("SAPI 播报失败：%s", e)
            self.state_changed.emit(f"error:{e}")


def _parse_volume(spec: str) -> float:
    """'+0%'/'-50%' → 0..1"""
    try:
        return max(0.0, min(1.0, 1.0 + int(spec.replace("%", "")) / 100.0))
    except (ValueError, TypeError):
        return 1.0


def _detect_lang(text: str) -> str:
    """含任何汉字即按中文选音色——中文音色读英文单词可接受，反向很怪。"""
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return "zh" if cjk else "en"


def _rate_to_speed(rate: str) -> float:
    """edge-tts 语速 '+15%' → OpenAI speech speed 1.15（0.25–4.0 截断）。"""
    try:
        speed = 1.0 + int(str(rate).replace("%", "")) / 100.0
    except (ValueError, TypeError):
        return 1.0
    return max(0.25, min(4.0, speed))


def _custom_request(c: dict, rate: str, text: str):
    """组装自定义 TTS 请求；配置不完整返回 None。

    返回 ((base_url, api_key, kwargs), cache_path)。voice / speed 留默认时
    不传，保持最素请求以兼容各类 OpenAI 兼容服务。
    """
    base_url = c.get("base_url", "")
    model = c.get("model", "")
    api_key = c.get("api_key", "")
    if not (base_url and model and api_key):
        return None
    voice = c.get("voice", "")
    speed = _rate_to_speed(rate)
    kwargs: dict = {"model": model, "input": text, "response_format": "mp3"}
    if voice:
        kwargs["voice"] = voice
    if speed != 1.0:
        kwargs["speed"] = speed
    cache_key = hashlib.md5(f"custom|{base_url}|{model}|{voice}|{speed}|{text}".encode()).hexdigest()
    return (base_url, api_key, kwargs), TTS_CACHE_DIR / f"{cache_key}.mp3"


def _select_sapi_voice(voice, lang: str) -> None:
    """按语言挑 SAPI 音色（中文系统通常自带 Huihui）。失败保持默认。"""
    try:
        wanted = "zh" if lang == "zh" else "en"
        voices = voice.GetVoices()  # late-binding 下必须显式调用
        for i in range(voices.Count):
            desc = voices.Item(i).GetDescription() or ""
            if wanted in desc.lower():
                voice.Voice = voices.Item(i)
                return
    except Exception:
        pass
