# 智慧树课程学习助手

使用 Python、Playwright 和兼容 Chat Completions 的 AI 接口，辅助观看课程视频、识别课中练习题和处理答题反馈。提供本机网页控制台，支持启动、停止、配置和实时日志。

## 功能

- 自动等待登录和课程加载，检测到视频后开始监控。
- 读取单选、多选和判断题，优先匹配本地题库，再请求 AI。
- 识别题目原图及数学公式，处理图片懒加载并缓存结果。
- 提交前核对题目和选中状态，避免重复提交；处理反馈、翻页和关闭弹窗。
- 视频自然播放结束后尝试切换下一节，临时加载或切课故障自动重试。
- 保存本机登录会话，提供视频暂停/恢复、诊断检查与清理。

## 快速开始

### 1. 安装环境

需要 Windows、Python 3.10 或更新版本，以及 Google Chrome 或 Microsoft Edge。下载项目后，在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path config.json)) {
    Copy-Item config.example.json config.json
}
```

直接依赖为 `playwright==1.63.0` 和 `rapidocr-onnxruntime==1.2.3`。安装依赖时会安装所需的图片识别组件。

### 2. 填写配置

编辑 `config.json`，填写自己的课程视频页地址、AI 接口地址、模型名称和 API Key。模板只包含站点通用入口，需要替换为具体课程地址。

下面展示主要字段；请将占位内容替换为自己的配置：

```json
{
  "course_url": "https://studyvideoh5.zhihuishu.com/stuStudy",
  "browser_channel": "auto",
  "poll_seconds": 2,
  "ai": {
    "base_url": "https://ai.example/v1",
    "model": "替换为服务商提供的模型名称",
    "api_key": "替换为自己的 API Key",
    "min_confidence": 0.85,
    "timeout_seconds": 45
  }
}
```

普通文字题需要文本对话接口；图片与公式题需要接口同时支持 `image_url`。地址必须使用 HTTPS，程序会自动追加 `/chat/completions`，也接受完整接口地址。

检查本地配置：

```powershell
.\.venv\Scripts\python.exe -X utf8 study_assistant.py --check-config
```

此命令不请求 AI。退出码 `0` 表示格式正确且 AI 必填字段已填写；退出码 `2` 表示配置无效或 AI 字段未填齐。填写成功不代表接口已经联网验证。

### 3. 启动网页控制台

双击 `web_console.cmd`，或执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 web_console.py
```

