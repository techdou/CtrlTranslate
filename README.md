# CtrlTranslate

读英文文献/网页时的桌面划词翻译工具：**选中文字 → 双击 Ctrl → 弹窗流式翻译（大模型）**，支持语音播报、生词本与翻译历史。Windows 平台，常驻系统托盘。

## 功能特性

- **双击 Ctrl 触发**——全局键盘钩子 + 状态机判定（与 Ctrl+C/V 等组合键不冲突），判定间隔 150–600 ms 可调，可整体停用
- **大模型流式翻译**——OpenAI 兼容协议，内置智谱 GLM Flash（免费、国内直连，默认推荐）、硅基流动、DeepSeek、OpenRouter、Gemini 预设，也可填任意兼容服务；打字机式逐字输出
- **学习模式**——译文之外额外输出专业术语表（读文献场景友好），也支持仅译文的简洁模式，Prompt 可完全自定义
- **语音播报（TTS）**——读原文（练听力）或读译文；在线音色（edge-tts）失败自动降级 Windows 系统语音；合成结果缓存，重复播报零等待
- **生词本 + 翻译历史**——SQLite 本地存储，支持搜索、删除、导出 CSV（可直接导入 Anki）
- **双取词引擎自动降级**——优先 UI Automation 直读选中（不碰剪贴板），失败自动改用模拟复制法，并完整保存/恢复你的剪贴板
- **细节体验**——弹窗跟随鼠标、高度自适应、钉住防误关、失焦自动关闭、深/浅双主题、开机自启（免管理员）、单实例保护

## 快速开始

### 方式一：直接用打包版

下载/构建 `CtrlTranslate.exe` 后双击运行，程序常驻系统托盘（可能被折叠到托盘溢出区）。

自行打包：

```bat
pip install -r requirements.txt pyinstaller
build.bat
:: 产出 dist\CtrlTranslate.exe
```

### 方式二：源码运行

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

## 配置 API Key（首次使用必做）

托盘图标右键 → **设置… → 翻译服务**，选择服务商并填入 API Key（点击「测试连接」验证）：

| 服务商 | 费用 | 获取 Key |
|---|---|---|
| 智谱 AI（默认） | **免费**（GLM Flash 系列） | [bigmodel.cn](https://bigmodel.cn) |
| 硅基流动 SiliconFlow | 注册赠额 + 免费档 | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) |
| DeepSeek | 低价 | [platform.deepseek.com](https://platform.deepseek.com) |
| OpenRouter | 含 `:free` 模型 | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Google Gemini | 免费额度 | [aistudio.google.com](https://aistudio.google.com) |

> OpenRouter 与 Gemini 在国内需要代理。API Key 仅保存在本机 `~/.ctrltrans/config.json`，不会上传到任何地方。

## 使用方法

1. 启动后确认托盘菜单「启用双击 Ctrl 取词」已勾选
2. 在浏览器 / PDF 中选中一段英文，快速双击 Ctrl（松开后触发）
3. 弹窗流式显示译文；按钮：读原文 / 读译文 / 收藏到生词本 / 复制 / 重试；「钉住」可防止点其他程序时弹窗关闭
4. Esc 或点击其他程序关闭弹窗

## 故障排查

- **双击没反应**：确认托盘勾选；部分全屏/管理员权限程序收不到普通权限的键盘钩子，以管理员身份运行本程序可解
- **提示未取到选中文本**：需先真实选中一段文字；禁止复制的页面无法取词（属预期）
- **edge-tts 偶发 403 / 无声**：微软接口有反滥用限制，程序会自动降级系统语音；重试或更换网络通常可恢复
- **系统级复制粘贴全部失灵**：Windows 剪贴板服务偶发卡死，管理员 PowerShell 执行 `Restart-Service cbdhsvc*` 或重启机器
- **日志位置**：`~/.ctrltrans/logs/app.log`

数据（配置 / 数据库 / 日志 / 语音缓存）全部在 `~/.ctrltrans/`，卸载删除该目录即可彻底清理。

## 开发

```bat
.venv\Scripts\python -m pytest tests/ -q        :: 单元测试
```

技术栈：Python 3.10+ · PySide6 · openai SDK · edge-tts · keyboard · Win32 API（UIA / 剪贴板 / SendInput）

```
main.py            # 入口：单实例 + 服务组装
app/
  core/            # 热键 / 取词 / 翻译 / TTS / 生词本 / 自启
  ui/              # 翻译弹窗 / 设置 / 历史生词本 / 托盘 / 主题
  db/              # SQLite 存储层
tests/             # 单元测试
scripts/           # 开发辅助（端到端测试 / 截图 / 图标生成 / 剪贴板修复）
assets/            # 应用图标
```

## 已知边界

- 仅支持 Windows（键盘钩子 / 剪贴板 / UIA / SAPI 均为 Win32 API）
- 剪贴板恢复覆盖文本 / HTML / 位图格式；文件列表（复制文件后划词）不恢复，日常划词场景几乎不涉及

## License

MIT
