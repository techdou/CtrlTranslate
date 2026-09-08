"""配置管理：默认值 + 用户 JSON 深合并持久化。

数据目录固定在 ~/.ctrltrans/，与安装位置解耦，升级不丢配置。
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

APP_NAME = "CtrlTranslate"
DATA_DIR = Path.home() / ".ctrltrans"
CONFIG_PATH = DATA_DIR / "config.json"

# OpenAI 兼容供应商预设：切换预设时仅是"填表模板"，实际生效值存 provider 三件套
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "zhipu": {
        "label": "智谱 AI · GLM Flash（免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash-250414",
        "api_key_url": "https://bigmodel.cn",
        "models": ["glm-4-flash-250414", "glm-4.5-flash", "glm-4-flashx-250414"],
        "free": True,
        "needs_proxy": False,
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow（有免费档）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-8B",
        "api_key_url": "https://cloud.siliconflow.cn",
        "models": ["Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "free": True,
        "needs_proxy": False,
    },
    "deepseek": {
        "label": "DeepSeek（付费，低价）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_url": "https://platform.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "free": False,
        "needs_proxy": False,
    },
    "openrouter": {
        "label": "OpenRouter（含 :free 模型，需代理）",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "api_key_url": "https://openrouter.ai/keys",
        "models": ["meta-llama/llama-3.3-70b-instruct:free", "google/gemini-2.0-flash-exp:free"],
        "free": True,
        "needs_proxy": True,
    },
    "gemini": {
        "label": "Google Gemini（免费额度，需代理）",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.0-flash",
        "api_key_url": "https://aistudio.google.com",
        "models": ["gemini-2.0-flash", "gemini-2.5-flash"],
        "free": True,
        "needs_proxy": True,
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容服务）",
        "base_url": "",
        "model": "",
        "api_key_url": "",
        "models": [],
        "free": False,
        "needs_proxy": False,
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": {
        "name": "zhipu",
        "base_url": PROVIDER_PRESETS["zhipu"]["base_url"],
        "model": PROVIDER_PRESETS["zhipu"]["model"],
        "api_key": "",
        "fallback": {             # 备用翻译服务：主服务失败时自动重发；三件套留空 = 禁用
            "base_url": "",
            "model": "",
            "api_key": "",
        },
    },
    "translate": {
        "mode": "study",            # study=译文+术语表 / concise=仅译文
        "custom_prompt": "",        # 非空则整体覆盖内置 system prompt，含 {text} 占位符
        "max_chars": 3000,
        "timeout_s": 60,
    },
    "trigger": {
        "enabled": True,
        "key": "ctrl",              # 双击触发键：ctrl / alt / shift
        "interval_ms": 300,         # 双击判定窗口
    },
    "capture": {
        "prefer_uia": True,         # False = 直接走剪贴板模拟
        "clipboard_wait_ms": 400,
    },
    "ocr": {
        "enabled": True,
        "model": "glm-4v-flash",    # 视觉模型（智谱免费）；地址与 Key 复用 provider 主服务
        "hotkey": "alt+q",          # OCR 截图热键（keyboard 库格式）；留空 = 禁用
    },
    "tts": {
        "enabled": True,
        "engine": "auto",           # auto=先 edge-tts 失败转 SAPI / edge / sapi / custom
        "voice_zh": "zh-CN-XiaoxiaoNeural",
        "voice_en": "en-US-AriaNeural",
        "rate": "+0%",              # edge-tts 语速格式；custom 引擎自动换算为 speed 0.25-4.0
        "volume": "+0%",
        "auto_play": False,
        "auto_play_what": "source", # source / translated
        "custom": {                 # engine=custom 时的 OpenAI 兼容 TTS（/audio/speech）
            "base_url": "",
            "model": "",
            "voice": "",
            "api_key": "",
        },
    },
    "popup": {
        "theme": "dark",            # dark / light
        "font_size": 14,
        "opacity": 0.96,
        "auto_close_s": 0,          # 0 = 不自动关闭
        "width": 480,
    },
    "general": {
        "history_enabled": True,
    },
    "network": {
        "proxy": "",                # 如 http://127.0.0.1:7890；留空 = 直连。翻译与 TTS 共用
    },
    "webdav": {
        "url": "",                  # 如 https://dav.jianguoyun.com/dav/
        "username": "",             # 坚果云 = 账户邮箱
        "password": "",             # 坚果云 = 应用密码（非登录密码）
        "remote_dir": "CtrlTranslate",  # 远端目录；备份文件为 <dir>/backup.json
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 里的值覆盖 base，缺失键回退 base 默认值。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


_lock = threading.Lock()


def load_config(path: Path = CONFIG_PATH) -> dict:
    user: dict = {}
    try:
        if path.exists():
            user = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 配置损坏时不让它挡启动，回退默认并保留坏文件备查
        try:
            path.rename(path.with_suffix(".json.bak"))
        except OSError:
            pass
    if not isinstance(user, dict):
        user = {}
    return deep_merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(deep_merge(DEFAULT_CONFIG, cfg), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def resolve_endpoint(cfg: dict) -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)，供 openai SDK 直接使用。"""
    p = cfg.get("provider", {})
    return (
        p.get("base_url") or "",
        p.get("api_key") or "",
        p.get("model") or "",
    )


def resolve_fallback(cfg: dict) -> tuple[str, str, str]:
    """备用翻译服务三件套；任一为空即视为未配置（返回空串元组）。"""
    fb = cfg.get("provider", {}).get("fallback", {})
    base_url = fb.get("base_url") or ""
    api_key = fb.get("api_key") or ""
    model = fb.get("model") or ""
    if not (base_url and api_key and model):
        return ("", "", "")
    return (base_url, api_key, model)


def get_proxy(cfg: dict) -> str:
    """网络代理（翻译与 TTS 出站请求共用）；空串 = 直连。"""
    return str(cfg.get("network", {}).get("proxy") or "").strip()


def resolve_vision_endpoint(cfg: dict) -> tuple[str, str, str]:
    """OCR 用：(base_url, api_key, vision_model)。地址与 Key 复用 provider 主服务，
    模型单独配置（文本模型不能接图，不能沿用 provider.model）。"""
    p = cfg.get("provider", {})
    return (
        p.get("base_url") or "",
        p.get("api_key") or "",
        str(cfg.get("ocr", {}).get("model") or "").strip(),
    )


def get_webdav(cfg: dict) -> tuple[str, str, str, str]:
    """WebDAV 备份配置 (url, username, password, remote_dir)；url/账号/密码任一为空
    = 未配置（全空串元组），调用方据此禁用备份功能。"""
    w = cfg.get("webdav", {})
    url = str(w.get("url") or "").strip()
    user = str(w.get("username") or "").strip()
    pw = str(w.get("password") or "")
    remote_dir = str(w.get("remote_dir") or "").strip() or "CtrlTranslate"
    if not (url and user and pw):
        return ("", "", "", "")
    return (url, user, pw, remote_dir)
