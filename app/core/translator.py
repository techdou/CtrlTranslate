"""LLM 流式翻译：OpenAI 兼容多供应商，支持主备自动切换。

后台线程执行流式请求，通过信号把增量推给 UI；新请求会作废旧请求
（任务号校验 + stream.close()）。用户可在设置里覆盖翻译 prompt。
主服务请求失败（网络 / 限流 / Key 失效等）且配置了备用服务时，自动用
备用服务重发；切换时发 fallback_started 信号（UI 重置正文并提示）。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

from app.config import resolve_endpoint, resolve_fallback

logger = logging.getLogger("ctrltrans.translator")


class _TaskCancelled(Exception):
    """流式请求期间任务被新请求作废——静默丢弃，不算错误。"""

STUDY_SYSTEM_PROMPT = """你是一名专业翻译引擎，服务于科研与工程文献的阅读场景。
把用户给出的文本在中文与英文之间互译（文本以英文为主则译为中文；以中文为主则译为英文）。
要求：忠实原意、专业术语准确、语句通顺、不增删内容。

输出格式（严格遵守，不要输出任何额外说明、前后缀或代码块标记）：
1. 直接给出完整译文；
2. 若原文包含专业术语、缩写、领域黑话或值得学习的表达，在译文后另起一行输出“【术语】”，\
之后每行一条：原文术语 — 中文含义（一句话解释）；
3. 没有值得列出的术语就省略第 2 部分。"""

CONCISE_SYSTEM_PROMPT = """你是一名专业翻译引擎。把用户给出的文本在中文与英文之间互译
（英文为主译为中文，中文为主译为英文），只输出译文本身，忠实原意、术语准确，不要任何解释或标记。"""


def build_messages(cfg: dict, text: str) -> list[dict]:
    translate_cfg = cfg.get("translate", {})
    custom = (translate_cfg.get("custom_prompt") or "").strip()
    mode = translate_cfg.get("mode", "study")
    system = custom if custom else (STUDY_SYSTEM_PROMPT if mode == "study" else CONCISE_SYSTEM_PROMPT)
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


class Translator(QObject):
    chunk = Signal(str, int)           # 增量文本, task_id
    finished = Signal(str, int)        # 完整译文, task_id
    failed = Signal(str, int)          # 错误消息, task_id
    fallback_started = Signal(int)     # 主服务失败、开始用备用服务重试, task_id

    def __init__(self, cfg_getter, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._task = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- API

    def translate(self, text: str) -> int:
        """发起翻译，返回任务号。旧任务会被作废。"""
        with self._lock:
            self._task += 1
            task_id = self._task
        threading.Thread(target=self._run, args=(text, task_id), daemon=True).start()
        return task_id

    def cancel_all(self) -> None:
        with self._lock:
            self._task += 1

    def test_connection(self, on_ok, on_fail, endpoint: tuple[str, str, str] | None = None) -> None:
        """设置界面用：发一条 mini 请求验证 endpoint（None = 主服务）。回调在主线程执行。"""
        threading.Thread(target=self._test_run, args=(on_ok, on_fail, endpoint), daemon=True).start()

    # ---------------------------------------------------------------- 内部

    def _expired(self, task_id: int) -> bool:
        with self._lock:
            return task_id != self._task

    def _stream_once(self, endpoint: tuple[str, str, str], messages: list[dict],
                     timeout: float, task_id: int) -> str:
        """对给定服务做一次完整流式请求；成功返回全文。

        任务被作废时抛 _TaskCancelled；请求/流读取失败抛原始异常（由调用方
        决定是否 fallback 与错误文案）。chunk 增量实时 emit。
        """
        from openai import OpenAI

        base_url, api_key, model = endpoint
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)
        try:
            stream = client.chat.completions.create(
                model=model, messages=messages, stream=True, temperature=0.3,
            )
        except Exception as e:
            if self._expired(task_id):
                raise _TaskCancelled from e
            raise

        parts: list[str] = []
        try:
            for event in stream:
                if self._expired(task_id):
                    stream.close()
                    raise _TaskCancelled
                if not event.choices:
                    continue
                piece = getattr(event.choices[0].delta, "content", None)
                if piece:
                    parts.append(piece)
                    self.chunk.emit(piece, task_id)
        except _TaskCancelled:
            raise
        except Exception as e:
            if self._expired(task_id):
                raise _TaskCancelled from e
            raise
        if self._expired(task_id):
            raise _TaskCancelled
        return "".join(parts)

    def _run(self, text: str, task_id: int) -> None:
        try:
            self._run_inner(text, task_id)
        except _TaskCancelled:
            pass  # 被新请求作废，静默丢弃

    def _run_inner(self, text: str, task_id: int) -> None:
        try:
            from openai import OpenAI  # noqa: F401 —— 与 _stream_once 保持同一入口报缺库
        except ImportError:
            self.failed.emit("缺少 openai 库，请 pip install openai", task_id)
            return

        cfg = self._cfg_getter()
        primary = resolve_endpoint(cfg)
        if not primary[0] or not primary[2]:
            self.failed.emit("翻译服务未配置完整（base_url / model），请到设置中填写", task_id)
            return
        if not primary[1]:
            self.failed.emit("未配置 API Key，请到托盘菜单 → 设置中填写", task_id)
            return

        timeout = float(cfg.get("translate", {}).get("timeout_s", 60))
        messages = build_messages(cfg, text)

        try:
            result = self._stream_once(primary, messages, timeout, task_id)
        except Exception as e:
            primary_err = e
        else:
            self.finished.emit(result, task_id)
            return

        fb = resolve_fallback(cfg)
        if not fb[0]:
            self.failed.emit(self._friendly_error(primary_err, primary[0]), task_id)
            return

        self.fallback_started.emit(task_id)
        try:
            result = self._stream_once(fb, messages, timeout, task_id)
        except Exception as e:
            self.failed.emit(
                f"主服务失败：{self._friendly_error(primary_err, primary[0])}\n"
                f"备用服务也失败：{self._friendly_error(e, fb[0])}",
                task_id,
            )
            return
        logger.info("主服务失败已由备用服务完成翻译（task %s）", task_id)
        self.finished.emit(result, task_id)

    def _test_run(self, on_ok, on_fail, endpoint: tuple[str, str, str] | None) -> None:
        base_url = ""
        try:
            from openai import OpenAI

            cfg = self._cfg_getter()
            base_url, api_key, model = endpoint or resolve_endpoint(cfg)
            if not base_url or not model:
                raise ValueError("API 地址 / 模型未填写")
            if not api_key:
                raise ValueError("API Key 为空")
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=20, max_retries=0)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=8,
            )
            answer = (resp.choices[0].message.content or "").strip()
            on_ok(f"连通正常（模型返回：{answer[:20]}）")
        except Exception as e:
            on_fail(self._friendly_error(e, base_url))

    # ---------------------------------------------------------------- 错误文案

    @staticmethod
    def _friendly_error(e: Exception, base_url: str) -> str:
        name = type(e).__name__
        msg = str(e).replace("\n", " ")[:300]
        if "AuthenticationError" in name or getattr(e, "status_code", None) == 401:
            return "API Key 无效或没有权限（401），请检查 Key 与所选服务商是否匹配"
        if getattr(e, "status_code", None) == 404 or "model" in msg.lower() and "not found" in msg.lower():
            return "模型名不存在（404），请到设置里核对 model 名称"
        if getattr(e, "status_code", None) == 429:
            return "请求过于频繁或额度不足（429），稍后再试或更换模型"
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return "请求超时，网络不通或需要代理"
        if "connection" in msg.lower() or "api_connection" in name.lower():
            hint = "（该服务商可能需要代理）" if base_url and "bigmodel" not in base_url else ""
            return f"无法连接到翻译服务{hint}：{msg[:120]}"
        return f"翻译失败：{msg[:160]}"
