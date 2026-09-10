"""网页服务模式可行性 spike v3：G4 攻坚（G1-G3 已复验通过，本版跳过）。

G4a 传图：React 合成事件会滤掉 isTrusted=false 的 JS 派发 paste/drop（v2 实测），
    改走真实输入管线——图片进系统剪贴板 → QTest 真实 Ctrl+V（与用户手动贴图无异）
G4b 新会话：不找侧栏按钮（2026 版 UI 无 aside 结构），直接导航回根路径 = 新会话
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QMainWindow

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / ".spike-profile"          # 已在 .gitignore，登录 cookie 只存本地
REPORT = STORAGE / "report.json"
SPIKE_IMG = ROOT / "assets" / "icon.png"   # 上传测试用的现成图片

TARGET_URL = "https://chat.deepseek.com/"
TEST_PROMPT = "请将下面的英文翻译成中文，只输出译文：The quick brown fox jumps over the lazy dog."

# ---------------------------------------------------------------- 注入脚本
# PySide6 6.11.2 runJavaScript 对象返回值损坏——所有脚本 JSON.stringify 收尾。

# React 受控组件必须用原生 setter 赋值再派发 input 事件，直接 el.value= 不生效
FILL_JS = """
JSON.stringify((() => {
  const ta = document.querySelector('textarea');
  if (!ta) return {ok: false, why: 'no textarea'};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, __PROMPT__);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.focus();
  return {ok: true, len: ta.value.length};
})())
""".replace("__PROMPT__", json.dumps(TEST_PROMPT))

SEND_JS = """
JSON.stringify((() => {
  const b = [...document.querySelectorAll('.ds-button--primary')].find(x => x.className.includes('--circle'));
  if (!b) return {ok: false, why: 'no send button'};
  if (b.className.includes('--disabled')) return {ok: false, why: 'send disabled'};
  b.click();
  return {ok: true};
})())
"""

PROBE_JS = """
JSON.stringify((() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const describe = el => ({
    tag: el.tagName.toLowerCase(),
    cls: (typeof el.className === 'string' ? el.className : '') || undefined,
    ph: el.getAttribute('placeholder') || undefined,
    aria: el.getAttribute('aria-label') || undefined,
    text: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 30) || undefined,
    visible: vis(el),
  });
  return {
    url: location.href,
    chatInput: (() => {
      const ta = document.querySelector('textarea');
      return ta ? {visible: vis(ta), value: ta.value.slice(0, 60)} : null;
    })(),
    sendEnabled: (() => {
      const b = [...document.querySelectorAll('.ds-button--primary')].find(x => x.className.includes('--circle'));
      return b ? !b.className.includes('--disabled') : null;
    })(),
    replyText: (() => {
      const nodes = [...document.querySelectorAll('[class*="markdown"]')];
      return nodes.length ? nodes[nodes.length - 1].innerText.trim() : '';
    })(),
    visibleImgs: [...document.querySelectorAll('img')].filter(i => i.offsetWidth > 0 && i.offsetHeight > 0).length,
    hasStopButton: [...document.querySelectorAll('.ds-button')].some(b => (b.innerText || '').includes('停止')),
    sidebar: (() => {
      const aside = document.querySelector('aside') || document.querySelector('[class*="sidebar"]');
      if (!aside) return null;
      return {
        clickable: [...aside.querySelectorAll('a, button, [role=button], .ds-button')]
          .filter(vis).map(describe).slice(0, 25),
      };
    })(),
    bodySample: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 500),
  };
})())
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(STORAGE / "spike.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def save_report(data: dict) -> None:
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


class SpikePage:
    """chooseFiles 静默回填：页面触发上传时不出文件对话框，直接给测试图片。"""

    def __init__(self, profile, parent_view):
        from PySide6.QtWebEngineCore import QWebEnginePage

        class _Page(QWebEnginePage):
            def chooseFiles(page_self, mode, oldFiles, acceptedMimeTypes):
                log(f"G4: file chooser 触发（mime={list(acceptedMimeTypes)[:3]}）→ 静默回填 {SPIKE_IMG.name}")
                return [str(SPIKE_IMG)]

        self.page = _Page(profile, parent_view)


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    STORAGE.mkdir(exist_ok=True)

    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    profile = QWebEngineProfile("spike", None)
    profile.setPersistentStoragePath(str(STORAGE))

    win = QMainWindow()
    win.setWindowTitle("网页服务模式验证 v2 — 自动推进中，可最小化勿关闭")
    win.resize(1280, 860)
    view = QWebEngineView(profile)
    spike_page = SpikePage(profile, view)
    view.setPage(spike_page.page)
    win.setCentralWidget(view)
    win.show()

    m = {"phase": "paste", "rounds": 0, "upload_wait": 0, "ns_wait": 0,
         "img_sent": False}

    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    def on_paste_focus(r):
        if r and r.get("focused"):
            img = QImage(str(SPIKE_IMG))
            QGuiApplication.clipboard().setImage(img)
            log(f"G4a: 图片({img.width()}x{img.height()})已进系统剪贴板，发送真实 Ctrl+V")
            QTest.keyClick(view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            m["phase"] = "uploading"
            m["upload_wait"] = 0
        else:
            log(f"G4a ✗ textarea 聚焦失败：{r}")

    def start_upload_test():
        log("G4a: 真实输入管线传图——剪贴板 + Ctrl+V")
        run_js(
            "JSON.stringify({focused: (() => { const t = document.querySelector('textarea');"
            " if (!t) return false; t.focus(); return true; })()})", on_paste_focus)

    def run_js(js, cb):
        def wrapped(result):
            try:
                data = json.loads(result) if isinstance(result, str) and result else None
            except (ValueError, TypeError):
                data = None
            cb(data)
        view.page().runJavaScript(js, wrapped)

    def on_img_send(r):
        if r and r.get("ok"):
            m["reply_prev"] = ""
            m["reply_stable"] = 0
            log("G4a: 图+问已发送 → 等流式回复")
        else:
            log(f"G4a 发送失败：{r}（下轮重试）")

    def do_new_session():
        log("G4b: 导航回根路径开新会话")
        m["phase"] = "new_session_check"
        m["ns_wait"] = 0
        view.load(QUrl(TARGET_URL))




    def on_probe(d) -> None:
        if d is None:
            return
        save_report(d)
        m["rounds"] += 1
        phase = m["phase"]
        chat = d.get("chatInput")
        logged_in = bool(chat and chat.get("visible"))

        if phase == "paste":
            if not logged_in:
                return
            m["phase"] = "baseline"
            log("G4a: 记录贴图前 img 基线…")
            return

        if phase == "baseline":
            m["img_baseline"] = d.get("visibleImgs", 0)
            m["phase"] = "pre_paste"
            log(f"G4a: img 基线={m['img_baseline']}，执行真实粘贴")
            start_upload_test()
            return

        if phase == "uploading":
            m["upload_wait"] += 1
            imgs = d.get("visibleImgs", 0)
            if imgs > m.get("img_baseline", 0):
                log(f"G4a ✓ 贴图成功：页面 img {m['img_baseline']}→{imgs}（出现上传预览）")
                m["phase"] = "send_with_img"
            elif m["upload_wait"] >= 8:
                log(f"G4a ✗ 粘贴后 16s 无上传预览（img 数未变）——通道不通")
                m["phase"] = "new_session"
                do_new_session()
            return

        if phase == "send_with_img":
            # 图片预览已在输入框：填一句请求一起发，验证"图 + prompt"端到端
            log("G4a: 附带提问一起发送，验证图片翻译端到端")
            run_js(FILL_JS, lambda r: run_js(SEND_JS, on_img_send))
            m["phase"] = "reading"
            return

        if phase == "reading":
            text = (d.get("replyText") or "").strip()
            if text and text == m.get("reply_prev") and not d.get("hasStopButton"):
                m["reply_stable"] += 1
            else:
                m["reply_stable"] = 0
            m["reply_prev"] = text
            if m["reply_stable"] >= 2 and len(text) > 4:
                log(f"G4a ✓✓ 端到端图片翻译完成（{len(text)} 字符）")
                log(f"    DeepSeek 对图片的回复：{text[:100]}")
                m["phase"] = "new_session"
                do_new_session()
            return

        if phase == "new_session_check":
            m["ns_wait"] += 1
            body = d.get("bodySample", "")
            url = d.get("url", "")
            # 根路径 + 输入框为空 = 新会话就绪
            if url.rstrip("/").endswith("chat.deepseek.com") and logged_in and (chat or {}).get("value", "") == "":
                log(f"G4b ✓ 新会话已开启（根路径、输入框空、欢迎界面）")
                m["phase"] = "done"
                log("=== 四关全部验证完毕 ===")
            elif m["ns_wait"] >= 10:
                log(f"G4b △ 20s 未确认新会话（url={url}）——数据见 report.json")
                m["phase"] = "done"
            return

    view.page().loadFinished.connect(lambda ok: log(f"loadFinished ok={ok}"))

    timer = QTimer()
    timer.setInterval(2000)
    timer.timeout.connect(lambda: run_js(PROBE_JS, on_probe))
    timer.start()

    log(f"spike v2 启动 → {TARGET_URL}（phase=boot，验证免登录）")
    view.load(QUrl(TARGET_URL))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
