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
    profile_deleted = []
    engine._profile = SimpleNamespace(deleteLater=lambda: profile_deleted.append(1))
    engine._page = SimpleNamespace(deleteLater=lambda: None)
    engine._on_render_crash(2, 5)
    assert got and "崩溃" in got[0]
    assert engine._phase == "idle"
    assert engine._page is None and engine._win is None
    assert closed == [1]
    # 旧 Profile 必须一并销毁：下次 _boot 同名 Profile 指向同一持久化目录，
    # 不删会撞 Cookies/leveldb 存储锁，且反复崩溃会累积 Chromium 上下文
    assert profile_deleted == [1]
    assert engine._profile is None


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


# ---------------------------------------------------------------- 2026-10-03 操作逻辑优化

def test_stream_progress_renews_deadline(engine, qapp):
    """流式有新文本必须续期 deadline——长译文不因 120s 总时长被误杀。"""
    import time

    from app.core.webai import STREAM_STALL_S

    engine._phase = "reading"
    engine._reply_prev = "第一段"
    engine._stable = 0
    engine._deadline = time.monotonic() - 1  # 已过期：若不续期下一轮 _tick 即失败
    engine._probe_reply({"replyText": "第一段第二段", "streaming": True})
    assert engine._deadline > time.monotonic() + STREAM_STALL_S - 1  # 已滚动续期


def test_stream_stall_keeps_deadline_on_stable_text(engine, qapp):
    """文本未变（仅稳定计数）不续期——停滞超时语义依赖此。"""
    import time

    engine._phase = "reading"
    engine._reply_prev = "不变"
    engine._stable = 0
    old_deadline = engine._deadline = time.monotonic() + 5
    engine._probe_reply({"replyText": "不变", "streaming": False})
    assert engine._deadline == old_deadline
    assert engine._stable == 1


def test_new_session_success_emits_session_ready(engine, qapp):
    import time as _t

    from app.core.webai import PAGE_LOAD_TIMEOUT_S

    got = []
    engine.session_ready.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "loading"
    engine._ns_attempted = True
    engine._ns_deadline = _t.monotonic() + PAGE_LOAD_TIMEOUT_S - 10
    engine._probe_new_session({"url": "https://chat.deepseek.com/a/chat/s/x",
                               "inputVisible": True, "replyText": "",
                               "streaming": False})
    assert got and got[0][0] is True
    assert "上下文已清空" in got[0][1]


def test_new_session_success_with_attachment_hints(engine, qapp):
    import time as _t

    from app.core.webai import PAGE_LOAD_TIMEOUT_S

    got = []
    engine.session_ready.connect(lambda ok, msg: got.append((ok, msg)))
    engine._session_has_attachment = True
    engine._phase = "loading"
    engine._ns_attempted = True
    engine._ns_deadline = _t.monotonic() + PAGE_LOAD_TIMEOUT_S - 10
    engine._probe_new_session({"url": "https://chat.deepseek.com/a/chat/s/x",
                               "inputVisible": True, "replyText": "",
                               "streaming": False})
    assert got and got[0][0] is True
    assert "附件" in got[0][1]
    assert engine._session_has_attachment is False  # 附件状态已清


def test_new_session_timeout_emits_false(engine, qapp):
    got = []
    engine.session_ready.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "loading"
    engine._ns_attempted = True
    engine._ns_deadline = 0  # 已超时
    engine._probe_new_session({"url": "https://chat.deepseek.com/a/chat/s/old",
                               "inputVisible": True, "replyText": "旧回复",
                               "streaming": False})
    assert got and got[0][0] is False
    assert "未确认" in got[0][1]
    assert engine._phase == "idle"


def test_upload_success_marks_attachment(engine, qapp):
    from types import SimpleNamespace

    got = []
    engine.upload_done.connect(lambda ok, msg: got.append((ok, msg)))
    engine._phase = "uploading"
    engine._page = SimpleNamespace(chooser_fired=True)
    engine._probe_upload({"sendEnabled": True, "inputValue": ""})
    assert got and got[0][0] is True
    assert "新会话" in got[0][1]           # 附件失效提示已带
    assert engine._session_has_attachment is True
    assert engine._phase == "idle"


