# Show HN 发帖弹药（v2）

> ⚠️ **2026-08-04 实测**：HN 正在临时限制不熟悉社区账号的 Show HN
> （提交后被重定向到 news.ycombinator.com/showlim：「We're temporarily restricting
> Show HNs because of a massive influx...」）。本账号（零 karma）目前发不了 Show HN。
>
> **正确路径（官方建议）**：先用 2-4 周做正常社区成员——
> 每周在别人帖下留 2-3 条有干货的评论（不推广自己），攒到 ~20+ karma 后重发。
> 重发时用下面备好的标题与正文。
>
> 发帖时间：美西早上 7-9 点（北京时间 22:00-0:00）曝光最佳。

---

**标题（三选一，按推荐排序）：**

1. `Show HN: Nolan – Local-first Chinese voice butler that operates your Windows PC`
2. `Show HN: I built a Jarvis-style voice assistant that clicks, types, and can be interrupted mid-sentence`
3. `Show HN: Nolan – Voice-driven GUI automation for Windows with full-duplex barge-in`

**正文：**

```
Hi HN! I built Nolan, a local-first Chinese voice butler for Windows. You speak to it in Chinese; it doesn't just answer — it operates your computer: opens apps, clicks and types in GUIs, runs sandboxed shell commands, searches the web, remembers you long-term, and wakes you up on schedule.

Repo: https://github.com/xcq20100224/Nolan
Demo (unedited, real-time): see docs/demo.gif in the repo — "open NetEase Cloud Music and play the first song in my favorites", and it drives the GUI by itself.

A few things that might be interesting to this crowd:

1. Full-duplex barge-in without AEC. While Nolan is speaking, you can just start talking and it stops and listens. Instead of acoustic echo cancellation, it uses two cheap defenses: an adaptive energy gate (its own TTS becomes the noise baseline) and echo text filtering (the server knows exactly what it's currently saying, so it compares ASR output against that). Both the CLI and web versions share the same design.

2. Conditional triggers. "If it rains tomorrow, remind me to take an umbrella" / "Whenever there's major AI news, tell me" / "Every 30 minutes remind me to drink water". Conditions are verified via web search at check time; actions can either just speak or actually execute tasks through the full agent loop.

3. Skill consolidation. Tasks it has done once are stored as parameterized templates and replayed instantly for similar requests (8/8 on my generalization benchmark).

4. Everything is benchmarked. 300-task single-step reservoir, a 57-question end-to-end regression suite (with real GUI actions) that runs on every commit, and an ASR benchmark that drove a model downgrade: faster-whisper medium→small with greedy decoding is 3.6x faster on CPU (1.53x real-time) with identical 92% accuracy. Beam-1 on medium was a trap (accuracy collapsed to 55% — greedy decoding breaks Simplified Chinese on that model); measurements first, changes second.

Stack: GLM-5.2 agent loop (14 tools), faster-whisper local ASR, GLM-TTS male voice with edge-tts fallback, pyautogui + GLM-4.5V for screen perception, React+Vite web UI. MIT licensed.

Happy to answer questions — especially about the barge-in design and the benchmark methodology.
```

**评论区首条自评（发完帖立刻贴，带节奏）：**

```
FAQ pre-empt: Why Chinese-first? I'm a native speaker and the Chinese voice-assistant
open-source space is much thinner than English. The architecture is language-agnostic —
swap the system prompt and wake words and it works in English. Why GLM instead of a local
LLM? The agent loop needs strong tool-calling; GLM-5.2's free tier was the best
price/performance I found. Everything except the LLM/TTS API calls runs locally.
```
