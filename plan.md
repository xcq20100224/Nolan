# 贾维斯（Jarvis）本地语音助手 · 执行蓝图

## 第一性原理分解

贾维斯 = 常驻循环（听 → 想 → 做 → 说）× 工具权限 × 记忆。
五层不可再分要素：耳朵(ASR) → 唤醒(VAD/唤醒词) → 大脑(LLM+工具) → 手(执行) → 嘴(TTS)。

## 阶段一：MVP 闭环（本轮执行）

目标：命令行运行 `python jarvis/jarvis.py`，对着麦克风说中文 → 识别 → 大脑回复 → 固定音色播报。

### 环境（已验证）
- 托管 Python 3.12；已安装 sounddevice / faster-whisper / edge-tts / pygame / numpy / httpx
- 麦克风=设备1（英特尔麦克风阵列），扬声器=设备3（Realtek）
- Whisper small 模型已缓存（下载需 HF_ENDPOINT=https://hf-mirror.com + HF_HUB_DISABLE_XET=1，运行时无需网络）
- 工作目录：<仓库根目录>\jarvis\

### 接口契约（各模块严格遵守）
- `ears.listen_once(timeout: float = 30.0) -> str | None` — 阻塞监听一句话，返回中文文本
- `mouth.speak(text: str) -> None` — edge-tts 固定音色合成并播放，播完返回
- `brain.think(user_text: str, history: list[dict]) -> str` — 返回回复文本；有 API Key 走 LLM，否则规则兜底

### 子代理分工（并行，各写各的文件，互不修改）
1. 耳朵工程师 → `ears.py`（sounddevice 16kHz 单声道录音 + 能量 VAD + faster-whisper small/int8 中文识别）
2. 嘴巴工程师 → `mouth.py`（edge-tts zh-CN-YunxiNeural → 临时 mp3 → pygame 播放）
3. 大脑工程师 → `brain.py`（规则引擎：时间/开应用/退出/个性闲聊 + 可选 OpenAI 兼容 API 接入，环境变量 JARVIS_API_KEY/JARVIS_BASE_URL/JARVIS_MODEL）
4. 集成工程师 → `jarvis.py` 主循环（"贾维斯"唤醒词前缀剥离、Ctrl+C 退出、对话打印）+ `test_jarvis.py`（免麦克风回路测试：TTS 合成 → ASR 识别 → 比对；brain 单测）+ `README.md`

### 验收
- `python jarvis/test_jarvis.py` 全绿（无需麦克风）
- 主程序可启动、能录音、识别、播报（由用户实测）

## 阶段二及以后（第一性原理路线图）

