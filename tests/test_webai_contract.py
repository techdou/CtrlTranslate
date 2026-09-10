"""前后端契约快照：信号签名、PROBE 字段 vs 读取点、跨层接口。

防漂移机制：契约任一侧（adapter JS / 引擎 / popup / main 接线）改动未同步
另一侧时，此处断言先红——强制两侧对齐，避免"E2E 通过但真实链路坏掉"
（review #1：测试脚本拼了指令而 main 链路没拼，正是这类漂移）。
"""

import inspect
import re


# ---------------------------------------------------------------- 引擎信号签名

def test_webai_engine_signal_signatures():
    """信号签名快照：main/popup 按 (类型, 任务号) 接线，签名漂移=接线静默失效。"""
    from app.core.webai import WebAIEngine

    mo = WebAIEngine.staticMetaObject
    sigs = {}
    for i in range(mo.methodOffset(), mo.methodCount()):
        m = mo.method(i)
        # methodOffset 起全是本类声明；WebAIEngine 无普通方法/槽，按信号收集
        if str(m.methodType()).endswith("Signal"):
            sigs[bytes(m.methodSignature()).decode()] = True
    expected = {
        "chunk(QString,int)",       # popup.on_chunk(piece, task_id)
        "finished(QString,int)",    # main.on_translated → popup.on_done
        "failed(QString,int)",      # popup.on_error（守卫 task_id）
        "upload_done(bool,QString)",
        "login_required()",
    }
    missing = expected - set(sigs)
    assert not missing, f"引擎信号签名漂移：{missing}（实际 {sorted(sigs)}）"


# ---------------------------------------------------------------- PROBE 字段对齐

def test_probe_fields_cover_all_consumers():
    """adapter PROBE 返回字段 ⊇ 引擎全部 d.get("...") 读取点。

    字段名在 JS 字符串与 Python 读取点两侧各写一遍，无编译期检查——
    typo 时 probe 永远拿到 None，任务挂到超时。此处静态对齐兜底。
    """
    from app.core import webai

    probe_fields = set(re.findall(r"^ {8}(\w+):", webai.DeepSeekAdapter.PROBE, re.M))
    assert probe_fields, "PROBE 字段提取失败（JS 结构改版？调整正则）"

    src = inspect.getsource(webai)
    reads = set(re.findall(r'd\.get\("(\w+)"', src))  # 不限右括号：兼容带默认值写法
    # _probe_upload/_probe_ensure 等也读 url/inputValue 等——全部必须在 PROBE 字段内
    leaked = reads - probe_fields
    assert not leaked, f"引擎读取了 PROBE 不提供的字段：{leaked}"


def test_probe_reads_are_all_consumed_or_documented():
    """反向：PROBE 字段无死字段（新增字段必须有人读，防止悄悄失效）。"""
    from app.core import webai

    probe_fields = set(re.findall(r"^ {8}(\w+):", webai.DeepSeekAdapter.PROBE, re.M))
    src = inspect.getsource(webai)
    reads = set(re.findall(r'd\.get\("(\w+)"', src))
    dead = probe_fields - reads
    assert not dead, f"PROBE 字段无人读取（死字段或读取点 typo）：{dead}"


# ---------------------------------------------------------------- popup 接口签名

def test_popup_show_translation_contract():
    """main 调用面：engine/payload/request/adopt_task 是网页模式接线的依赖参数。"""
    from app.ui.popup import TranslatePopup

    sig = inspect.signature(TranslatePopup.show_translation)
    params = set(sig.parameters)
    need = {"source", "method", "force", "engine", "request", "payload"}
    missing = need - params
    assert not missing, f"show_translation 缺参数：{missing}（main 接线依赖）"
    assert hasattr(TranslatePopup, "adopt_task")
    assert hasattr(TranslatePopup, "ocr_retry_requested")
    assert hasattr(TranslatePopup, "engine_toggle_requested")


def test_tray_webai_signals_exist():
    """托盘三信号：main 按名接线，改名=菜单静默失灵。"""
    from app.ui.tray import TrayController

    for name in ("webai_window_requested", "webai_new_session", "webai_upload_requested"):
        assert hasattr(TrayController, name), f"TrayController 缺信号 {name}"


# ---------------------------------------------------------------- main 注入契约

def test_main_webai_prompts_carry_text_placeholder():
    """main 的网页模式 prompt 常量必须含 {text} 占位且非空（划词指令注入依赖）。"""
    import main

    assert "{text}" in main.WEBAI_TRANSLATE_PROMPT
    assert "{text}" in main.WEBAI_TRANSLATE_PROMPT_CONCISE
    assert main.WEBAI_OCR_PROMPT  # 截图指令非空
    # 学习版要求「【术语】」段——与 popup._parse_terms 解析格式对齐
    assert "【术语】" in main.WEBAI_TRANSLATE_PROMPT
