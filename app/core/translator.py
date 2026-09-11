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

from app.config import get_proxy, resolve_endpoint, resolve_fallback, resolve_vision_endpoint

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

OCR_SYSTEM_PROMPT = """你是一名 OCR 识别引擎。识别图片中的全部文字，按原始阅读顺序输出纯文本。
只输出识别到的文字本身：不要翻译、不要解释、不要总结、不要添加任何说明或标记。
图片中没有文字就输出空内容。"""


def build_messages(cfg: dict, text: str) -> list[dict]:
    translate_cfg = cfg.get("translate", {})
    custom = (translate_cfg.get("custom_prompt") or "").strip()
    mode = translate_cfg.get("mode", "study")
    system = custom if custom else (STUDY_SYSTEM_PROMPT if mode == "study" else CONCISE_SYSTEM_PROMPT)
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def build_ocr_messages(image_b64: str) -> list[dict]:
    """OCR 第一阶段用：图片 + 纯识别指令的多模态消息（OpenAI 协议格式，
    _stream_once 透传）。识别与翻译分离——见 _run_image_inner 注释。"""
    return [
        {"role": "system", "content": OCR_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": "请识别图片中的全部文字。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]},
    ]


def cache_key(cfg: dict, text: str) -> str:
    """缓存键：正文 + 影响译文的所有配置（模型/模式/自定义 prompt）。"""
    import hashlib

    t = cfg.get("translate", {})
    raw = "|".join([
        text,
        cfg.get("provider", {}).get("model") or "",
        t.get("mode", "study"),
        (t.get("custom_prompt") or "").strip(),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _make_client(endpoint: tuple[str, str, str], timeout: float, max_retries: int,
                 proxy: str = ""):
    """OpenAI 兼容客户端；配置了代理时经 httpx2 显式走代理（GUI 启动不继承终端环境变量）。"""
    from openai import OpenAI

    base_url, api_key, model = endpoint
    kwargs: dict = {"base_url": base_url, "api_key": api_key,
                    "timeout": timeout, "max_retries": max_retries}
    if proxy:
        import httpx2

        kwargs["http_client"] = httpx2.Client(proxy=proxy)
    return OpenAI(**kwargs)


class Translator(QObject):
    chunk = Signal(str, int)           # 增量文本, task_id
    finished = Signal(str, int)        # 完整译文, task_id
    failed = Signal(str, int)          # 错误消息, task_id
    fallback_started = Signal(int)     # 主服务失败、开始用备用服务重试, task_id
    ocr_text_ready = Signal(str, int)  # OCR 第一阶段识别出的原文, task_id
    test_result = Signal(object, str, bool)  # (回调, 消息, 是否成功) —— 后台线程结果回投主线程

    def __init__(self, cfg_getter, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._task = 0
        self._lock = threading.Lock()
        self.test_result.connect(self._dispatch_test_result)

    # ---------------------------------------------------------------- API

    def translate(self, text: str, use_cache: bool = True, raw: bool = False) -> int:
        """发起翻译，返回任务号。旧任务会被作废。use_cache=False 强制重译（重试入口）。
        raw=True 时 text 是完整指令 prompt（术语解释等），不套翻译 system。"""
        with self._lock:
            self._task += 1
            task_id = self._task
        threading.Thread(target=self._run, args=(text, task_id, use_cache, raw), daemon=True).start()
        return task_id

    def translate_image(self, image_b64: str, followup_prompt: str | None = None) -> int:
        """OCR 截图翻译（两阶段）：视觉模型只做识别，识别文本再走普通文本
        翻译链（study/concise prompt、术语段、缓存、备用服务全复用）。

        followup_prompt 非空时第二阶段改发该模板（.format(text=识别文本)，
        raw 模式不套翻译 system）——截图术语解释用：识别出术语后交给
        术语解释链而非翻译链。

        两阶段的根因：glm-4v-flash 这类 OCR 专精模型指令遵循弱，"识别并翻译"
        一步到位经常只回识别文本不翻译（真机实测）；识别交给它擅长的，翻译交回
        文本模型。与文本翻译共用 task_id 体系（互相作废）与流式信号；不读图片
        缓存（每次截图内容都不同），但识别出的文本走翻译缓存——同一段文字重复
        截图秒出。识别阶段失败不走 fallback（备用是文本模型，接图必错）。"""
        with self._lock:
            self._task += 1
            task_id = self._task
        threading.Thread(target=self._run_image,
                         args=(image_b64, task_id, followup_prompt), daemon=True).start()
        return task_id

    def cancel_all(self) -> None:
        with self._lock:
            self._task += 1

    def test_connection(self, on_ok, on_fail, endpoint: tuple[str, str, str] | None = None) -> None:
        """设置界面用：发一条 mini 请求验证 endpoint（None = 已保存的主服务）。
        结果经 test_result 信号在主线程执行回调（UI 安全）。"""
        threading.Thread(target=self._test_run, args=(on_ok, on_fail, endpoint), daemon=True).start()

    @staticmethod
    def _dispatch_test_result(callback, message: str, ok: bool) -> None:
        callback(message)

    # ---------------------------------------------------------------- 内部

    def _expired(self, task_id: int) -> bool:
        with self._lock:
            return task_id != self._task

    def _stream_once(self, endpoint: tuple[str, str, str], messages: list[dict],
                     timeout: float, task_id: int, proxy: str = "") -> str:
        """对给定服务做一次完整流式请求；成功返回全文。

        任务被作废时抛 _TaskCancelled；请求/流读取失败抛原始异常（由调用方
        决定是否 fallback 与错误文案）。chunk 增量实时 emit。
        """
        client = _make_client(endpoint, timeout, max_retries=1, proxy=proxy)
        _base, _key, model = endpoint
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

    def _run(self, text: str, task_id: int, use_cache: bool = True, raw: bool = False) -> None:
        try:
            self._run_inner(text, task_id, use_cache, raw)
        except _TaskCancelled:
            pass  # 被新请求作废，静默丢弃

    def _run_image(self, image_b64: str, task_id: int,
                   followup_prompt: str | None = None) -> None:
        try:
            self._run_image_inner(image_b64, task_id, followup_prompt)
        except _TaskCancelled:
            pass

    def _run_inner(self, text: str, task_id: int, use_cache: bool = True,
                   raw: bool = False) -> None:
        try:
            from openai import OpenAI  # noqa: F401 —— 与 _make_client 保持同一入口报缺库
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

        if use_cache:
            cached = self._cache_get(cfg, text)
            if cached is not None:
                logger.info("cache hit (task %s)", task_id)
                self.finished.emit(cached, task_id)
                return

        timeout = float(cfg.get("translate", {}).get("timeout_s", 60))
        # raw=True：text 本身是完整指令（术语解释模板），不套翻译 system
        messages = ([{"role": "user", "content": text}] if raw
                    else build_messages(cfg, text))
        proxy = get_proxy(cfg)

        try:
            result = self._stream_once(primary, messages, timeout, task_id, proxy)
        except Exception as e:
            primary_err = e
        else:
            self._cache_put(cfg, text, result)
            self.finished.emit(result, task_id)
            return

        fb = resolve_fallback(cfg)
        if not fb[0]:
            self.failed.emit(self._friendly_error(primary_err, primary[0]), task_id)
            return

        self.fallback_started.emit(task_id)
        try:
            result = self._stream_once(fb, messages, timeout, task_id, proxy)
        except Exception as e:
            self.failed.emit(
                f"主服务失败：{self._friendly_error(primary_err, primary[0])}\n"
                f"备用服务也失败：{self._friendly_error(e, fb[0])}",
                task_id,
            )
            return
        logger.info("主服务失败已由备用服务完成翻译（task %s）", task_id)
        self._cache_put(cfg, text, result)
        self.finished.emit(result, task_id)

    def _run_image_inner(self, image_b64: str, task_id: int,
                         followup_prompt: str | None = None) -> None:
        try:
            from openai import OpenAI  # noqa: F401 —— 与 _make_client 保持同一入口报缺库
        except ImportError:
            self.failed.emit("缺少 openai 库，请 pip install openai", task_id)
            return

        cfg = self._cfg_getter()
        base_url, api_key, model = resolve_vision_endpoint(cfg)
        if not base_url:
            self.failed.emit("翻译服务未配置（base_url），请到设置中填写", task_id)
            return
        if not api_key:
            self.failed.emit("未配置 API Key，请到托盘菜单 → 设置中填写", task_id)
            return
        if not model:
            self.failed.emit("未配置 OCR 识别模型，请到 设置 → 触发与取词 → 屏幕截图翻译 填写", task_id)
            return

        # 阶段一：视觉模型纯识别（不发 chunk——用户要的是译文不是原文堆砌）
        timeout = float(cfg.get("translate", {}).get("timeout_s", 60))
        try:
            ocr_text = self._stream_once(
                (base_url, api_key, model), build_ocr_messages(image_b64),
                timeout, task_id, get_proxy(cfg))
        except Exception as e:
            self.failed.emit(self._friendly_error(e, base_url), task_id)
            return
        ocr_text = ocr_text.strip()
        if not ocr_text:
            self.failed.emit("未能从图片中识别出文字（截图区域可能没有文本）", task_id)
            return
        self.ocr_text_ready.emit(ocr_text, task_id)

        # 阶段二：默认走文本翻译链；给了 followup_prompt 则走 raw 指令链
        # （截图术语解释：识别文本填进术语解释模板）
        if followup_prompt:
            self._run_inner(followup_prompt.format(text=ocr_text),
                            task_id, use_cache=True, raw=True)
        else:
            self._run_inner(ocr_text, task_id, use_cache=True)

    def _test_run(self, on_ok, on_fail, endpoint: tuple[str, str, str] | None) -> None:
        base_url = ""
        try:
            if endpoint is None:
                endpoint = resolve_endpoint(self._cfg_getter())
            base_url, api_key, model = endpoint
            if not base_url or not model:
                raise ValueError("API 地址 / 模型未填写")
            if not api_key:
                raise ValueError("API Key 为空")
            proxy = get_proxy(self._cfg_getter())
            client = _make_client(endpoint, 20, 0, proxy=proxy)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=8,
            )
            answer = (resp.choices[0].message.content or "").strip()
            self.test_result.emit(on_ok, f"连通正常（模型返回：{answer[:20]}）", True)
        except Exception as e:
            self.test_result.emit(on_fail, self._friendly_error(e, base_url), False)

    # ---------------------------------------------------------------- 缓存

    @staticmethod
    def _cache_get(cfg: dict, text: str) -> str | None:
        try:
            from app.db import database

            row = database.get_cached_translation(cache_key(cfg, text))
            return row
        except Exception:
            return None  # 缓存层故障不影响翻译主流程

    @staticmethod
    def _cache_put(cfg: dict, text: str, result: str) -> None:
        try:
            from app.db import database

            database.put_cached_translation(cache_key(cfg, text), text, result)
        except Exception:
            pass

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
