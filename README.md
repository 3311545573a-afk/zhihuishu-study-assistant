# 智慧树课程学习助手

基于 Python、Playwright 和兼容 Chat Completions 的 AI 接口，辅助处理课程视频中的单选、多选和判断练习题。

支持课程页面识别、题目文字提取、图片与数学公式转写、答案选择、弹题翻页和关闭、自然播放结束后切换下一节。登录会话保存在本机；图片加载和切课的临时故障会在有限时间内自动恢复，持续失败才会暂停并提示。

## 环境与安装

- Windows，Python 3.10+，Google Chrome 或 Microsoft Edge。
- 直接依赖：`playwright==1.63.0`、`rapidocr-onnxruntime==1.2.3`。
- 图片视觉识别需要支持 `image_url` 的 Chat Completions 接口。普通文字题只要求文本对话能力。

在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

默认 `browser_channel` 为 `auto`：优先使用已安装的 Chrome，Chrome 不可用时自动使用 Microsoft Edge，不会自动下载 Chromium。若使用 Playwright Chromium，将 `browser_channel` 改为 `chromium`，再运行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 配置

编辑本机 `config.json`：

- `course_url`：填写登录后课程视频页的完整地址。模板仅含站点通用地址。
- `ai.base_url`：AI 接口的 HTTPS 基础地址，程序会追加 `/chat/completions`，也支持填写完整接口地址。
- `ai.model`：服务商提供的模型名称。
- `ai.api_key`：自己的 API Key。
- `ai.min_confidence`：最低置信度，默认 `0.85`。
- `ai.timeout_seconds`：单次 API 请求超时，默认 45 秒。
- `poll_seconds`：页面轮询间隔，默认 2 秒。
- `browser_channel`：`auto` 或 `chrome` 按 Chrome→Edge 回退，也可固定为 `msedge` 或 `chromium`。

环境变量 `ZHS_AI_API_KEY`、`ZHS_AI_BASE_URL`、`ZHS_AI_MODEL` 可以覆盖配置文件中的对应字段。不要把密钥写入源码或提交到 Git。

```powershell
.\.venv\Scripts\python.exe -X utf8 study_assistant.py --check-config
```

此命令只检查本地配置，不调用远程接口。

## 运行

双击 `start.cmd`，或执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 study_assistant.py
```

1. 在脚本打开的 Chrome 或 Edge 中登录并进入具体课程视频页。
2. 检测到课程后自动开始监控；若浏览器阻止首次播放，手动点击播放。
3. 遇到练习题时读取完整题干和全部选项，再匹配可选本地题库或请求 AI。
4. 已加载的图片即使位于弹窗滚动区域下方，也直接读取原图；未加载图片自动滚动并等待加载。点击底部选项时自动滚动。
5. 显示身份验证、未知弹窗或持续识别失败时，按终端提示手动处理。按 `Ctrl+C` 停止。

更新代码后需停止旧程序、关闭它打开的浏览器，再重新运行。

## 网页控制台

也可以双击 `web_console.cmd`，或执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 web_console.py
```

控制台只监听本机 `127.0.0.1`，提供启动、停止、重启、暂停/恢复视频、实时日志、配置校验和诊断清理。API Key 在页面和接口返回中会被掩码；发送给助手的补充输入只通过本机进程管道传递。

答题识别、图片懒加载和切课恢复期间不需要点击“发送回车”。图片与切课临时故障最多自动恢复 45 秒，空弹窗等待 15 秒；同一结束视频的下一节按钮最多尝试 3 次。持续失败仍会保留诊断并等待人工处理。

## 图片与公式

AI 配置完整时，将题目原图发送给配置的视觉接口转写，保留公式上下标、分段条件和区间，再调用解题接口。每张未缓存的图片会增加一次 API 请求；首次识别可能需要数秒至数十秒，后续核对使用内存缓存。

未配置 AI 时，图片读取使用本地 RapidOCR，但普通 OCR 不保证复杂公式的识别效果。视觉模型也可能误读文字或图形，置信度不等于正确率；不完整结果会暂停。

本项目不修改视频播放速率、进度或平台学习记录，也不处理验证码。填空题、复杂图表及站点结构变化可能需要进一步适配。下一节按钮持续不可用时会暂停，不能据此判断整门课已完成。

## 本地题库

可将 `question_bank.example.json` 复制为 `question_bank.json`，按完整题干和选项文字添加已确认答案：

```json
[
  {"question": "SQL 查询使用哪个关键字？", "answers": ["SELECT"]}
]
```

使用选项文字匹配，避免选项乱序引起错答。个人题库已被 Git 忽略。

## 隐私与诊断

以下内容只保存在本机，已加入 `.gitignore`：

- `config.json`：个人接口和课程配置。
- `browser_profile/`、`.session/`：浏览器资料、Cookie 和 Storage。
- `logs/`、`diagnostics/`：运行日志、页面截图和控件诊断。
- `question_bank.json`：个人题库。

发布版本不包含个人账号、密钥、课程标识、浏览器登录数据、运行记录或实际页面截图。测试图片由程序生成。

诊断截图可能包含页面显示的个人信息，分享前请自行检查。仅诊断模式：

```powershell
.\start.cmd --inspect
```

选择器可在 `config.json` 的 `selectors` 中覆盖。无法读取选中状态的自定义控件会暂停，不能只靠点击位置判断答题成功。

## 源码与测试

| 文件 | 用途 |
| --- | --- |
| `study_assistant.py` | 配置、课程监控和运行入口 |
| `browser_adapter.py` | 页面读取、选项操作和续播 |
| `image_text.py` | 原图读取、懒加载与识别缓存 |
| `answer_engine.py` | 题库匹配、视觉转写和 AI 答案校验 |
| `session_store.py` | 本机会话保存和恢复 |
| `run_control.py` | 助手与网页控制台之间的本机命令和状态 |
| `web_console.py` / `web_console.cmd` | 本机网页控制台 |
| `cleanup.py` | 诊断目录按策略清理 |
| `tests/` | 模拟页面、模拟 API 和合成图片回归测试 |

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile answer_engine.py browser_adapter.py image_text.py study_assistant.py session_store.py
```

测试使用本机 Chrome，无须登录真实课程，也不会请求付费 AI。平台页面和第三方模型可能变化，本地测试通过不代表所有线上课程都已验证。
