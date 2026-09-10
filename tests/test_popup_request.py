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