| 阶段 | 目标 | 关键动作 |
|---|---|---|
| 产品定名 | Nolan | 产品已定名 Nolan（2026-07-23）：助手自称 Nolan、称用户「先生」，唤醒词认「Nolan」（大小写不敏感）与「诺兰」，旧名「贾维斯」不再唤醒 |
| 二 · 手 | ✅ 已完成（2026-07-22）真能干事 | 落地 hands.py 工具集（9 个）：open_app/open_url/web_search/fetch_url/read_file/write_file/list_files/run_command/get_time，含文件沙盒与 shell 白名单安全边界 |
| 三 · 记忆 | ✅ 已完成（2026-07-23）认识你 | 落地 memory.py 长期记忆：jarvis\memory\long_term.txt 纯文本逐行追加（带时间戳），记住/回忆/忘掉四函数契约，不做向量检索与自动摘要 |
| 四 · 主动 | ✅ 已完成（2026-07-23）不用你开口 | 落地 reminders.py 主动提醒（「提醒我X分钟后…」中文时间解析 → jarvis\memory\reminders.txt 落盘 → 主循环到点弹出播报，一次性提醒不做重复规则）+ nolan_app.py tkinter 聊天窗口（Nolan.bat 双击启动，Nolan-CLI.bat 保留旧控制台模式） |
| 五 · 网页版 | ✅ 已落地 | 网页版 nolan-web 已落地（2026-07-23，React+Vite 前端 + Python 标准库 API，npm run dev 一键同启） |
| 五 · 修复 | ✅ 已落地 | 通用应用打开 + 本地 Whisper 网页语音输入已落地（2026-07-23）：open_app 通用化（别名 + PATH 查找 + 已知路径 + 开始菜单递归搜索）；网页录音改 MediaRecorder 上传 → 本地 faster-whisper 识别，不再依赖浏览器 SpeechRecognition |
| 五 · 应用解析 | ✅ 已落地 | 应用名解析通用化（归一化 + 模糊匹配）已落地（2026-07-23）：口语化说法（如「chrome浏览器」「微信电脑版」）可命中开始菜单与桌面快捷方式，不打别名补丁 |
| 五 · 升级 | ✅ 已落地 | 人格可靠化 + run_shell 通用执行 + NEGA 风界面（2026-07-23）：人格正式精确简练、先确认后执行、如实汇报、承认无知、不搞笑；run_shell 替代 run_command（任意 cmd/PowerShell + 三级安全闸）；网页版黑底全屏声波可视化界面 |
| 五 · 可靠 | ✅ 已落地 | LLM 调用可靠性（重试 + 日志透明化 + 超时 60s）+ 管家音色 zh-CN-YunjianNeural（edge-tts 重试 + SAPI 兜底）+ 网页版麦克风权限引导与默认浏览器自动打开（2026-07-23） |
| 五 · 进化 | ✅ 已落地 | 服务端录音语音输入 + media_control 媒体控制 + 判断力与诚实汇报 prompt（2026-07-23）：网页语音改服务端 sounddevice 录音（麦克风属于机器不属于浏览器，前端只当遥控器，无需浏览器权限，内嵌窗口也能用）；新增 media_control 工具（Windows 媒体键 ctypes 零依赖：播放暂停/切歌/音量/静音，工具数 9→10）；大脑 prompt 明确能力边界（shell 够不到 GUI 内部）与如实汇报 |
| 五 · 手之眼 | ✅ 已完成（2026-07-23）能看懂屏幕并操作界面 | 落地 eyes.py 屏幕感知 + gui_control 工具（glm-4v-flash 视觉 + pyautogui 鼠标键盘自动化）：截屏 → 视觉模型理解返回动作 JSON → pyautogui 执行 → 循环直到完成；安全闸=步数上限 + FAILSAFE（鼠标甩角落中止）+ 执行前需主人确认（复用 [[NEEDS_CONFIRM]] 待确认状态机）；禁止事项硬编码进感知 prompt：输密码、支付、删文件、给联系人发消息 |
| 五 · 闹钟可靠 | ✅ 已落地 | 闹钟服务闭环（叫醒意图+到点必响）+ 网页版浏览器发声（2026-07-23）：「叫醒我/叫我起床/闹钟」说法进入提醒系统（默认内容「起床啦，先生」），到点服务端音箱连续播报两遍；/api/chat 与 /api/due 携带 edge-tts 合成缓存的音频 URL，前端 <audio> 直接在浏览器播放 |
| 五 · 声音必达 + 大脑通用化 | ✅ 已落地 | GLM-TTS 男声三级发声链 + 大脑 Agent 循环通用化 + 声音测试按钮（2026-07-23） |
| 五 · 单实例与链式协作 | ✅ 已落地 | 后端单实例守卫 + capture_screen/set_web_background 通用截屏与背景 + 链式协作（2026-07-23）：server.py 启动自动清理旧进程并独占绑定端口（修复声音等功能时好时坏的根源）；capture_screen 截屏 + glm-4v-flash 定位任意界面元素裁剪存图；set_web_background 写 web_background.json，/api/background + 前端轮询自动应用（深色遮罩保证可读）；「把网易云喜欢列表第一首歌的封面设为聊天背景」由 Agent 循环串联 open_app/gui_control → capture_screen → set_web_background，工具数 11→13 |
| 五 · 视觉升级与链路强化 | ✅ 已落地 | 视觉升级 glm-4.5v + 任务链路强化（应用自动打开/失败具体化/动词过滤）让简单任务一次做对（2026-07-23） |
| 五 · 前导内置 | ✅ 已落地 | gui_control 自动开应用前导（窗口检测+自动打开+等待置前），GUI 任务不再对空气操作（2026-07-23） |
| 五 · 加固 | ✅ 已落地 | 常用应用别名扩充（网易云/QQ音乐/酷狗/抖音/B站/爱奇艺/腾讯视频/Steam/钉钉/飞书/QQ/Word/PPT/迅雷/百度网盘）+ /api/version 版本端点（一眼可验后端是否为新代码）（2026-07-23） |
| 五 · 人格 | 像"他" | 锁定音色、性格 prompt、情绪语调 |
| 六 · 常驻 | 永远在听 | 唤醒词引擎、看板 Widget 可视化状态、开机自启 |

## Nolan 成长路线图（第一性原理）

**原则**：每个阶段交付一个物理上可验证的新能力，可靠性（能力 × 可控）优先于功能数量。

| 阶段 | 目标 | 内容 | 完成标志 |
|---|---|---|---|
| 六 · 常驻唤醒 | 永远在听 | 本地离线唤醒词引擎 + 开机自启 + 系统托盘 | 息屏状态下喊「Nolan」3 秒内响应 |
| 七 · 主动智能 | 不用你开口 | 例程系统：『早上好』= 天气 + 日程 + 新闻播报、盯盘提醒、久坐提醒 | 连续 7 天主动播报无误触发 |
| 八 · 记忆深化 | 越用越懂你 | 语义检索 + 偏好自动沉淀 | 30 天后仍能准确回答主人的偏好 |
| 九 · 声音人格 | 像"他" | 本地 TTS 克隆专属音色 + 情绪语调 | 盲听能区分默认音色 |
| 十 · 产品化 | 开箱即用 | PyInstaller 打包 exe + 设置界面 + 工具插件化 | 干净 Windows 机器双击即用 |