浏览器会打开 [本机控制台](http://127.0.0.1:8765/)。点击“启动助手”，在助手打开的浏览器中登录并进入课程视频页。课程加载后自动开始，无需发送回车；首次播放被浏览器阻止时，手动点击播放。

也可以双击 `start.cmd` 直接运行助手，或使用命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 study_assistant.py
```

`start.cmd` 会准备虚拟环境、安装缺少的依赖，并在配置文件不存在时复制模板。`web_console.cmd` 使用已有环境；首次使用请先完成上面的安装步骤。

## 控制台操作

| 操作 | 作用 |
| --- | --- |
| 启动助手 | 打开浏览器，恢复仍有效的登录会话并等待课程加载 |
| 停止助手 | 请求退出；超时后结束助手及其浏览器进程 |
| 重启助手 | 重新加载代码和配置 |
| 暂停视频 / 开始视频 | 暂停或恢复播放；暂停期间仍会处理弹题 |
| 发送回车（继续） | 人工处理完浏览器提示后，让等待中的助手继续检查 |
| 发送输入 / 发送 q | 向等待输入的助手发送内容，或请求退出 |
| 保存配置 | 校验后保存，并生成 `config.json.bak` |
| 检查并按策略清理 | 预览诊断清理方案，确认后删除旧诊断文件 |

代码和配置在助手启动时加载，修改后点击“重启助手”才会生效。正常读题、图片加载和切课重试期间，无需反复点击“发送回车”。

控制台只监听本机 `127.0.0.1`。密钥字段不会回传完整 API Key，页面显示掩码；输入框留空表示保留原密钥。诊断文件仅在确认清理后删除，始终保留最新一份。

按 `Ctrl+C` 退出控制台时，它也会停止自己启动的助手。直接运行助手时，按 `Ctrl+C` 停止。

## 自动恢复与人工处理

| 场景 | 处理方式 |
| --- | --- |
| 图片仍在加载 | 单次加载最多等待 5 秒；首次失败后进入 45 秒自动重试窗口 |
| 可见弹窗暂时没有题目内容 | 等待 15 秒，继续检查内容 |
| 切课时控件超时或视频暂时缺失 | 在同一 45 秒恢复窗口内继续轮询 |
| 点击下一节后仍是原视频 | 等待 20 秒后重试，同一结束视频最多成功点击 3 次 |
| 已点击下一节，但按钮暂时不可用 | 保留当前切课状态，最多等待 45 秒恢复 |
| 播放进度持续不变 | 超过 60 秒后提示人工检查；答题、识别和恢复耗时不计入播放卡顿 |
| 图片无法识别、答案置信度不足或题目发生变化 | 暂停自动提交，等待人工处理 |

超时在轮询时检查，单次网页操作耗时可能让实际等待略长。持续失败会记录原因并保存诊断，之后才进入等待输入。

需要人工处理时，先查看日志，在课程浏览器中完成提示或处理当前题目，再点击“发送回车（继续）”。如果修改了配置，应点击“重启助手”。身份验证需自行完成。

视频结束且未找到可用的下一节入口时，助手可能停止；请核对课程目录与学习进度，不能据此认定整门课已完成。

## 配置说明

| 字段 | 默认值 / 要求 |
| --- | --- |
| `course_url` | HTTPS 的智慧树课程视频页地址 |
| `browser_channel` | `auto`；优先 Chrome，不可用时尝试 Edge |
| `poll_seconds` | `2` 秒，允许 `0.5–30` 秒 |
| `ai.base_url` | 自己的 HTTPS AI 接口地址 |
| `ai.model` | 服务商提供的模型名称 |
| `ai.api_key` | 自己的 API Key |
| `ai.min_confidence` | `0.85`，允许 `0–1`；图片视觉转写至少要求 `0.85` |
| `ai.timeout_seconds` | `45` 秒，允许 `1–120` 秒 |
| `diagnostics.keep_days` | `7` 天 |
| `diagnostics.keep_count` | `50` 份 |
| `diagnostics.keep_mb` | `200` MB |
| `selectors` | 弹窗、题干、选项等 CSS 选择器；通常保持模板值 |

`ZHS_AI_API_KEY`、`ZHS_AI_BASE_URL`、`ZHS_AI_MODEL` 环境变量会覆盖文件中的 AI 字段。若控制台保存了配置但运行时仍使用旧接口，请检查启动控制台的环境变量。

`browser_channel` 也支持 `chrome`、`msedge` 和 `chromium`。`chrome` 同样允许回退到 Edge；`msedge` 固定使用 Edge。使用 `chromium` 时需自行安装：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 图片、公式与题库

AI 配置完整时，程序将题目原图发给配置的视觉接口转写，再用完整题干和选项解题。转写会保留公式上下标、分段条件和区间；识别结果缓存在内存中，后续核对使用缓存。

每张首次识别的图片会增加一次 API 请求，完整题目还会调用一次解题接口。读图可能需要数秒至数十秒，可在日志中查看进展。置信度用于过滤答案，不代表正确率保证。

未配置 AI 时，图片读取使用本地 RapidOCR，已确认题目可匹配本地题库；未知题目会暂停。复杂公式、图表、填空题及页面结构变化可能需要人工处理或进一步适配。

本地题库可从模板创建：

```powershell
if (!(Test-Path question_bank.json)) {
    Copy-Item question_bank.example.json question_bank.json
}
```

按完整题干和选项文字填写已确认答案，多选题可填写多个答案：

```json
[
  {"question": "SQL 查询使用哪个关键字？", "answers": ["SELECT"]}
]
```

程序按选项文字匹配，避免选项乱序造成错答。更新项目时保留自己的 `question_bank.json`。

## 隐私与诊断

发布仓库使用通用配置和合成测试图片，个人运行数据由 `.gitignore` 排除：

| 本机文件 / 目录 | 内容 |
| --- | --- |
| `config.json`、`config.json.bak` | 课程地址、接口配置和密钥 |
| `browser_profile/`、`.session/` | 浏览器资料、Cookie 和 Storage |
| `logs/` | 日志、运行状态和控制命令 |
| `diagnostics/` | 页面截图与控件诊断 |
| `question_bank.json` | 自己维护的题库 |

调用 AI 时会发送题目文字、选项或题目原图；读取题图使用原始像素。发布代码不包含个人密钥、课程标识、登录数据或实际页面截图。

诊断截图和日志可能包含页面显示的个人信息，分享前请检查。仅采集诊断、不播放或答题：

```powershell
.\start.cmd --inspect
```

诊断模式需要让目标弹窗保持显示，再按终端提示回车保存。清理前可查看计划：

```powershell
.\.venv\Scripts\python.exe -X utf8 cleanup.py --dry-run
```

## 常见问题

| 问题 | 处理方法 |
| --- | --- |
| 提示缺少 `.venv` | 先按“快速开始”安装，或运行 `start.cmd` |
| 找不到 `config.json` | 复制 `config.example.json`，填写配置后再启动 |
| 控制台端口被占用 | 关闭已有控制台，或使用 `web_console.py --port 8766` |
| 一直等待课程 | 在助手打开的浏览器中完成登录，并进入具体视频页 |
| AI 返回 HTTP 错误或超时 | 根据日志检查接口地址、模型、密钥、额度和网络 |
| 公式题一直识别失败 | 确认接口支持 `image_url`，查看图片加载或识别错误 |
| 修改配置没有生效 | 点击“重启助手”，并检查环境变量是否覆盖配置 |
| 仍需回车才能继续 | 确认旧助手已重启；若是持续失败或人工提示，按日志处理 |

## 源码与验证

| 文件 | 用途 |
| --- | --- |
| `study_assistant.py` | 配置、监控循环和助手入口 |
| `browser_adapter.py` | 页面读取、选项操作、反馈与切课 |
| `image_text.py` | 原图读取、懒加载和识别缓存 |
| `answer_engine.py` | 题库匹配、视觉转写和答案校验 |
| `session_store.py` | 本机会话保存与恢复 |
| `run_control.py` | 本机控制命令和运行状态 |
| `web_console.py`、`web/` | 网页控制台后端与界面 |
| `cleanup.py` | 诊断目录清理 |
| `tests/` | 本地页面、模拟 API 和合成图片回归测试 |

运行测试与语法检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile answer_engine.py browser_adapter.py cleanup.py image_text.py run_control.py session_store.py study_assistant.py web_console.py
```

本次发布验证：216 项测试，215 通过、1 项因本机无法创建符号链接跳过。测试使用本机 Chrome，不需要登录真实课程，不请求付费 AI；新版真实课程行为仍需使用时验证。

助手按视频自然播放结束切课，不修改播放速率、播放进度或平台学习记录，也不处理验证码。