# ---------------------------------------------------------------- Phase B：tag 透传 / 替换队列 / 静默化

def test_busy_submit_queues_with_replace_semantics(engine, qapp):
    """忙时提交进待发槽；再提交顶掉旧槽（用户永远拿到最新划词的结果）。"""
    engine._phase = "reading"
    assert engine.submit_text("第一条", tag=11) == -1
    assert engine.submit_text("第二条", tag=22) == -1
    assert engine._queued["text"] == "第二条"
    assert engine._queued["tag"] == 22


def test_cleanup_drains_queued_task(engine, qapp):
    """当前任务收尾（fail/finish/upload 完成）自动发出待发槽。"""
    engine._phase = "reading"
    engine.submit_text("排队的", tag=9)
    engine._page = None
    got = []
    engine.failed.connect(lambda m, t: got.append(m))
    engine._fail("当前任务失败")
    # 队列已消费：新任务进入 loading（_submit 被重新驱动）
    assert engine._queued is None
    assert engine._phase == "loading"
    assert engine._pending_text == "排队的"
    assert engine._tag == 9  # 排队任务的 tag 在真正启动时生效


def test_signals_carry_caller_tag(engine, qapp):
    """chunk/finished 透传调用方 tag——上层守卫与引擎内部计数解耦。"""
    from PySide6.QtCore import QTimer

    got_chunk, got_final = [], []
    engine.chunk.connect(lambda piece, tag: got_chunk.append((piece, tag)))
    engine.finished.connect(lambda text, tag: got_final.append((text, tag)))
    engine.submit_text("hi", tag=77)
    assert engine._tag == 77
    engine._phase = "reading"
    engine._start_poll(lambda d: engine._probe_reply(d))
    engine._probe_reply({"replyText": "你好", "streaming": True})
    engine._probe_reply({"replyText": "你好", "streaming": False})
    engine._probe_reply({"replyText": "你好", "streaming": False})
    assert got_chunk and got_chunk[0][1] == 77
    assert got_final and got_final[0][1] == 77


def test_text_task_does_not_present_window(engine, qapp):
    """文本任务静默：不弹大窗（回复走调用方弹窗）；图片任务仍需弹窗贴图。"""
    calls = []
    engine.present_window = lambda: calls.append(1)
    engine._page = object()  # 已有页面：跳过 boot
    engine.submit_text("静默任务")
    assert calls == []
    engine._phase = "idle"
    engine._image_bytes = b"\x89PNG\r\n\x1a\n"
    engine.submit_image(engine._image_bytes, "识别这张图")
    assert calls == [1]


def test_after_paste_hides_window(engine, qapp):
    """贴图完成即收起窗口（回复走 popup，追问再唤回）。"""
    from types import SimpleNamespace

    hidden = []
    engine._win = SimpleNamespace(isVisible=lambda: True, hide=lambda: hidden.append(1))
    engine._page = SimpleNamespace(runJavaScript=lambda js, cb: None)
    engine._phase = "pasting"
    engine._pending_text = "prompt"
    engine._after_paste()
    assert hidden == [1]
    assert engine._phase == "filling"


# ---------------------------------------------------------------- Phase C：登录前置 / 状态

def test_preflight_ready_does_not_dispatch_task(engine, qapp):
    """预检确认就绪时不得派发任务（无 pending 任务却 _do_fill 会填空内容）。"""
    engine._preflight = True
    engine._phase = "loading"
    engine._pending_text = ""
    engine._probe_ensure({"url": "https://chat.deepseek.com/", "inputVisible": True})
    assert engine._phase == "idle"          # 静默就绪，未进 filling
    assert engine._preflight is False
    assert not engine._poll.isActive() if engine._poll else True


