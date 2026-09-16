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
    # 单测不把承载窗口弹到前台（_submit 现在会触发 present_window 抢焦点）
    eng.present_window = lambda: None
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


def test_probe_reply_non_prefix_emits_nothing(engine, qapp):
    """非前缀扩展（站点重排/修正）不 emit——popup 只会追加渲染，全量重发
    会拼出脏文本；保持旧文不动，等 finished 全量覆盖纠正。"""
    got_chunk = []
    engine.chunk.connect(lambda piece, _tid: got_chunk.append(piece))
    engine._phase = "reading"
    engine._reply_prev = "旧答案"
    engine._probe_reply({"replyText": "修正后的答案", "streaming": False})
    assert got_chunk == []                      # 不发增量
    assert engine._reply_prev == "修正后的答案"  # 内部状态已跟上，finished 会全量纠正


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


# ---------------------------------------------------------------- review 修复回归

def test_is_busy_reflects_phase(engine):
    engine._phase = "idle"
    assert engine.is_busy is False
    engine._phase = "ready"
    assert engine.is_busy is False
    for phase in ("loading", "filling", "sending", "reading", "pasting", "uploading"):
        engine._phase = phase
        assert engine.is_busy is True


def test_fail_routes_upload_to_upload_done(engine, qapp):
    """上传流程失败必须走 upload_done——failed 会被 popup 任务号守卫丢弃。"""
    got_upload, got_failed = [], []
    engine.upload_done.connect(lambda ok, msg: got_upload.append((ok, msg)))
    engine.failed.connect(lambda m, t: got_failed.append(m))
    engine._phase = "uploading"          # 上传中被 _tick 超时
    engine._fail("任务超时（120s）")
    assert got_upload == [(False, "任务超时（120s）")]
    assert got_failed == []
    engine._phase = "reading"            # 普通翻译失败仍走 failed
    engine._fail("翻译失败原因")
    assert len(got_failed) == 1 and got_upload == [(False, "任务超时（120s）")]


def test_fail_routes_pending_upload_even_in_loading(engine, qapp):
    """upload 已排队（pending）但还没进 uploading 阶段就失败——同样分流。"""
    got_upload = []
    engine.upload_done.connect(lambda ok, msg: got_upload.append((ok, msg)))
    engine._upload_pending = True
    engine._phase = "loading"
    engine._fail("网页加载失败")
    assert got_upload == [(False, "网页加载失败")]
    assert engine._upload_pending is False


def test_new_session_returns_false_when_not_booted(engine):
    from app.core.webai import WebAIEngine

    assert WebAIEngine().new_session() is False


# ---------------------------------------------------------------- 指令智能注入

def test_instruction_injected_on_first_task(engine):
    engine.submit_text("hello", instruction="请翻译成中文")
    assert engine._pending_inject is True
    assert engine._pending_text == "请翻译成中文\nhello"


def test_same_instruction_second_task_sends_bare(engine):
    # 模拟首轮已发送成功（_on_sent 的推进效果）
    engine._last_instruction = "请翻译成中文"
    engine._since_inject = 0
    engine.submit_text("world", instruction="请翻译成中文")
    assert engine._pending_inject is False
    assert engine._pending_text == "world"


def test_changed_instruction_reinjects(engine):
    engine._last_instruction = "请翻译成中文"
    engine._since_inject = 0
    engine.submit_text("backprop", instruction="解释术语")
    assert engine._pending_inject is True
    assert engine._pending_text.startswith("解释术语\n")


def test_refresh_interval_forces_reinject(engine):
    from app.core.webai import INSTRUCTION_REFRESH_N

    engine._last_instruction = "请翻译成中文"
    engine._since_inject = INSTRUCTION_REFRESH_N  # 裸发次数到顶：强制重注入
    engine.submit_text("hello", instruction="请翻译成中文")
    assert engine._pending_inject is True


def test_no_instruction_always_full_payload(engine):
    # instruction 为空（术语解释等 {text} 在中部的模板）＝ 调用方全量拼好
    engine._last_instruction = "请翻译成中文"
    engine.submit_text("解释「术语」…")
    assert engine._pending_inject is False
    assert engine._pending_text == "解释「术语」…"


def test_sent_success_advances_injection_state(engine):
    engine._pending_inject = True
    engine._instruction = "请翻译成中文"
    engine._phase = "sending"
    engine._on_sent({"ok": True})
    assert engine._last_instruction == "请翻译成中文"
    assert engine._since_inject == 0
    # 裸发成功：计数 +1
    engine._pending_inject = False
    engine._phase = "sending"
    engine._on_sent({"ok": True})
    assert engine._since_inject == 1


def test_new_session_resets_instruction_state(engine):
    from unittest.mock import MagicMock

    engine._page = MagicMock()
    engine._last_instruction = "请翻译成中文"
    engine._since_inject = 3
    engine.new_session()
    assert engine._last_instruction == ""
    assert engine._since_inject == 0


# ---------------------------------------------------------------- 剪贴板串行 worker（主线程零 Win32 剪贴板调用）

def test_clipboard_worker_serial_and_signal_back_to_main(qapp):
    """job 在后台线程执行、结果经 done 信号排队回主线程、顺序保序。"""
    import threading
    import time

    from PySide6.QtCore import QThread

    from app.core.webai import _ClipboardWorker

    worker = _ClipboardWorker()
    got = []
    worker.done.connect(lambda jid, result: got.append((jid, result, QThread.currentThread())))

    main_thread = QThread.currentThread()
    worker.submit("save", lambda: [(13, "文本")])
    worker.submit("restore", lambda: None)

    deadline = time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert [g[0] for g in got] == ["save", "restore"]       # 队列保序
    assert got[0][1] == [(13, "文本")]                      # 结果透传
    assert all(g[2] is main_thread for g in got)            # 信号落主线程


def test_clipboard_worker_survives_job_exception(qapp):
    """job 抛异常不杀 worker：结果为 None，后续 job 照常。"""
    import time

    from app.core.webai import _ClipboardWorker

    worker = _ClipboardWorker()
    got = []
    worker.done.connect(lambda jid, result: got.append((jid, result)))
    worker.submit("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    worker.submit("good", lambda: "ok")

    deadline = time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert got == [("bad", None), ("good", "ok")]


def test_on_clip_saved_phase_guard(engine, qapp):
    """快照回调到达时任务已被作废/失败：不得再动剪贴板/窗口（无 _win 也不炸）。"""
    engine._phase = "idle"          # 非 pasting：守卫直接返回
    engine._paste_image = None
    engine._on_clip_saved([(13, "文本")])   # 不应抛异常（_win 为 None）
    assert engine._clip_saved is None       # 快照未被采用


def test_settle_clipboard_delegates_to_worker(engine, qapp):
    """恢复走串行 worker（异步），立即清 _clip_saved 防重复恢复。"""
    submitted = []
    engine._clip_worker.submit = lambda jid, fn: submitted.append(jid)
    engine._clip_saved = [(13, "文本")]
    engine._settle_clipboard()
    assert submitted == ["restore"]
    assert engine._clip_saved is None
    # 无快照时不提交
    engine._settle_clipboard()
    assert submitted == ["restore"]
