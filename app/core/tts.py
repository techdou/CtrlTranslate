"""TTS 播报：edge-tts 在线合成（音质好）→ Windows SAPI 离线兜底，自动降级。

线程模型：合成在后台线程（asyncio.run 独立事件循环）；播放用 QMediaPlayer，
必须主线程操作，经 play_file 信号投递。合成结果按 (文本, 音色, 语速, 音量)
哈希缓存 mp3，重复播报零等待零请求。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from app.config import DATA_DIR

logger = logging.getLogger("ctrltrans.tts")

TTS_CACHE_DIR = DATA_DIR / "tts_cache"
SAPI_TEXT_LIMIT = 800


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
        self._sapi_voice = None

    # ---------------------------------------------------------------- API

    def speak(self, text: str, lang: str = "en") -> None:
        """lang: 'en' 读原文 / 'zh' 读译文。主线程调用。"""
        text = (text or "").strip()
        if not text:
            return
        self.stop()
        cfg = self._cfg_getter().get("tts", {})
        if not cfg.get("enabled", True):
            return
        engine = cfg.get("engine", "auto")
        threading.Thread(target=self._run, args=(text, lang, engine, cfg), daemon=True).start()

    def stop(self) -> None:
        self._player.stop()
        voice = self._sapi_voice
        if voice is not None:
            try:
                voice.Skip("Sentence", 10_000_000)  # 跳到结尾，中断同步朗读
            except Exception:
                pass

    # ---------------------------------------------------------------- 合成与播放

    def _run(self, text: str, lang: str, engine: str, cfg: dict) -> None:
        if engine in ("auto", "edge"):
            voice = cfg.get("voice_en" if lang == "en" else "voice_zh", "")
            rate = cfg.get("rate", "+0%")
            volume = cfg.get("volume", "+0%")
            if voice and self._edge_synth(text, voice, rate, volume):
                return  # 播放由 play_requested 信号接管
            if engine == "edge":
                self.state_changed.emit("error:edge-tts 合成失败（可在设置里切换 TTS 引擎为系统语音）")
                return
        self._sapi_speak(text, lang)

    def _edge_synth(self, text: str, voice: str, rate: str, volume: str) -> bool:
        cache_key = hashlib.md5(f"{text}|{voice}|{rate}|{volume}".encode()).hexdigest()
        cache = TTS_CACHE_DIR / f"{cache_key}.mp3"
        try:
            if not cache.exists():
                import edge_tts

                TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")

                async def _save() -> None:
                    await edge_tts.Communicate(text, voice, rate=rate, volume=volume).save(str(tmp))

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
        try:
            import win32com.client

            voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._sapi_voice = voice
            _select_sapi_voice(voice, lang)
            self.state_changed.emit("playing")
            voice.Speak(text[:SAPI_TEXT_LIMIT], 0)  # 同步朗读，可被 stop() 打断
            self.state_changed.emit("idle")
        except Exception as e:
            logger.warning("SAPI 播报失败：%s", e)
            self.state_changed.emit(f"error:{e}")
        finally:
            self._sapi_voice = None


def _parse_volume(spec: str) -> float:
    """'+0%'/'-50%' → 0..1"""
    try:
        return max(0.0, min(1.0, 1.0 + int(spec.replace("%", "")) / 100.0))
    except (ValueError, TypeError):
        return 1.0


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
