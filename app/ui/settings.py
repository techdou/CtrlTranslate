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
VOLUMES = ["-50%", "-30%", "-15%", "+0%"]  # edge-tts 音量为 ±N% 格式，放大无效只提供降档


class SettingsDialog(QDialog):
    config_saved = Signal(dict)

    def __init__(self, cfg: dict, translator, backup=None, parent=None):
        super().__init__(parent)
        self._orig = copy.deepcopy(cfg)
        self.cfg = copy.deepcopy(cfg)
        self._translator = translator
        self._backup = backup  # BackupService（main 持有）；None 时自建，测试/独立打开用
        if self._backup is None:
            from app.core.backup import BackupService
            self._backup = BackupService()
        self._backup.action_result.connect(self._on_backup_action)
        self._backup.snapshot_ready.connect(self._on_snapshot_ready)
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
            ("backup", "数据备份"),
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
        self.pages.addWidget(self._page_backup())

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
        _select_combo(self.cb_preset, current)  # findData 找不到时保持第 0 项
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
        # 按已存地址反查预设让下拉不误导；blockSignals 防止反查触发模板覆盖已填值
        fb_url = fb.get("base_url", "")
        self.cb_fb_preset.blockSignals(True)
        if fb_url:  # 空配置不反查（custom 的 base_url 也是空串，会误匹配）
            for i in range(self.cb_fb_preset.count()):
                if PROVIDER_PRESETS[self.cb_fb_preset.itemData(i)]["base_url"] == fb_url:
                    self.cb_fb_preset.setCurrentIndex(i)
                    break
        self.cb_fb_preset.blockSignals(False)
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

        net_head = QLabel("网络")
        net_head.setObjectName("sectionTitle")
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(10, 300)
        self.sp_timeout.setSuffix(" 秒")
        self.sp_timeout.setValue(int(self.cfg["translate"].get("timeout_s", 60)))
        lbl_timeout_hint = QLabel("单次翻译/OCR 请求的超时上限；慢代理或长文可调大")
        lbl_timeout_hint.setObjectName("dim")
        self.ed_proxy = QLineEdit(self.cfg.get("network", {}).get("proxy", ""))
        self.ed_proxy.setPlaceholderText("http://127.0.0.1:7890 —— OpenRouter / Gemini 等需代理的服务商；留空 = 直连")
        lbl_proxy_hint = QLabel("翻译与语音合成出站请求共用；仅支持 http(s) 代理地址")
        lbl_proxy_hint.setObjectName("dim")

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
        form.addRow(net_head)
        form.addRow("请求超时", self.sp_timeout)
        form.addRow("", lbl_timeout_hint)
        form.addRow("代理地址", self.ed_proxy)
        form.addRow("", lbl_proxy_hint)

        # ---- 网页版引擎（免费额度）：与"用哪个引擎翻译"同页，语义对齐 ----
        webai = self.cfg.get("webai", {})
        webai_head = QLabel("网页版引擎（免费额度）")
        webai_head.setObjectName("sectionTitle")
        self.ck_webai = QCheckBox("启用（划词/截图改走内嵌网页版 DeepSeek，无需 API Key）")
        self.ck_webai.setChecked(webai.get("enabled", False))
        lbl_webai_hint = QLabel("首次使用会弹出网页窗口，登录 DeepSeek 一次即可长期有效；"
                                "速度取决于网页服务。与 API 模式二选一，重试按钮跟随各自引擎。")
        lbl_webai_hint.setObjectName("dim")
        lbl_webai_hint.setWordWrap(True)
        form.addRow(webai_head)
        form.addRow("", self.ck_webai)
        form.addRow("", lbl_webai_hint)
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

        self.cb_volume = QComboBox()
        self.cb_volume.addItems(VOLUMES)
        self.cb_volume.setCurrentText(t.get("volume", "+0%"))

        self.ck_autoplay = QCheckBox("翻译完成后自动播报")
        self.ck_autoplay.setChecked(t.get("auto_play", False))
        self.cb_autoplay_what = QComboBox()
        self.cb_autoplay_what.addItem("播报英文原文", "source")
        self.cb_autoplay_what.addItem("播报中文译文", "translated")
        _select_combo(self.cb_autoplay_what, t.get("auto_play_what", "source"))
        self.ck_tts.toggled.connect(self._sync_tts_enabled)
        self.cb_engine.currentIndexChanged.connect(
            lambda _i: self._sync_engine_fields(form))
        for w in (self.cb_engine, self.cb_voice_zh, self.cb_voice_en, self.cb_rate, self.cb_volume,
                  self.ed_tts_url, self.ed_tts_model, self.ed_tts_voice, self.ed_tts_key,
                  self.btn_tts_eye, self.ck_autoplay, self.cb_autoplay_what):
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
        form.addRow("音量", self.cb_volume)
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
        for w in (self.cb_engine, self.cb_voice_zh, self.cb_voice_en, self.cb_rate, self.cb_volume,
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

        # ---- 屏幕截图翻译（OCR） ----
        ocr = self.cfg.get("ocr", {})
        ocr_head = QLabel("屏幕截图翻译（OCR）")
        ocr_head.setObjectName("sectionTitle")

        self.ck_ocr = QCheckBox("启用（托盘菜单 + 热键框选屏幕区域，识别并翻译图内文字）")
        self.ck_ocr.setChecked(ocr.get("enabled", True))

        self.cb_ocr_model = QComboBox()
        self.cb_ocr_model.setEditable(True)
        self.cb_ocr_model.addItems(["glm-4v-flash", "glm-4v-plus", "gpt-4o-mini", "gemini-2.0-flash"])
        self.cb_ocr_model.setCurrentText(ocr.get("model", "glm-4v-flash"))

        self.ed_ocr_hotkey = QLineEdit(ocr.get("hotkey", "alt+q"))
        self.ed_ocr_hotkey.setPlaceholderText("如 alt+q；留空 = 禁用热键（仍可从托盘菜单触发）")
        lbl_ocr_hint = QLabel("识别模型须支持图片输入；地址与 API Key 复用上方翻译服务。"
                              "智谱 glm-4v-flash 免费，填了翻译 Key 即可直接用")
        lbl_ocr_hint.setObjectName("dim")

        form.addRow("", self.ck_hotkey)
        form.addRow("触发键", self.cb_key)
        form.addRow("双击判定间隔", _hbox(self.sl_interval, self.lbl_interval))
        form.addRow("", self.ck_uia)
        form.addRow("复制法等待上限", self.sp_clip_wait)
        form.addRow("单次翻译长度上限（字符）", self.sp_max_chars)
        form.addRow(ocr_head)
        form.addRow("", self.ck_ocr)
        form.addRow("识别模型", self.cb_ocr_model)
        form.addRow("截图热键", self.ed_ocr_hotkey)
        form.addRow("", lbl_ocr_hint)
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

        self.btn_clear_cache = QPushButton("清空翻译缓存")
        self.btn_clear_cache.clicked.connect(self._clear_cache)

        self.lbl_paths = QLabel("配置、数据库与日志均存于 ~/.ctrltrans/")
        self.lbl_paths.setObjectName("dim")

        form.addRow("", self.ck_history)
        form.addRow("数据位置", self.btn_open_dir)
        form.addRow("翻译缓存", self.btn_clear_cache)
        form.addRow("", self.lbl_paths)
        return _scroll(page)

    def _clear_cache(self) -> None:
        from app.db import database

        database.clear_translation_cache()
        self.btn_clear_cache.setText("已清空")
        self.btn_clear_cache.setEnabled(False)

    # ---------------------------------------------------------------- WebDAV 备份

    def _page_backup(self) -> QWidget:
        w = self.cfg.get("webdav", {})
        form, page = self._page("数据备份（WebDAV）")

        self.ed_wd_url = QLineEdit(w.get("url", ""))
        self.ed_wd_url.setPlaceholderText("如 https://dav.jianguoyun.com/dav/（坚果云）")
        self.ed_wd_user = QLineEdit(w.get("username", ""))
        self.ed_wd_user.setPlaceholderText("坚果云 = 账户邮箱")
        self.ed_wd_password = QLineEdit(w.get("password", ""))
        self.ed_wd_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_wd_eye = QPushButton("显示")
        self.btn_wd_eye.setCheckable(True)
        self.btn_wd_eye.setFixedWidth(52)
        self.btn_wd_eye.toggled.connect(
            lambda on: self.ed_wd_password.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        wd_pw_row = QHBoxLayout()
        wd_pw_row.addWidget(self.ed_wd_password, 1)
        wd_pw_row.addWidget(self.btn_wd_eye)

        self.ed_wd_dir = QLineEdit(w.get("remote_dir", "CtrlTranslate"))
        self.ed_wd_dir.setPlaceholderText("远端目录名，默认 CtrlTranslate")

        self.btn_wd_test = QPushButton("测试连接")
        self.lbl_wd_test = QLabel("")
        self.lbl_wd_test.setObjectName("dim")
        self.btn_wd_test.clicked.connect(self._test_webdav)
        wd_test_row = QHBoxLayout()
        wd_test_row.addWidget(self.btn_wd_test)
        wd_test_row.addWidget(self.lbl_wd_test, 1)

        self.btn_wd_backup = QPushButton("立即备份")
        self.lbl_wd_backup = QLabel("")
        self.lbl_wd_backup.setObjectName("dim")
        self.btn_wd_backup.clicked.connect(self._backup_now)
        wd_backup_row = QHBoxLayout()
        wd_backup_row.addWidget(self.btn_wd_backup)
        wd_backup_row.addWidget(self.lbl_wd_backup, 1)

        self.btn_wd_restore = QPushButton("从备份恢复…")
        self.btn_wd_restore.clicked.connect(self._restore_now)

        lbl_wd_hint = QLabel(
            "备份内容 = 翻译历史 + 生词本（不含翻译缓存），远端为单个 backup.json，"
            "每次备份覆盖（坚果云网页端保留历史版本可回滚）。恢复是合并导入："
            "只增不删，重复条目自动跳过。\n"
            "坚果云：账户信息 → 安全选项 → 添加应用密码，密码栏填它（不是登录密码）。"
            "其他标准 WebDAV（Nextcloud / NAS）同样可用。"
        )
        lbl_wd_hint.setObjectName("dim")
        lbl_wd_hint.setWordWrap(True)

        form.addRow("服务器地址", self.ed_wd_url)
        form.addRow("账号", self.ed_wd_user)
        form.addRow("密码（应用密码）", _wrap_h(wd_pw_row))
        form.addRow("远端目录", self.ed_wd_dir)
        form.addRow("", _wrap_h(wd_test_row))
        form.addRow("", _wrap_h(wd_backup_row))
        form.addRow("恢复", self.btn_wd_restore)
        form.addRow("", lbl_wd_hint)
        return _scroll(page)

    def _wcfg_from_form(self) -> tuple[str, str, str, str]:
        """用表单当前值组 WebDAV 配置（未保存的账号密码也要能测）。"""
        url = self.ed_wd_url.text().strip()
        user = self.ed_wd_user.text().strip()
        pw = self.ed_wd_password.text()
        remote_dir = self.ed_wd_dir.text().strip() or "CtrlTranslate"
        if not (url and user and pw):
            raise ValueError("服务器地址 / 账号 / 密码还没填完整")
        return (url, user, pw, remote_dir)

    def _test_webdav(self) -> None:
        try:
            wcfg = self._wcfg_from_form()
        except ValueError as e:
            self.lbl_wd_test.setText(str(e))
            return
        self.lbl_wd_test.setText("测试中…")
        self.btn_wd_test.setEnabled(False)
        self._backup.test_now(wcfg, self._proxy_from_cfg())

    def _backup_now(self) -> None:
        try:
            wcfg = self._wcfg_from_form()
        except ValueError as e:
            self.lbl_wd_backup.setText(str(e))
            return
        self.lbl_wd_backup.setText("备份中…")
        self.btn_wd_backup.setEnabled(False)
        self._backup.backup_now(wcfg, self._proxy_from_cfg())

    def _restore_now(self) -> None:
        try:
            wcfg = self._wcfg_from_form()
        except ValueError as e:
            self.lbl_wd_backup.setText(str(e))
            return
        self.btn_wd_restore.setEnabled(False)
        self.btn_wd_restore.setText("读取远端备份…")
        self._backup.fetch_snapshot(wcfg, self._proxy_from_cfg())

    def _proxy_from_cfg(self) -> str:
        return str(self.cfg.get("network", {}).get("proxy") or "").strip()

    def _set_result_label(self, label: QLabel, msg: str, ok: bool) -> None:
        from app.ui.theme import palette as _palette

        label.setText(msg)
        label.setStyleSheet(
            f"color: {_palette(self.cfg['popup']['theme'])['accent' if ok else 'error']}"
        )

    def _on_backup_action(self, action: str, ok: bool, msg: str) -> None:
        if action == "test":
            self.btn_wd_test.setEnabled(True)
            self._set_result_label(self.lbl_wd_test, msg, ok)
        elif action == "backup":
            self.btn_wd_backup.setEnabled(True)
            self._set_result_label(self.lbl_wd_backup, msg, ok)
        elif action == "restore_fetch":
            self.btn_wd_restore.setEnabled(True)
            self.btn_wd_restore.setText("从备份恢复…")
            self._set_result_label(self.lbl_wd_backup, msg, ok)

    def _on_snapshot_ready(self, snap: dict) -> None:
        from PySide6.QtWidgets import QMessageBox

        from app.core.backup import apply_snapshot, snapshot_summary

        self.btn_wd_restore.setEnabled(True)
        self.btn_wd_restore.setText("从备份恢复…")
        answer = QMessageBox.question(
            self, "从备份恢复",
            f"{snapshot_summary(snap)}\n\n恢复为合并导入：只增不删，重复条目自动跳过。继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            stats = apply_snapshot(snap)
        except Exception as e:
            self._set_result_label(self.lbl_wd_backup, f"恢复失败：{e}", False)
            return
        self._set_result_label(
            self.lbl_wd_backup,
            f"已导入 {stats['history_imported']} 条历史、{stats['vocabulary_imported']} 个生词"
            f"（跳过已有历史 {stats['history_skipped']} 条）",
            True,
        )

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
        # 用表单当前值测试（未保存的 Key/模型也要能测），与备用测试同一通道
        endpoint = (
            self.ed_base_url.text().strip(),
            self.ed_api_key.text().strip(),
            self.cb_model.currentText().strip(),
        )
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

        self._translator.test_connection(ok, fail, endpoint=endpoint)

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
        cfg["translate"]["timeout_s"] = self.sp_timeout.value()

        t = cfg["tts"]
        t["enabled"] = self.ck_tts.isChecked()
        t["engine"] = self.cb_engine.currentData()
        t["voice_zh"] = self.cb_voice_zh.currentText().strip()
        t["voice_en"] = self.cb_voice_en.currentText().strip()
        t["rate"] = self.cb_rate.currentText()
        t["volume"] = self.cb_volume.currentText()
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

        o = cfg.setdefault("ocr", {})
        o["enabled"] = self.ck_ocr.isChecked()
        o["model"] = self.cb_ocr_model.currentText().strip()
        o["hotkey"] = self.ed_ocr_hotkey.text().strip()

        w = cfg.setdefault("webai", {})
        w["enabled"] = self.ck_webai.isChecked()

        po = cfg["popup"]
        po["theme"] = self.cb_theme.currentData()
        po["font_size"] = self.sp_font.value()
        po["opacity"] = self.sl_opacity.value() / 100.0
        po["width"] = self.sp_width.value()
        po["auto_close_s"] = self.sp_autoclose.value()

        cfg["general"]["history_enabled"] = self.ck_history.isChecked()
        cfg.setdefault("network", {})["proxy"] = self.ed_proxy.text().strip()

        w = cfg.setdefault("webdav", {})
        w["url"] = self.ed_wd_url.text().strip()
        w["username"] = self.ed_wd_user.text().strip()
        w["password"] = self.ed_wd_password.text().strip()
        w["remote_dir"] = self.ed_wd_dir.text().strip() or "CtrlTranslate"
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
