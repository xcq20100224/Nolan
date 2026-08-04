// Nolan NEGA 主界面：黑底全屏 + 中央声波可视化 + 字幕式对话 + 历史浮层
// 交互逻辑与 API 契约保持原样：/api/health、/api/chat、/api/due 轮询、/api/memory、/api/reminders、exit 禁用
import { useCallback, useEffect, useRef, useState } from 'react'
import NegaHeader from '@/sections/NegaHeader'
import WaveCanvas from '@/sections/WaveCanvas'
import SubtitleBar from '@/sections/SubtitleBar'
import HistoryOverlay from '@/sections/HistoryOverlay'
import NegaInput from '@/sections/NegaInput'
import type { WaveMode } from '@/sections/WaveCanvas'
import type { Message } from '@/types/message'
import { checkHealth, sendChatStream, getDueMessages, getGreeting, getWakeState, setWake, getWakeEvents, getMemoryText, getRemindersText, playAudio, enqueueAudio, stopAllAudio, stopSpeak, soundTest, getBackground, clientLog } from '@/lib/api'

/** 当前时间，格式 HH:MM（24 小时制） */
function nowHHMM(): string {
  const d = new Date()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

/** 生成消息唯一 ID */
let seq = 0
function nextId(): string {
  seq += 1
  return `${Date.now()}-${seq}`
}

/**
 * 顺序播放一组音频地址：用 ended 事件串联，避免多条到点提醒同时叠音。
 * 某条播放失败（解码错误 / 自动播放被拦截）时跳到下一条，保证队列不死锁。
 */
function playInSequence(urls: string[]): void {
  const [head, ...rest] = urls
  if (!head) return
  const audio = playAudio(head)
  if (!audio) {
    playInSequence(rest)
    return
  }
  let advanced = false
  const next = () => {
    if (advanced) return
    advanced = true
    playInSequence(rest)
  }
  audio.addEventListener('ended', next, { once: true })
  audio.addEventListener('error', next, { once: true })
  // 兜底：自动播放被浏览器拒绝时 ended/error 都不会触发，
  // 1.5 秒后若仍 paused 且未开始过，视为未出声，直接推进队列
  window.setTimeout(() => {
    if (audio.paused && audio.currentTime === 0) next()
  }, 1500)
}

export default function ChatApp() {
  const [messages, setMessages] = useState<Message[]>([])
  const [online, setOnline] = useState(false)
  const [exited, setExited] = useState(false)
  const [wakeOn, setWakeOn] = useState(false)
  /** 等待 brain 回复中（驱动声波快速律动） */
  const [busy, setBusy] = useState(false)
  /** 录音中（服务端录音，驱动声波切换为模拟律动） */
  const [recording, setRecording] = useState(false)
  /** 历史浮层开关 */
  const [historyOpen, setHistoryOpen] = useState(false)
  /** 网页背景图地址（/api/background 轮询结果，null 时保持纯黑底） */
  const [bgUrl, setBgUrl] = useState<string | null>(null)

  // 防止 React StrictMode 开发模式下副作用执行两次导致重复欢迎语
  const bootedRef = useRef(false)
  // 会话结束后不再追加任何消息
  const exitedRef = useRef(false)

  /** 当前流式请求的中止器：新消息进场时中止未完成的上一轮 */
  const streamAbortRef = useRef<AbortController | null>(null)

  /** 追加一条 Nolan 消息 */
  const pushNolan = useCallback((text: string) => {
    if (exitedRef.current) return
    setMessages((prev) => [...prev, { id: nextId(), role: 'nolan', text, time: nowHHMM() }])
  }, [])

  /** 发送用户消息并请求 brain 回复（句级流式：LLM 边想、TTS 边产、喇叭边播） */
  const handleSend = useCallback(async (text: string) => {
    if (exitedRef.current) return

    // 0. 新一轮开口即打断上一轮：中止未完成的流式请求 + 清空浏览器播报队列 + 服务端音箱
    streamAbortRef.current?.abort()
    stopAllAudio()
    stopSpeak()

    // 1. 插入用户消息
    setMessages((prev) => [...prev, { id: nextId(), role: 'user', text, time: nowHHMM() }])

    // 2. 插入「请稍候」占位，声波进入快速律动
    const placeholderId = nextId()
    setMessages((prev) => [
      ...prev,
      { id: placeholderId, role: 'nolan', text: '先生，请稍候。', time: nowHHMM(), pending: true },
    ])
    setBusy(true)

    // 3. 流式请求：delta 逐字上字幕，sentence 逐句排队播，fallback 按整段处理
    let acc = ''
    const ctrl = new AbortController()
    streamAbortRef.current = ctrl
    const patchReply = (replyText: string) =>
      setMessages((prev) =>
        prev.map((m) =>
          m.id === placeholderId ? { ...m, text: replyText, time: nowHHMM(), pending: false } : m,
        ),
      )
    try {
      await sendChatStream(
        text,
        {
          // LLM 增量文本：字幕逐字出现（不等任何音频，感知延迟的第一刀）
          onDelta: (piece) => {
            acc += piece
            patchReply(acc)
          },
          // 一句合成好即入队播放（playInSequence 的边收边播动态队列版）
          onSentence: (s) => {
            if (s.audio_url) enqueueAudio(s.audio_url)
          },
          // 全量收尾：以服务端权威全量文本为准
          onDone: (d) => {
            if (d.reply) {
              acc = d.reply
              patchReply(d.reply)
            }
          },
          // 回退整段：规则意图/工具调用/流式失败——行为与旧 /api/chat 完全一致
          onFallback: (d) => {
            acc = d.reply
            patchReply(d.reply)
            if (d.audio_url) playAudio(d.audio_url)
            if (d.exit) {
              exitedRef.current = true
              setExited(true)
            }
          },
        },
        ctrl.signal,
      )
    } catch {
      // 被自己打断（新一轮已接管）：静默，不覆盖新消息
      if (ctrl.signal.aborted) return
      patchReply('先生，后端暂时无响应，请稍后再试。')
    } finally {
      // 只有当前这一轮仍是最新轮时才收尾 busy（防老轮 finally 抢掉新轮的律动）
      if (streamAbortRef.current === ctrl) setBusy(false)
    }
  }, [])

  // 挂载：健康检查 + 欢迎语（各执行一次）
  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true

    clientLog('页面加载 build 0804-1')
    checkHealth().then((ok) => {
      clientLog(`健康检查: ${ok}`)
      setOnline(ok)
    })
    // 主动晨报（J5）：每天第一次打开时 Nolan 主动问候（时段+日期+待办提醒），
    // 当天已问候或后端不可用时退回静态欢迎语
    const fallbackWelcome = () =>
      setMessages([{ id: nextId(), role: 'nolan', text: '先生，Nolan 在线，请讲。', time: nowHHMM() }])
    getGreeting()
      .then((g) => {
        if (g.text) {
          setMessages([{ id: nextId(), role: 'nolan', text: g.text, time: nowHHMM() }])
          playAudio(g.audio_url)
        } else {
          fallbackWelcome()
        }
      })
      .catch(fallbackWelcome)
  }, [])

  // 每 15 秒轮询到点提醒：逐条作为 Nolan 消息插入字幕（前置 ⏰ 以示闹钟），并按顺序播放合成语音
  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const due = await getDueMessages()
        if (cancelled) return
        // 字幕：闹钟消息前置 ⏰，在 NEGA 黑金界面中保持克制的醒目
        due.forEach((msg) => pushNolan(`⏰ ${msg.text}`))
        // 语音：收集非空 audio_url，用 ended 事件串联依次播放，避免叠音
        playInSequence(
          due.map((m) => m.audio_url).filter((u): u is string => typeof u === 'string' && u !== ''),
        )
      } catch {
        // 后端短暂不可用时静默跳过，等待下一轮
      }
    }
    const timer = window.setInterval(poll, 15_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pushNolan])

  // 每 15 秒轮询网页背景：首轮延迟 5 秒，与上方 /api/due 轮询（挂载即发起）错开，避免后端瞬时双倍请求。
  // 拿到非空 image_url 时更新背景图；为 null 时恢复纯黑底；请求失败静默保留当前背景。
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const url = await getBackground()
        if (!cancelled) setBgUrl(url)
      } catch {
        // 后端短暂不可用时静默跳过，保留当前背景
      }
    }
    const starter = window.setTimeout(() => {
      void poll()
      timer = window.setInterval(() => void poll(), 15_000)
    }, 5_000)
    return () => {
      cancelled = true
      window.clearTimeout(starter)
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [])

  // 「记忆」按钮：查询长期记忆并作为 Nolan 消息插入
  const handleMemory = useCallback(async () => {
    try {
      pushNolan(await getMemoryText())
    } catch {
      pushNolan('先生，记忆读取失败了。')
    }
  }, [pushNolan])

  // 「提醒」按钮：查询提醒列表并作为 Nolan 消息插入
  const handleReminders = useCallback(async () => {
    try {
      pushNolan(await getRemindersText())
    } catch {
      pushNolan('先生，提醒列表读取失败了。')
    }
  }, [pushNolan])

  // 「声音测试」按钮（右上角 🔊）：浏览器 + 音箱双通道同时发声，验证发声链是否完好
  const handleSoundTest = useCallback(async () => {
    try {
      const data = await soundTest()
      // audio_url 非空时在浏览器里播放同一句话（服务端音箱已由后端同步播报）
      if (data.audio_url) playAudio(data.audio_url)
      pushNolan('先生，声音测试已发出——您应该同时从浏览器和音箱听到我的声音。若都没有，请检查系统音量。')
    } catch {
      pushNolan('先生，声音测试请求失败了，请检查后端是否在线。')
    }
  }, [pushNolan])

  // 「唤醒词」开关（右上角）：开启后服务端耳蜗常驻，说「诺兰」即回应；
  // 状态落盘在后端，挂载时同步真实状态
  const handleWakeToggle = useCallback(async () => {
    try {
      const next = !wakeOn
      const st = await setWake(next)
      setWakeOn(st.enabled && st.listening)
      pushNolan(st.enabled ? '先生，唤醒词已开启。对麦克风说「诺兰」，我随时在。' : '好的先生，唤醒词已关闭。')
    } catch {
      pushNolan('先生，唤醒词开关失败了，请检查后端是否在线。')
    }
  }, [wakeOn, pushNolan])

  // 挂载时同步唤醒词真实状态（状态落盘在后端，可能已开启）
  useEffect(() => {
    getWakeState()
      .then((st) => setWakeOn(st.enabled && st.listening))
      .catch(() => {})
  }, [])

  // 耳蜗开启时 2.5 秒轮询事件：
  //   wake 事件 → 播报确认音 + 字幕提示（原有行为）；
  //   bargein 事件（P3 全双工）→ 主人打断了播报：立即停掉浏览器播报，
  //   把听到的指令原文作为用户消息自动发送（物理闭环：开口 → 静音 → 执行）
  useEffect(() => {
    if (!wakeOn) return
    let cancelled = false
    const poll = async () => {
      try {
        const data = await getWakeEvents()
        if (cancelled) return
        data.events.forEach((ev) => {
          if (ev.kind === 'bargein' && ev.text) {
            clientLog(`打断事件: ${ev.text.slice(0, 30)}`)
            stopAllAudio()
            stopSpeak() // 双通道都静音：浏览器 + 服务端音箱（闹钟场景）
            pushNolan('⏸️ 收到打断，先生。')
            handleSend(ev.text) // 指令原文自动发送，走正常对话链路
            return
          }
          pushNolan(ev.text)
          if (ev.audio_url) playAudio(ev.audio_url)
        })
      } catch {
        // 后端短暂不可用时静默跳过
      }
    }
    const timer = window.setInterval(poll, 2_500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [wakeOn, pushNolan, handleSend])

  // 声波模式：录音 > 等待回复 > 闲置呼吸
  const waveMode: WaveMode = recording ? 'recording' : busy ? 'busy' : 'idle'

  return (
    // 根容器改为相对定位的三层结构：背景图层（z-0）→ 深色遮罩（z-0）→ 前景内容（z-10）
    // bgUrl 为 null 时两层背景均以 opacity-0 隐藏，页面回到纯黑底，切换由 0.5s 过渡完成
    <div className="relative h-screen overflow-hidden bg-[#0b0b0d] text-[#e8e0d0]">
      {/* 背景图层：cover center 铺满，0.5s 淡入淡出 */}
      <div
        aria-hidden
        className="absolute inset-0 z-0 bg-cover bg-center transition-opacity duration-500"
        style={{
          backgroundImage: bgUrl ? `url(${bgUrl})` : undefined,
          opacity: bgUrl ? 1 : 0,
        }}
      />
      {/* 深色遮罩：压在背景图之上，保证 NOLAN 标题、声波、字幕、输入框清晰可读 */}
      <div
        aria-hidden
        className={`absolute inset-0 z-0 bg-black/70 transition-opacity duration-500 ${
          bgUrl ? 'opacity-100' : 'opacity-0'
        }`}
      />

      {/* 前景内容：抬升到 z-10，布局与原结构一致 */}
      <div className="relative z-10 flex h-full flex-col overflow-hidden">
        <NegaHeader
          online={online}
          exited={exited}
          onMemory={handleMemory}
          onReminders={handleReminders}
          onHistory={() => setHistoryOpen(true)}
          onSoundTest={handleSoundTest}
          wakeOn={wakeOn}
          onWakeToggle={handleWakeToggle}
        />

        {/* 中央：声波 + 状态提示 + 字幕 */}
        <main className="flex min-h-0 flex-1 flex-col items-center justify-center px-4">
          {/* 服务端录音后浏览器不再有真实波形流，stream 恒为 null（录音态由 mode='recording' 驱动模拟律动） */}
          <WaveCanvas mode={waveMode} stream={null} />
          <p className="mt-4 h-4 text-[11px] font-light tracking-[0.25em] text-[#4a4a50]">
            {exited
              ? '会话已结束'
              : recording
                ? '正在聆听，先生'
                : busy
                  ? '思考中'
                  : online
                    ? ''
                    : '后端离线'}
          </p>
          <SubtitleBar messages={messages} onOpen={() => setHistoryOpen(true)} />
        </main>

        <NegaInput
          disabled={exited}
          onSend={handleSend}
          onRecordingChange={setRecording}
          onStatus={pushNolan}
        />

        {historyOpen && <HistoryOverlay messages={messages} onClose={() => setHistoryOpen(false)} />}

        {/* 构建水印：排查「页面跑的是旧缓存」用——截图带它即可确认前端版本 */}
        <span className="pointer-events-none absolute bottom-2 right-3 text-[10px] font-light tracking-widest text-[#3a3a40]">
          build 0804-1
        </span>
      </div>
    </div>
  )
}
