# Nolan

**Local-first Chinese voice AI butler for Windows — 会听、会说、会动手操作电脑的本地 AI 管家。**

> Nolan is a local-first, Chinese-speaking voice assistant for Windows. It listens with faster-whisper, thinks with a GLM-5.2 agent loop driving 14 tools (including arbitrary sandboxed shell commands), sees your screen and automates the GUI, remembers you long-term, and talks back with a GLM-TTS male voice. It ships with both a desktop (tkinter) app and a NEGA-styled web UI (React + Vite). Everything runs on your own machine — your API key, memory, and files never leave the repo folder.

> 🇨🇳 以下为中文详细版。

---

## Nolan 是什么

Nolan 是一个**本地优先的 Windows 中文语音 AI 管家**：对着麦克风说中文，它不仅能听懂、会回答，还能真的动手——执行 shell 命令、操作应用程序、控制媒体播放、看屏幕、记事情、定闹钟。它不是云端 SaaS，你的记忆、文件和 API Key 全部留在自己电脑上。

## ✨ 特性

- 🎙️ **语音对话**：faster-whisper 本地中文识别 + 自然语音播报，全程免键盘
- 🧠 **长期记忆**：记住你的偏好与事实，跨会话回忆（`jarvis/memory/` 本地存储）
- ⏰ **主动提醒与闹钟**：「1 分钟后叫醒我」「明天提醒我开会」——到点主动弹出并播报
- 🤖 **Agent 多步工具循环**：GLM-5.2 驱动，一次请求可连续调用多个工具直至任务完成
- 🛠️ **14 个工具**：含沙盒化**任意 shell 命令**、文件读写、通用应用打开、系统信息等
- 👁️ **屏幕感知与 GUI 自动化**：截屏 + GLM-4.5V 视觉理解 + pyautogui 键鼠操作
- 🎵 **媒体控制**：播放 / 暂停 / 切歌 / 音量调节（Windows 媒体键）
- 🖥️ **网页版 NEGA 风界面**：React + Vite 构建的复古未来风驾驶舱（`nolan-web/`）
- 🗣️ **GLM-TTS 男声**：低沉男声播报，edge-tts 离线兜底
- 🛡️ **三级安全闸**：危险命令分级拦截（自动放行 → 确认执行 → 硬性拒绝）

## 🏗️ 架构

```
        你（中文语音）
            │
            ▼
      ┌───────────┐     ┌────────────────────────────────┐
      │  耳朵 ears │ ──▶ │            大脑 brain           │
      │ whisper   │     │   GLM-5.2 Agent 多步工具循环     │
      └───────────┘     └───┬───────────┬───────────┬────┘
                            ▼           ▼           ▼
                      ┌──────────┐ ┌────────┐ ┌──────────┐
                      │ 手 hands │ │眼 eyes │ │记忆+提醒 │
                      │ 14 个工具 │ │截屏+GUI│ │ memory   │
                      │ 含 shell │ │ 自动化  │ │ reminders│
                      └──────────┘ └────────┘ └──────────┘
                            │
                            ▼
                      ┌───────────┐
                      │  嘴巴 mouth│ ──▶ GLM-TTS 男声播报
                      └───────────┘        （edge-tts 离线兜底）
```

## 🚀 Quickstart

### 1. 安装依赖

```bash
# Python 依赖（建议 Python 3.10+，虚拟环境）
pip install sounddevice faster-whisper edge-tts pygame numpy httpx beautifulsoup4 pyttsx3 pyautogui mss

# 网页版前端依赖
cd nolan-web
npm install
```

### 2. 配置智谱 API Key

```bash
# 复制示例配置并填入你自己的智谱 API Key（https://open.bigmodel.cn）
cp jarvis/llm_config.example.json jarvis/llm_config.json
# 编辑 llm_config.json，把 api_key 改成你的真实 Key
```

> `llm_config.json` 已在 `.gitignore` 中，不会被提交。

### 3. 启动

**网页版（推荐）**

```bash
cd nolan-web
npm run dev        # 同时拉起 Python 后端 (7101) 与 Vite 前端
# 打开终端打印的地址（Chrome/Edge，语音输入需要麦克风权限）
```

**桌面版（tkinter GUI）**

```bash
python jarvis/nolan_app.py    # 或直接双击 Nolan.bat
```

**命令行语音模式**

```bash
python jarvis/jarvis.py       # 或双击 Nolan-CLI.bat
```

> 💡 三个 `.bat` 启动器会优先使用 PATH 中的 Python（`where python`），找不到再回退到内置托管运行时；也可用 `NOLAN_PYTHON` 环境变量覆盖 `nolan-web/scripts/dev.mjs` 的解释器选择。

## 🎬 演示

> 📹 **GIF 演示待补充** —— 语音对话 + 工具调用 + 网页版驾驶舱的录屏将放在这里。

## ⚖️ 商用与 API 条款提醒

Nolan 依赖[智谱开放平台](https://open.bigmodel.cn)的 GLM 系列模型（GLM-5.2 / GLM-4.5V / GLM-TTS）。**智谱 API 的免费额度仅限个人非商用体验**；将 Nolan 或其能力用于商业用途前，请务必阅读并遵守智谱开放平台的[服务条款与商用授权政策](https://open.bigmodel.cn)，自行取得相应授权。本项目作者不承担因违反第三方 API 条款产生的任何责任。

## 📄 License

[MIT License](LICENSE) © 2026 xcq20100224

---

### Repo layout

```
Nolan/
├── jarvis/          # Python 后端：ears / brain / hands / eyes / memory / mouth + 自测
│   ├── llm_config.example.json   # 配置模板（复制为 llm_config.json 并填 Key）
│   └── test_jarvis.py            # 免麦克风全链路自动测试
├── nolan-web/       # React + Vite 网页版（NEGA 风驾驶舱）+ Python 标准库后端
├── Nolan.bat        # 桌面 GUI 启动器
├── Nolan-CLI.bat    # 命令行语音模式启动器
└── Nolan-Web.bat    # 网页版一键启动器
```