def test_preflight_login_branch_no_failed_signal(engine, qapp):
    """预检遇登录页：不发 failed（无任务语义），弹窗+login_required+启动监测。"""
    got_fail, got_login = [], []
    engine.failed.connect(lambda m, t: got_fail.append(m))
    engine.login_required.connect(lambda: got_login.append(True))
    engine.present_window = lambda: None
    engine._preflight = True
    engine._phase = "loading"
    engine._queued = {"text": "x", "image": False, "instruction": "", "tag": 0}
    engine._probe_ensure({"url": "https://chat.deepseek.com/sign_in",
                          "inputVisible": False})
    assert got_fail == []                    # 预检：无任务失败语义
    assert got_login == [True]
    assert engine._queued is None            # 登录未就绪：待发槽一并作废
    assert engine._phase == "idle"
    assert engine._poll is not None and engine._poll.isActive()  # 登录监测已启动


def test_login_watch_confirms_and_emits(engine, qapp):
    got = []
    engine.login_ok.connect(lambda: got.append(True))
    engine._phase = "idle"
    engine._rounds = 2
    engine._start_login_watch()
    engine._probe_login_watch({"url": "https://chat.deepseek.com/a/chat/s/x",
                               "inputVisible": True})
    assert got == [True]
    assert not engine._poll.isActive()      # 监测结束


def test_login_watch_yields_to_task(engine, qapp):
    """登录监测期间用户开始新任务：任务链探测接管，监测静默退位。"""
    got = []
    engine.login_ok.connect(lambda: got.append(True))
    engine._phase = "loading"               # 新任务进行中
    engine._probe_login_watch({"url": "https://chat.deepseek.com/a/chat/s/x",
                               "inputVisible": True})
    assert got == []                        # 不与任务链抢轮询语义


def test_status_ready_rounds_hint(engine, qapp):
    from app.core.webai import INSTRUCTION_REFRESH_N

    got = []
    engine.status_changed.connect(lambda msg: got.append(msg))
    engine._rounds = INSTRUCTION_REFRESH_N
    engine._status_ready()
    assert "建议开新会话" in got[-1]
    engine._rounds = 1
    engine._status_ready()
    assert "会话第 1 轮" in got[-1]
    engine._rounds = 0
    engine._status_ready()
    assert got[-1] == "就绪"


# ---------------------------------------------------------------- Phase D：阈值可配 / 几何

def test_refresh_n_configurable_injection(engine, qapp):
    """refresh_n=2 时：连发 2 条裸文后第 3 条必须重注入指令。"""
    from app.core.webai import WebAIEngine

    eng = WebAIEngine(refresh_n=2)
    eng._phase = "idle"
    eng.present_window = lambda: None
    eng._page = object()
    eng._last_instruction = "翻译成中文"
    eng._instruction = "翻译成中文"
    eng._since_inject = 2  # 已裸发 2 条（= 阈值）
    eng.submit_text("新句子", instruction="翻译成中文")
    assert eng._pending_inject is True
    assert eng._pending_text.startswith("翻译成中文")


def test_set_refresh_n_updates_threshold(engine, qapp):
    engine._refresh_n = 8
    engine.set_refresh_n(3)
    assert engine._refresh_n == 3
    engine.set_refresh_n(3)   # 同值不重复（幂等）
    engine.set_refresh_n(0)   # 越界忽略
    assert engine._refresh_n == 3


def test_preflight_sets_loading_phase(engine, qapp):
    """preflight 必须自己进入 loading——_probe_ensure 的 phase 守卫只认 loading，
    漏置则全部探测被静默吞掉（真机冒烟实锤；早期单测手动铺 loading 掩盖了它）。"""
    booted = []

    def fake_boot():
        booted.append(1)
        engine._page = object()
    engine._boot = fake_boot
    engine._phase = "idle"
    engine.preflight_login()
    assert engine._phase == "loading"
    assert booted == [1]
    # 已有页面分支同样必须进入 loading
    engine._phase = "ready"
    engine.preflight_login()
    assert engine._phase == "loading"


# ---------------------------------------------------------------- 交叉评审修复回归（Codex 2026-10-03）

