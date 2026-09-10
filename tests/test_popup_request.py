"""popup 翻译请求分流测试：request=False 只展示不发请求、adopt_task 挂回任务号。

背景（P0 回归锁）：OCR 旧流程 show_translation 偷发文本任务占住 _task_id，
后续 translate_image 的任务号对不上被守卫丢弃——截图结果永远不显示。
"""

import pytest

from app.config import DEFAULT_CONFIG


class _FakeTranslator:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def translate(self, text: str, use_cache: bool = True) -> int:
        self.calls.append((text, use_cache))
        return 100


@pytest.fixture()
def popup(qapp):
    from app.ui.popup import TranslatePopup

    eng = _FakeTranslator()
    p = TranslatePopup(lambda: DEFAULT_CONFIG, tts=None, translator=eng)
    yield p, eng


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_show_translation_default_sends_request(popup):
    p, eng = popup
    tid = p.show_translation("hello")
    assert tid == 100
    assert p._task_id == 100
    assert eng.calls == [("hello", True)]


def test_show_translation_request_false_skips(popup):
    """OCR 场景：只展示不发请求，任务号待 adopt_task 挂回。"""
    p, eng = popup
    tid = p.show_translation("屏幕截图 OCR", method="ocr", request=False)
    assert tid == -1
    assert eng.calls == []  # 没有偷发文本翻译

    p.adopt_task(200)
    assert p._task_id == 200
    p.on_done("识别结果", 200)  # 图片任务结果不再被守卫丢弃
    assert p._translated == "识别结果"


def test_task_guard_still_rejects_stale(popup):
    p, _eng = popup
    p.show_translation("first", request=False)
    p.adopt_task(1)
    p.on_done("过期结果", 999)
    assert p._translated == ""  # 旧守卫依然有效


# ---------------------------------------------------------------- review 修复回归

def test_payload_sent_to_engine_not_source(popup):
    """网页模式：source 仅预览/历史，payload（带指令 prompt）才是发给引擎的内容。"""
    p, eng = popup
    p.show_translation("hello world", engine=None, payload="请翻译：hello world")
    assert eng.calls == [("请翻译：hello world", True)]  # 发的是 payload
    assert p._source == "hello world"                     # 预览保持原文


def test_busy_minus_one_shows_error(popup):
    """引擎忙返回 -1：立即转错误显示，不裸等回调（骨架屏永转修复）。"""
    p, eng = popup

    class _BusyEngine:
        def translate(self, text, use_cache=True):
            return -1

    tid = p.show_translation("hello", engine=_BusyEngine())
    assert tid == -1
    assert p._task_id == -1
    # show_message 已切错误态（translated 留空、错误窗显示）——不再断言内部渲染细节，
    # 关键断言：后续回调全被守卫挡住也不会有"永转"状态残留
    p.on_done("迟到的结果", 999)
    assert p._translated == ""


def test_ocr_retry_emits_signal_not_text(popup):
    """OCR 态重试：发信号交还 main（截图在 main 手里），不再把占位文本送翻译。"""
    p, eng = popup
    got = []
    p.ocr_retry_requested.connect(lambda: got.append(True))
    p.show_translation("屏幕截图 OCR", method="ocr", request=False)
    p._retry()
    assert got == [True]
    assert eng.calls == []  # 没有把"屏幕截图 OCR"当文本翻译


def test_text_retry_resends_payload(popup):
    """文本态重试：重发 payload（网页模式完整 prompt）而非 source。"""
    p, eng = popup
    p.show_translation("原文", engine=None, payload="指令+原文")
    assert eng.calls == [("指令+原文", True)]
    p._retry()
    assert eng.calls[-1] == ("指令+原文", False)  # 重试 force=True → use_cache=False


def test_show_result_resets_engine_state(popup):
    """历史回看：引擎/方式/负载清空，重试回退默认 API 行为（不残留 webai）。"""
    p, eng = popup
    alt = _FakeTranslator()  # 模拟"上次是网页引擎"的残留
    p.show_translation("原文", method="", engine=alt, payload="x")
    p.show_result("历史原文", "历史译文")
    assert p._engine is None and p._method == "" and p._payload is None
    p._retry()  # 回看后的重试走默认 API 翻译器（eng），不再走 alt
    assert eng.calls and eng.calls[-1][0] == "历史原文"


# ---------------------------------------------------------------- 引擎一键切换

def test_engine_button_toggles_and_refreshes(popup):
    """切换钮：点击发信号（main 写配置）；文案随 cfg 刷新（当前引擎所见即所得）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    import app.config as config

    p, _eng = popup
    cfg = config.DEFAULT_CONFIG.copy()
    # fixture 的 cfg_getter 返回 DEFAULT_CONFIG 常量——换可变 dict 才能反映切换
    p._cfg_getter = lambda: cfg

    got = []
    p.engine_toggle_requested.connect(lambda: got.append(True))
    assert p.btn_engine.text() == "API"          # 默认关网页 → 显示当前引擎 API

    QTest.mouseClick(p.btn_engine, Qt.MouseButton.LeftButton)
    assert got == [True]                          # 只发信号，popup 不自己写配置

    cfg["webai"]["enabled"] = True                # 模拟 main 已写配置并回调刷新
    p.refresh_engine_button()
    assert p.btn_engine.text() == "网页"
    assert "网页版" in p.btn_engine.toolTip()


def test_toggle_webai_enabled_pure_function():
    import main

    cfg = {"webai": {"enabled": False}}
    assert main.toggle_webai_enabled(cfg) is True
    assert cfg["webai"]["enabled"] is True        # 原地翻转
    assert main.toggle_webai_enabled(cfg) is False
    assert cfg["webai"]["enabled"] is False
    # 无 webai 段的旧配置也能安全翻转
    cfg2 = {}
    assert main.toggle_webai_enabled(cfg2) is True
    assert cfg2["webai"]["enabled"] is True
