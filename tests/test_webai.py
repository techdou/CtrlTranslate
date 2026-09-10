"""WebAI 引擎测试：adapter JS 生成、任务状态机、流式稳定判定（不依赖真实网页）。

真实网页交互走 scripts/webai_e2e.py 手动验证（需要登录态，不进 CI）。
"""

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- adapter

def test_fill_js_escapes_text_safely():
    import json

    from app.core.webai import DeepSeekAdapter

    text = '含"引号"和\n换行'
    js = DeepSeekAdapter.fill_js(text)
    assert json.dumps(text) in js          # json 转义后整体注入（中文转 \u 序列）
    assert "__TEXT__" not in js            # 占位符已替换


def test_probe_and_send_js_are_wrapped():
    from app.core.webai import DeepSeekAdapter

    for js in (DeepSeekAdapter.PROBE, DeepSeekAdapter.SEND, DeepSeekAdapter.NEW_SESSION,
               DeepSeekAdapter.focus_js()):
        assert js.startswith("JSON.stringify((() => {")  # 对象返回损坏的统一绕法
        assert js.rstrip().endswith("})())")


# ---------------------------------------------------------------- 状态机

@pytest.fixture()
def engine(qapp):
    from app.core.webai import WebAIEngine

    eng = WebAIEngine()
    eng._phase = "idle"
    return eng


def test_submit_rejected_when_busy(engine):
    engine._phase = "reading"
    assert engine.submit_text("hi") == -1
    engine._phase = "sending"
    assert engine.submit_text("hi") == -1


def test_submit_accepts_idle_and_ready(engine):
    assert engine.submit_text("hi") > 0        # idle 可提交
    engine._phase = "ready"
    assert engine.submit_text("hi") > 0        # ready（页面已就绪）可连续任务


def test_new_session_noop_without_page(engine):
    engine.new_session()  # 不应抛异常、不应起轮询
    assert engine._poll is None


def test_fail_emits_signal_and_resets(engine, qapp):
    got = []
    engine.failed.connect(lambda msg, tid: got.append((msg, tid)))
    engine._phase = "reading"
    engine._fail("测试失败")
    assert got and "测试失败" in got[0][0]
    assert engine._phase == "idle"


def test_probe_reply_stability_emits_chunk_and_finish(engine, qapp):
    from PySide6.QtCore import QTimer

    got_chunk, got_final = [], []
    engine.chunk.connect(lambda piece, _tid: got_chunk.append(piece))
    engine.finished.connect(lambda text, tid: got_final.append(text))
    engine._phase = "reading"
    engine._start_poll(lambda d: engine._probe_reply(d))

    # 模拟三轮数据：增量两轮 + 稳定两轮 → chunk 增量 + finished
    feed = [
        {"replyText": "你好", "streaming": True},
        {"replyText": "你好世界", "streaming": True},
        {"replyText": "你好世界", "streaming": False},
        {"replyText": "你好世界", "streaming": False},
    ]
    it = iter(feed)

    def drive():
        try:
            engine._probe_reply(next(it))
        except StopIteration:
            pass

    pump = QTimer(engine)
    pump.setInterval(10)
    pump.timeout.connect(drive)
    pump.start()

    from PySide6.QtCore import QDeadlineTimer
    import time
    deadline = time.monotonic() + 3
    while not got_final and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    pump.stop()

    assert got_chunk == ["你好", "世界"]  # 前缀扩展才发增量
    assert got_final == ["你好世界"]
    assert engine._phase == "idle"


def test_probe_reply_non_prefix_resends_full(engine, qapp):
    got_chunk = []
    engine.chunk.connect(lambda piece, _tid: got_chunk.append(piece))
    engine._phase = "reading"
    engine._reply_prev = "旧答案"
    engine._probe_reply({"replyText": "修正后的答案", "streaming": False})
    assert got_chunk == ["修正后的答案"]  # 非前缀扩展全量重发


# ---------------------------------------------------------------- Phase 3：上传/新会话/崩溃恢复

def test_upload_missing_file_emits_fail(engine, qapp):
    got = []
    engine.upload_done.connect(lambda ok, msg: got.append((ok, msg)))
    engine.upload_file(r"Z:\不存在的文件.pdf")
    assert got and got[0][0] is False
    assert engine._phase == "idle"


def test_upload_busy_rejected(engine, qapp):
    got = []
    engine.upload_done.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "reading"
    engine.upload_file("README.md")
    assert got and got[0][0] is False and "忙" in got[0][1]


def test_upload_probe_success(engine, qapp):
    from types import SimpleNamespace

    got = []
    engine.upload_done.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "uploading"
    engine._page = SimpleNamespace(chooser_fired=True, file_to_feed=None,
                                   deleteLater=lambda: None)
    engine._probe_upload({"sendEnabled": True, "inputValue": ""})
    assert got and got[0][0] is True
    assert engine._phase == "idle"
    assert engine._poll is None


def test_upload_probe_timeout_no_chooser(engine, qapp):
    from types import SimpleNamespace
    import time as _t

    got = []
    engine.upload_done.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "uploading"
    engine._page = SimpleNamespace(chooser_fired=False, file_to_feed=None)
    # chooseFiles 3s 宽限已过（deadline 距今 < VERIFY-3）
    engine._upload_deadline = _t.monotonic() + 1
    engine._probe_upload({"sendEnabled": False})
    assert got and got[0][0] is False
    assert engine._phase == "idle"


def test_new_session_success_condition(engine, qapp):
    import time as _t

    from app.core.webai import PAGE_LOAD_TIMEOUT_S

    engine._phase = "loading"
    engine._ns_attempted = False
    engine._ns_deadline = _t.monotonic() + PAGE_LOAD_TIMEOUT_S - 10  # 已过 2s settle
    engine._probe_new_session({"url": "https://chat.deepseek.com/a/chat/s/new-id",
                               "inputVisible": True, "replyText": "",
                               "streaming": False})
    assert engine._phase == "ready"
    assert engine._poll is None


def test_new_session_login_redirect_fails(engine, qapp):
    got_fail, got_login = [], []
    engine.failed.connect(lambda m, t: got_fail.append(m))
    engine.login_required.connect(lambda: got_login.append(True))
    engine._phase = "loading"
    engine._ns_deadline = _t_deadline()
    engine._probe_new_session({"url": "https://chat.deepseek.com/sign_in",
                               "inputVisible": False})
    assert got_login == [True]
    assert got_fail and "未登录" in got_fail[0]
    assert engine._phase == "idle"


def _t_deadline():
    import time
    return time.monotonic() + 30


def test_render_crash_recovers(engine, qapp):
    from types import SimpleNamespace

    got = []
    engine.failed.connect(lambda m, t: got.append(m))
    engine._phase = "reading"
    closed = []
    engine._win = SimpleNamespace(close=lambda: closed.append(1))
    engine._win._allow_close = False
    engine._page = SimpleNamespace(deleteLater=lambda: None)
    engine._on_render_crash(2, 5)
    assert got and "崩溃" in got[0]
    assert engine._phase == "idle"
    assert engine._page is None and engine._win is None
    assert closed == [1]