def test_busy_submit_does_not_pollute_inflight_instruction(engine, qapp):
    """A 在途时排队 B：B 的指令不得覆盖 A 的（否则 _on_sent 错记 B 指令、
    后续裸发判断错乱）——排队只存参数，不动共享字段。"""
    engine._phase = "sending"          # A 在途（指令已装配）
    engine._instruction = "A 的翻译指令"
    engine._pending_inject = True
    engine._pending_text = "A 的翻译指令\nA 原文"
    engine.submit_text("B 原文", instruction="B 的解释指令", tag=9)
    assert engine._instruction == "A 的翻译指令"       # 在途字段未被污染
    assert engine._queued["instruction"] == "B 的解释指令"  # 队列存的是 B 自己的


def test_busy_image_submit_keeps_inflight_bytes(engine, qapp):
    """A 贴图在途时排队 B 图片：B 的字节不得覆盖 A 的（否则 A 贴出 B 的图）。"""
    engine._phase = "pasting"
    engine._image_bytes = b"A-bytes"
    engine.submit_image(b"B-bytes", "B prompt", tag=8)
    assert engine._image_bytes == b"A-bytes"
    assert engine._queued["image_bytes"] == b"B-bytes"


def test_login_watch_deadline_renewed(engine, qapp):
    """登录监测启动必须刷新通用 deadline——旧任务遗留的过期 deadline
    会在首轮 _tick 把监测当任务超时杀掉（不发 login_ok、误发 failed）。"""
    import time

    engine._phase = "idle"
    engine._deadline = time.monotonic() - 10  # 模拟旧任务遗留的过期值
    engine._start_login_watch()
    assert engine._deadline == engine._login_watch_deadline
    assert engine._deadline > time.monotonic()


def test_new_session_renews_deadline_and_drains(engine, qapp):
    """闲置过期后再开新会话：deadline 刷新不被 _tick 误杀；超时收尾发出待发任务。"""
    import time

    from types import SimpleNamespace

    engine._page = SimpleNamespace(load=lambda url: None, runJavaScript=lambda js, cb: None)
    engine._phase = "idle"
    engine._deadline = time.monotonic() - 10
    engine._queued = {"text": "排队任务", "image": False, "tag": 5,
                      "instruction": "", "image_bytes": None}
    assert engine.new_session() is True
    assert engine._deadline == engine._ns_deadline  # 已刷新
    engine._ns_deadline = 0  # 直接触发超时分支
    engine._probe_new_session({"url": "https://chat.deepseek.com/a/chat/s/old",
                               "inputVisible": True, "replyText": "旧", "streaming": False})
    assert engine._queued is None      # 超时收尾 drain：待发任务已发出
    assert engine._phase == "loading"  # drain 的新任务进入 loading


def test_preflight_ready_drains_queued(engine, qapp):
    """预检就绪分支也要 drain（监测期间用户划的词不滞留）。"""
    engine._preflight = True
    engine._phase = "loading"
    engine._page = object()
    engine._queued = {"text": "x", "image": False, "tag": 3,
                      "instruction": "", "image_bytes": None}
    engine._probe_ensure({"url": "https://chat.deepseek.com/", "inputVisible": True})
    assert engine._queued is None
    assert engine._phase == "loading"  # drain 的新任务已启动


def test_cleanup_clears_preflight_flag(engine, qapp):
    """预检异常收尾必须清 _preflight——残留会把下一条真实任务静默吞掉。"""
    engine._preflight = True
    engine._phase = "loading"
    engine._fail("预检中途超时")
    assert engine._preflight is False


def test_fail_keeps_login_status_when_watching(engine, qapp):
    """登录监测中的任务失败：状态保持「未登录」，不刷成误导性的「就绪」。"""
    got = []
    engine.status_changed.connect(lambda s: got.append(s))
    engine._login_watching = True
    engine._phase = "loading"
    engine._fail("网页版未登录——请在弹出的窗口中登录后再试")
    assert "未登录" in got[-1]
    assert "就绪" not in got[-1]


