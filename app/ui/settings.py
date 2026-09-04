"""设置界面：左分类 + 右表单。保存后发 config_saved 信号（完整配置对象）。"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import PROVIDER_PRESETS
from app.ui.theme import build_qss, palette

# edge-tts 常用音色（中英各留几个，够用不堆全表）
VOICE_ZH = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunyangNeural",
    "zh-CN-XiaoyiNeural", "zh-TW-HsiaoChenNeural",
]
VOICE_EN = [
    "en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural",
    "en-GB-SoniaNeural", "en-US-RyanNeural",
]
RATES = ["-30%", "-15%", "+0%", "+15%", "+30%"]


class SettingsDialog(QDialog):
    config_saved = Signal(dict)

    def __init__(self, cfg: dict, translator, parent=None):
        super().__init__(parent)
        self._orig = copy.deepcopy(cfg)
        self.cfg = copy.deepcopy(cfg)
        self._translator = translator
        self.setWindowTitle("CtrlTranslate 设置")
        self.resize(760, 540)
        self._build_ui()
        self._apply_theme()

    # ---------------------------------------------------------------- 布局

    def _build_ui(self) -> None:
        root = QHBoxLayout()  # 不带 parent，最后统一 setLayout
        self.pages = QStackedWidget()

        self.nav = QListWidget()
        for key, label in [
            ("provider", "翻译服务"),
            ("tts", "语音播报"),
            ("trigger", "触发与取词"),
            ("popup", "弹窗外观"),
            ("data", "历史与数据"),
        ]:
            self.nav.addItem(label)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setFixedWidth(150)

        self.pages.addWidget(self._page_provider())
        self.pages.addWidget(self._page_tts())
        self.pages.addWidget(self._page_trigger())
        self.pages.addWidget(self._page_popup())
        self.pages.addWidget(self._page_data())

        root.addWidget(self.nav)
        root.addWidget(self.pages, 1)

        btns = QHBoxLayout()
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary")
        btn_save.clicked.connect(self._save)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)

        wrap = QVBoxLayout()
        wrap.addLayout(root, 1)
        wrap.addLayout(btns)
        self.setLayout(wrap)

    def _page(self, title: str) -> tuple[QFormLayout, QWidget]:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("sectionTitle")
        form.addRow(head)
        return form, page

    # ---------------------------------------------------------------- 各页

    def _page_provider(self) -> QWidget:
        p = self.cfg["provider"]
        form, page = self._page("翻译服务（OpenAI 兼容）")

        self.cb_preset = QComboBox()
        for key, preset in PROVIDER_PRESETS.items():
            label = preset["label"] + ("  · 需代理" if preset["needs_proxy"] else "")
            self.cb_preset.addItem(label, key)
        current = p.get("name", "zhipu")
        self.cb_preset.setCurrentIndex(max(0, list(PROVIDER_PRESETS).index(current)))
        self.cb_preset.currentIndexChanged.connect(self._on_preset_changed)

        self.ed_base_url = QLineEdit(p["base_url"])
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)
        preset = PROVIDER_PRESETS[current]
        self.cb_model.addItems(preset["models"] or [p["model"]])
        self.cb_model.setCurrentText(p["model"])

        self.ed_api_key = QLineEdit(p["api_key"])
        self.ed_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_eye = QPushButton("显示")
        self.btn_eye.setCheckable(True)
        self.btn_eye.setFixedWidth(52)
        self.btn_eye.toggled.connect(
            lambda on: self.ed_api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.ed_api_key, 1)
        key_row.addWidget(self.btn_eye)

        self.btn_test = QPushButton("测试连接")
        self.lbl_test = QLabel("")
        self.lbl_test.setObjectName("dim")
        self.btn_test.clicked.connect(self._test_connection)
        test_row = QHBoxLayout()
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.lbl_test, 1)

        self.rb_study = QRadioButton("学习模式（推荐读文献）")
        self.rb_concise = QRadioButton("简洁模式")
        mode = self.cfg["translate"].get("mode", "study")
        (self.rb_study if mode == "study" else self.rb_concise).setChecked(True)
        lbl_mode_hint = QLabel("学习模式输出译文 + 专业术语表；简洁模式仅输出译文")
        lbl_mode_hint.setObjectName("dim")

        self.ed_prompt = QPlainTextEdit(self.cfg["translate"].get("custom_prompt", ""))
        self.ed_prompt.setPlaceholderText("留空使用内置 prompt；可自定义，用 {text} 代表原文")
        self.ed_prompt.setFixedHeight(72)

        # ---- 备用服务（fallback）----
        fb = p.get("fallback", {})

        self.cb_fb_preset = QComboBox()
        for key, preset in PROVIDER_PRESETS.items():
            label = preset["label"] + ("  · 需代理" if preset["needs_proxy"] else "")
            self.cb_fb_preset.addItem(label, key)
        self.cb_fb_preset.setCurrentIndex(0)
        self.cb_fb_preset.currentIndexChanged.connect(self._on_fb_preset_changed)

        self.ed_fb_url = QLineEdit(fb.get("base_url", ""))
        self.cb_fb_model = QComboBox()
        self.cb_fb_model.setEditable(True)
        self.cb_fb_model.addItems(PROVIDER_PRESETS["zhipu"]["models"])
        self.cb_fb_model.setCurrentText(fb.get("model", ""))

        self.ed_fb_key = QLineEdit(fb.get("api_key", ""))
        self.ed_fb_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_fb_eye = QPushButton("显示")
        self.btn_fb_eye.setCheckable(True)
        self.btn_fb_eye.setFixedWidth(52)
        self.btn_fb_eye.toggled.connect(
            lambda on: self.ed_fb_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        fb_key_row = QHBoxLayout()
        fb_key_row.addWidget(self.ed_fb_key, 1)
        fb_key_row.addWidget(self.btn_fb_eye)

        self.btn_fb_test = QPushButton("测试备用")
        self.lbl_fb_test = QLabel("")
        self.lbl_fb_test.setObjectName("dim")
        self.btn_fb_test.clicked.connect(self._test_fallback)
        fb_test_row = QHBoxLayout()
        fb_test_row.addWidget(self.btn_fb_test)
        fb_test_row.addWidget(self.lbl_fb_test, 1)

        lbl_fb_hint = QLabel("主服务失败（网络 / 限流 / Key 失效）时自动用备用服务重试；三项填写完整后启用")
        lbl_fb_hint.setObjectName("dim")
        fb_head = QLabel("备用服务（可选）")
        fb_head.setObjectName("sectionTitle")

        form.addRow("服务商预设", self.cb_preset)
        form.addRow("API 地址", self.ed_base_url)
        form.addRow("模型", self.cb_model)
        form.addRow("API Key", _wrap_h(key_row))
        form.addRow("", _wrap_h(test_row))
        form.addRow("翻译模式", self.rb_study)
        form.addRow("", self.rb_concise)
        form.addRow("", lbl_mode_hint)
        form.addRow("自定义 Prompt", self.ed_prompt)
        form.addRow(fb_head)
        form.addRow("预设（填表模板）", self.cb_fb_preset)
        form.addRow("备用 API 地址", self.ed_fb_url)
        form.addRow("备用模型", self.cb_fb_model)
        form.addRow("备用 API Key", _wrap_h(fb_key_row))
        form.addRow("", _wrap_h(fb_test_row))
        form.addRow("", lbl_fb_hint)
        return _scroll(page)

    def _page_tts(self) -> QWidget:
        t = self.cfg["tts"]
        c = t.get("custom", {})
        form, page = self._page("语音播报（TTS）")

        self.ck_tts = QCheckBox("启用语音播报")
        self.ck_tts.setChecked(t.get("enabled", True))

        self.cb_engine = QComboBox()
        self.cb_engine.addItem("自动（在线优先，失败转系统语音）", "auto")
        self.cb_engine.addItem("仅 edge-tts 在线语音", "edge")
        self.cb_engine.addItem("仅系统语音（离线）", "sapi")
        self.cb_engine.addItem("自定义（OpenAI 兼容 TTS）", "custom")
        _select_combo(self.cb_engine, t.get("engine", "auto"))

        self.cb_voice_zh = QComboBox()
        self.cb_voice_zh.setEditable(True)
        self.cb_voice_zh.addItems(VOICE_ZH)
        self.cb_voice_zh.setCurrentText(t.get("voice_zh", VOICE_ZH[0]))
        self.cb_voice_en = QComboBox()
        self.cb_voice_en.setEditable(True)
        self.cb_voice_en.addItems(VOICE_EN)
        self.cb_voice_en.setCurrentText(t.get("voice_en", VOICE_EN[0]))

        # 自定义 TTS（OpenAI 兼容 /audio/speech）：地址 / 模型 / 音色 / Key
        self.ed_tts_url = QLineEdit(c.get("base_url", ""))
        self.ed_tts_url.setPlaceholderText("如 https://api.siliconflow.cn/v1")
        self.ed_tts_model = QLineEdit(c.get("model", ""))
        self.ed_tts_model.setPlaceholderText("如 FunAudioLLM/CosyVoice2-0.5B、tts-1")
        self.ed_tts_voice = QLineEdit(c.get("voice", ""))
        self.ed_tts_voice.setPlaceholderText("如 alloy（留空 = 服务端默认音色）")
        self.ed_tts_key = QLineEdit(c.get("api_key", ""))
        self.ed_tts_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_tts_eye = QPushButton("显示")
        self.btn_tts_eye.setCheckable(True)
        self.btn_tts_eye.setFixedWidth(52)
        self.btn_tts_eye.toggled.connect(
            lambda on: self.ed_tts_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        tts_key_row = QHBoxLayout()
        tts_key_row.addWidget(self.ed_tts_key, 1)
        tts_key_row.addWidget(self.btn_tts_eye)

        self.cb_rate = QComboBox()
        self.cb_rate.addItems(RATES)
        self.cb_rate.setCurrentText(t.get("rate", "+0%"))

        self.ck_autoplay = QCheckBox("翻译完成后自动播报")
        self.ck_autoplay.setChecked(t.get("auto_play", False))
        self.cb_autoplay_what = QComboBox()
        self.cb_autoplay_what.addItem("播报英文原文", "source")
        self.cb_autoplay_what.addItem("播报中文译文", "translated")
        _select_combo(self.cb_autoplay_what, t.get("auto_play_what", "source"))
        self.ck_tts.toggled.connect(self._sync_tts_enabled)
        self.cb_engine.currentIndexChanged.connect(
            lambda _i: self._sync_engine_fields(form))
        for w in (self.cb_engine, self.cb_voice_zh, self.cb_voice_en, self.cb_rate,
                  self.ed_tts_url, self.ed_tts_model, self.ed_tts_voice, self.ed_tts_key,
                  self.ck_autoplay, self.cb_autoplay_what):
            w.setEnabled(self.ck_tts.isChecked())

        form.addRow("", self.ck_tts)
        form.addRow("合成引擎", self.cb_engine)
        form.addRow("中文音色（读译文）", self.cb_voice_zh)
        form.addRow("英文音色（读原文）", self.cb_voice_en)
        form.addRow("TTS API 地址", self.ed_tts_url)
        form.addRow("TTS 模型", self.ed_tts_model)
        form.addRow("TTS 音色", self.ed_tts_voice)
        form.addRow("TTS API Key", _wrap_h(tts_key_row))
        form.addRow("语速", self.cb_rate)
        form.addRow("", self.ck_autoplay)
        form.addRow("自动播报内容", self.cb_autoplay_what)
        self._sync_engine_fields(form)  # 按当前引擎初始化显隐
        return _scroll(page)

    def _sync_engine_fields(self, form: QFormLayout) -> None:
        """custom 引擎显示地址/模型/音色/Key，隐藏 edge 音色；其余引擎反之。"""
        custom = self.cb_engine.currentData() == "custom"
        for w in (self.ed_tts_url, self.ed_tts_model, self.ed_tts_voice, self.ed_tts_key):
            form.setRowVisible(w, custom)
        for w in (self.cb_voice_zh, self.cb_voice_en):
            form.setRowVisible(w, not custom)

    def _sync_tts_enabled(self) -> None:
        on = self.ck_tts.isChecked()
        for w in (self.cb_engine, self.cb_voice_zh, self.cb_voice_en, self.cb_rate,
                  self.ed_tts_url, self.ed_tts_model, self.ed_tts_voice, self.ed_tts_key,
                  self.btn_tts_eye, self.ck_autoplay, self.cb_autoplay_what):
            w.setEnabled(on)

    def _page_trigger(self) -> QWidget:
        tr, cap = self.cfg["trigger"], self.cfg["capture"]
        form, page = self._page("触发与取词")

        self.ck_hotkey = QCheckBox("启用双击划词翻译")
        self.ck_hotkey.setChecked(tr.get("enabled", True))

        self.cb_key = QComboBox()
        self.cb_key.addItem("双击 Ctrl", "ctrl")
        self.cb_key.addItem("双击 Alt", "alt")
        self.cb_key.addItem("双击 Shift", "shift")
        _select_combo(self.cb_key, tr.get("key", "ctrl"))

        self.sl_interval = QSlider(Qt.Horizontal)
        self.sl_interval.setRange(150, 600)
        self.sl_interval.setValue(tr.get("interval_ms", 300))
        self.lbl_interval = QLabel(f"{self.sl_interval.value()} ms")
        self.sl_interval.valueChanged.connect(lambda v: self.lbl_interval.setText(f"{v} ms"))

        self.ck_uia = QCheckBox("优先用 UIA 直接读取选中（不动剪贴板；失败自动改用复制法）")
        self.ck_uia.setChecked(cap.get("prefer_uia", True))

        self.sp_clip_wait = QSpinBox()
        self.sp_clip_wait.setRange(100, 1500)
        self.sp_clip_wait.setSingleStep(50)
        self.sp_clip_wait.setSuffix(" ms")
        self.sp_clip_wait.setValue(cap.get("clipboard_wait_ms", 400))

        self.sp_max_chars = QSpinBox()
        self.sp_max_chars.setRange(100, 20000)
        self.sp_max_chars.setSingleStep(100)
        self.sp_max_chars.setValue(self.cfg["translate"].get("max_chars", 3000))

        form.addRow("", self.ck_hotkey)
        form.addRow("触发键", self.cb_key)
        form.addRow("双击判定间隔", _hbox(self.sl_interval, self.lbl_interval))
        form.addRow("", self.ck_uia)
        form.addRow("复制法等待上限", self.sp_clip_wait)
        form.addRow("单次翻译长度上限（字符）", self.sp_max_chars)
        return _scroll(page)

    def _page_popup(self) -> QWidget:
        po = self.cfg["popup"]
        form, page = self._page("弹窗外观")

        self.cb_theme = QComboBox()
        self.cb_theme.addItem("深色", "dark")
        self.cb_theme.addItem("浅色", "light")
        _select_combo(self.cb_theme, po.get("theme", "dark"))

        self.sp_font = QSpinBox()
        self.sp_font.setRange(11, 22)
        self.sp_font.setValue(po.get("font_size", 14))

        self.sl_opacity = QSlider(Qt.Horizontal)
        self.sl_opacity.setRange(50, 100)
        self.sl_opacity.setValue(int(po.get("opacity", 0.96) * 100))
        self.lbl_opacity = QLabel(f"{self.sl_opacity.value()}%")
        self.sl_opacity.valueChanged.connect(lambda v: self.lbl_opacity.setText(f"{v}%"))

        self.sp_width = QSpinBox()
        self.sp_width.setRange(320, 900)
        self.sp_width.setSingleStep(20)
        self.sp_width.setValue(po.get("width", 480))

        self.sp_autoclose = QSpinBox()
        self.sp_autoclose.setRange(0, 120)
        self.sp_autoclose.setSpecialValueText("不自动关闭")
        self.sp_autoclose.setSuffix(" 秒")
        self.sp_autoclose.setValue(po.get("auto_close_s", 0))

        form.addRow("主题", self.cb_theme)
        form.addRow("正文字号", self.sp_font)
        form.addRow("不透明度", _hbox(self.sl_opacity, self.lbl_opacity))
        form.addRow("弹窗宽度", self.sp_width)
        form.addRow("自动关闭", self.sp_autoclose)
        return _scroll(page)

    def _page_data(self) -> QWidget:
        g = self.cfg["general"]
        form, page = self._page("历史与数据")

        self.ck_history = QCheckBox("保存翻译历史（生词本不受影响）")
        self.ck_history.setChecked(g.get("history_enabled", True))

        self.btn_open_dir = QPushButton("打开数据目录")
        self.btn_open_dir.clicked.connect(self._open_data_dir)

        self.lbl_paths = QLabel("配置、数据库与日志均存于 ~/.ctrltrans/")
        self.lbl_paths.setObjectName("dim")

        form.addRow("", self.ck_history)
        form.addRow("数据位置", self.btn_open_dir)
        form.addRow("", self.lbl_paths)
        return _scroll(page)

    # ---------------------------------------------------------------- 行为

    def _on_preset_changed(self, index: int) -> None:
        key = self.cb_preset.currentData()
        preset = PROVIDER_PRESETS.get(key)
        if not preset:
            return
        self.ed_base_url.setText(preset["base_url"])
        self.cb_model.clear()
        self.cb_model.addItems(preset["models"] or [])
        self.cb_model.setCurrentText(preset["model"])

    def _on_fb_preset_changed(self, index: int) -> None:
        preset = PROVIDER_PRESETS.get(self.cb_fb_preset.currentData())
        if not preset:
            return
        self.ed_fb_url.setText(preset["base_url"])
        self.cb_fb_model.clear()
        self.cb_fb_model.addItems(preset["models"] or [])
        self.cb_fb_model.setCurrentText(preset["model"])

    def _test_fallback(self) -> None:
        endpoint = (
            self.ed_fb_url.text().strip(),
            self.ed_fb_key.text().strip(),
            self.cb_fb_model.currentText().strip(),
        )
        self.lbl_fb_test.setText("测试中…")
        self.btn_fb_test.setEnabled(False)

        def ok(msg):
            self.lbl_fb_test.setText(msg)
            self.lbl_fb_test.setStyleSheet(f"color: {palette(self.cfg['popup']['theme'])['accent']}")
            self.btn_fb_test.setEnabled(True)

        def fail(msg):
            self.lbl_fb_test.setText(msg)
            self.lbl_fb_test.setStyleSheet(f"color: {palette(self.cfg['popup']['theme'])['error']}")
            self.btn_fb_test.setEnabled(True)

        self._translator.test_connection(ok, fail, endpoint=endpoint)

    def _test_connection(self) -> None:
        self._collect_into(self.cfg)  # 用当前表单值测试
        self.lbl_test.setText("测试中…")
        self.btn_test.setEnabled(False)

        def ok(msg):
            self.lbl_test.setText(msg)
            self.lbl_test.setStyleSheet(f"color: {palette(self.cfg['popup']['theme'])['accent']}")
            self.btn_test.setEnabled(True)

        def fail(msg):
            self.lbl_test.setText(msg)
            self.lbl_test.setStyleSheet(f"color: {palette(self.cfg['popup']['theme'])['error']}")
            self.btn_test.setEnabled(True)

        self._translator.test_connection(ok, fail)

    def _open_data_dir(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        from app.config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

    def _collect_into(self, cfg: dict) -> dict:
        p = cfg["provider"]
        p["name"] = self.cb_preset.currentData() or "custom"
        p["base_url"] = self.ed_base_url.text().strip()
        p["model"] = self.cb_model.currentText().strip()
        p["api_key"] = self.ed_api_key.text().strip()
        p["fallback"] = {
            "base_url": self.ed_fb_url.text().strip(),
            "model": self.cb_fb_model.currentText().strip(),
            "api_key": self.ed_fb_key.text().strip(),
        }

        cfg["translate"]["mode"] = "study" if self.rb_study.isChecked() else "concise"
        cfg["translate"]["custom_prompt"] = self.ed_prompt.toPlainText().strip()
        cfg["translate"]["max_chars"] = self.sp_max_chars.value()

        t = cfg["tts"]
        t["enabled"] = self.ck_tts.isChecked()
        t["engine"] = self.cb_engine.currentData()
        t["voice_zh"] = self.cb_voice_zh.currentText().strip()
        t["voice_en"] = self.cb_voice_en.currentText().strip()
        t["rate"] = self.cb_rate.currentText()
        t["auto_play"] = self.ck_autoplay.isChecked()
        t["auto_play_what"] = self.cb_autoplay_what.currentData()
        t["custom"] = {
            "base_url": self.ed_tts_url.text().strip(),
            "model": self.ed_tts_model.text().strip(),
            "voice": self.ed_tts_voice.text().strip(),
            "api_key": self.ed_tts_key.text().strip(),
        }

        cfg["trigger"]["enabled"] = self.ck_hotkey.isChecked()
        cfg["trigger"]["key"] = self.cb_key.currentData() or "ctrl"
        cfg["trigger"]["interval_ms"] = self.sl_interval.value()
        cfg["capture"]["prefer_uia"] = self.ck_uia.isChecked()
        cfg["capture"]["clipboard_wait_ms"] = self.sp_clip_wait.value()

        po = cfg["popup"]
        po["theme"] = self.cb_theme.currentData()
        po["font_size"] = self.sp_font.value()
        po["opacity"] = self.sl_opacity.value() / 100.0
        po["width"] = self.sp_width.value()
        po["auto_close_s"] = self.sp_autoclose.value()

        cfg["general"]["history_enabled"] = self.ck_history.isChecked()
        return cfg

    def _save(self) -> None:
        cfg = self._collect_into(self.cfg)
        self.config_saved.emit(copy.deepcopy(cfg))
        self.accept()

    def _apply_theme(self) -> None:
        self.setStyleSheet(build_qss(palette(self.cfg["popup"]["theme"])))


# ---------------------------------------------------------------- 小工具

def _wrap_h(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w


def _hbox(*widgets) -> QWidget:
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    for i, w in enumerate(widgets):
        lay.addWidget(w, 1 if i == 0 else 0)
    return _wrap_h(lay)


def _scroll(page: QWidget) -> QWidget:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # 表单只允许纵向滚动
    area.setWidget(page)
    return area


def _select_combo(combo: QComboBox, data: str) -> None:
    idx = combo.findData(data)
    if idx >= 0:
        combo.setCurrentIndex(idx)