def test_negative_tag_is_preserved_as_sentinel_free(engine, qapp):
    """文档化回归：tag=-1 是真值会被原样采用——main 侧已改为 _WEBAI_TAG_BASE
    分配唯一正号，不再依赖 show_translation(request=False) 的 -1。引擎侧
    保证非零 tag 一律透传（含负数也不覆盖，便于将来哨兵语义）。"""
    engine._phase = "idle"
    engine._page = object()
    engine.submit_text("x", tag=1_000_001)
    assert engine._tag == 1_000_001


# ---------------------------------------------------------------- 复审第二轮修复回归

def test_login_branch_notifies_cancelled_queued_tag(engine, qapp):
    """A 在途 + B 排队时撞登录页：B（最新弹窗在等的）必须收到终止信号，
    不能只对 A 发 failed——B 的弹窗会永远转圈。"""
    got = []
    engine.failed.connect(lambda m, t: got.append((m, t)))
    engine.present_window = lambda: None
    engine._phase = "loading"
    engine._preflight = False
    engine._tag = 1_000_004                 # A 的 tag（在途）
    engine._queued = {"text": "B", "image": False, "tag": 1_000_005,
                      "instruction": "", "image_bytes": None}
    engine._probe_ensure({"url": "https://chat.deepseek.com/sign_in",
                          "inputVisible": False})
    tags = [t for _, t in got]
    assert 1_000_004 in tags and 1_000_005 in tags   # A 失败 + B 取消都发了
    b_msg = [m for m, t in got if t == 1_000_005][0]
    assert "取消" in b_msg


def test_render_crash_clears_preflight_flag(engine, qapp):
    """预检期间渲染崩溃：_preflight 必须清——残留会把 drain 出的
    下一条真实任务当预检静默吞掉（复现实锤过 phase=idle 任务没发出）。"""
    from types import SimpleNamespace

    engine._preflight = True
    engine._login_watching = True
    engine._phase = "loading"
    engine._win = SimpleNamespace(close=lambda: None, _allow_close=False)
    engine._profile = SimpleNamespace(deleteLater=lambda: None)
    engine._page = SimpleNamespace(deleteLater=lambda: None)
    engine._on_render_crash(2, 5)
    assert engine._preflight is False
    assert engine._login_watching is False


def test_tick_timeout_ns_emits_session_ready_not_failed(engine, qapp):
    """新会话超时走真实 _tick：必须报 session_ready(False)，不得向旧任务
    tag 发 failed（旧测试直接调 probe 绕过了 _tick，没覆盖到）。"""
    import time

    from types import SimpleNamespace

    got_ready, got_fail = [], []
    engine.session_ready.connect(lambda ok, msg: got_ready.append(ok))
    engine.failed.connect(lambda m, t: got_fail.append(m))
    engine._phase = "loading"
    engine._task = 3
    engine._tag = 1_000_002  # 上一任务遗留 tag：超时不得发给它
    engine._page = SimpleNamespace(load=lambda u: None, runJavaScript=lambda js, cb: None)
    assert engine.new_session() is True
    engine._deadline = time.monotonic() - 1  # 强制 _tick 超时路径
    engine._tick(engine._probe_new_session)
    assert got_ready == [False]
    assert got_fail == []                    # 没有误发任务失败


def test_tick_timeout_login_watch_silent(engine, qapp):
    """登录监测超时走真实 _tick：静默结束（不发 failed），标志清除。"""
    import time

    got_fail = []
    engine.failed.connect(lambda m, t: got_fail.append(m))
    engine._phase = "idle"
    engine._login_watching = True
    engine._start_login_watch()
    engine._deadline = time.monotonic() - 1
    engine._tick(engine._probe_login_watch)
    assert got_fail == []
    assert engine._login_watching is False
    assert not engine._poll.isActive()


def test_submit_takes_over_login_watch_flag(engine, qapp):
    """登录监测期间新任务提交：监测标志必须终结（否则任务失败被误报成未登录）。"""
    engine._login_watching = True
    engine._phase = "idle"
    engine._page = object()
    engine.submit_text("新任务", tag=1_000_009)
    assert engine._login_watching is False
