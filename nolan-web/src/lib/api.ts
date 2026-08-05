// Nolan 后端 API 封装
// 默认走相对路径 '/api/...'（vite 代理到 7101）；
// 内嵌 webview 被证实存在『代理链路响应丢失』问题，故关键端点支持
// 直连兜底：http://127.0.0.1:7101（CORS 已放开，GET 为简单请求无预检）

/** 直连后端的兜底地址（绕过 vite 代理这一中间人） */
const DIRECT_BASE = 'http://127.0.0.1:7101'

/** 通用 JSON 请求助手：失败时抛出带说明的错误 */
async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`请求失败：${url}，状态码 ${res.status}`)
  }
  return (await res.json()) as T
}

/**
 * 手动超时 fetch：AbortSignal.timeout 在部分内嵌 webview 不可用（且对排队中的
 * 请求行为不一），用 AbortController + setTimeout 实现同等能力，兼容性最好
 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit | undefined,
  timeoutMs: number,
): Promise<Response> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: ctrl.signal })
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * 前端黑匣子：把诊断事件 fire-and-forget 上报到后端 /api/clientlog 落盘。
 * 关键特性——只依赖『请求能到达后端』，不依赖响应（响应丢失已在本机
 * webview 上被证实）。代理、直连双通道各发一份，确保至少一条到达。
 */
export function clientLog(msg: string): void {
  const q = `/api/clientlog?m=${encodeURIComponent(msg)}`
  try {
    void fetch(q).catch(() => undefined)
  } catch {
    /* 忽略 */
  }
  try {
    void fetch(DIRECT_BASE + q).catch(() => undefined)
  } catch {
    /* 忽略 */
  }
}

/**
 * 停止说话：POST /api/stop，立即打断服务端音箱当前播报。
 * 仿 clientLog 的 fire-and-forget 风格：只保证请求尽量到达，不依赖响应。
 */
export function stopSpeak(): void {
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }
  try {
    void fetch('/api/stop', init).catch(() => undefined)
  } catch {
    /* 忽略 */
  }
}

/** 健康检查：GET /api/health → {"ok": true, "name": "Nolan"} */
export async function checkHealth(): Promise<boolean> {
  try {
    const data = await fetchJson<{ ok: boolean; name: string }>('/api/health')
    return data.ok === true
  } catch {
    return false
  }
}

/**
 * 聊天：POST /api/chat {"text"} → {"reply", "audio_url", "exit"?}
 * audio_url 为 edge-tts 合成的音频地址（/api/tts/<sha1>.mp3），合成失败时为 null
 */
export async function sendChat(
  text: string,
): Promise<{ reply: string; exit?: boolean; audio_url: string | null }> {
  return fetchJson<{ reply: string; exit?: boolean; audio_url: string | null }>('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ text }),
  })
}

/** 句级流式对话的事件回调（/api/chat/stream，SSE） */
export interface StreamChatHandlers {
  /** LLM 增量文本：字幕逐字出现，不等音频 */
  onDelta?: (piece: string) => void
  /** 一句合成完毕：audio_url 非空即可 enqueueAudio 排队播放 */
  onSentence?: (sentence: { text: string; audio_url: string | null }) => void
  /** 全量收尾：reply 为完整回复（degraded 表示流中断按部分内容收尾） */
  onDone?: (d: { reply: string; degraded?: boolean }) => void
  /** 回退整段：与 /api/chat 响应完全同形（规则意图/工具调用/流式早期失败） */
  onFallback?: (d: { reply: string; audio_url: string | null; exit?: boolean }) => void
}

/**
 * 句级流式聊天：POST /api/chat/stream（SSE，fetch + ReadableStream 手解分帧）。
 * LLM 边产出边推 delta，后端句级流水线合成好一句推一句，前端边收边播。
 * 回退契约：旧后端无端点（404）时自动改走 sendChat 整段并以 onFallback 上交——
 * 对话绝不因流式化而挂掉。signal 用于新消息进场时中止未完成的上一轮。
 */
export async function sendChatStream(
  text: string,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
    signal,
  })
  if (res.status === 404) {
    // 陈旧后端（无流式端点）：整段回退，行为与旧版完全一致
    handlers.onFallback?.(await sendChat(text))
    return
  }
  if (!res.ok || !res.body) {
    throw new Error(`请求失败：/api/chat/stream，状态码 ${res.status}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    // SSE 分帧：事件之间以空行（\n\n）分隔，逐行取 data: 负载
    let sep = buf.indexOf('\n\n')
    while (sep >= 0) {
      const rawEvent = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      for (const line of rawEvent.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          const ev = JSON.parse(line.slice(5).trim()) as Record<string, unknown> & {
            type?: string
          }
          if (ev.type === 'delta') {
            handlers.onDelta?.(String(ev.text ?? ''))
          } else if (ev.type === 'sentence') {
            handlers.onSentence?.({
              text: String(ev.text ?? ''),
              audio_url: typeof ev.audio_url === 'string' ? ev.audio_url : null,
            })
          } else if (ev.type === 'done') {
            handlers.onDone?.({ reply: String(ev.reply ?? ''), degraded: ev.degraded === true })
          } else if (ev.type === 'fallback') {
            handlers.onFallback?.({
              reply: String(ev.reply ?? ''),
              audio_url: typeof ev.audio_url === 'string' ? ev.audio_url : null,
              exit: ev.exit === true,
            })
          }
        } catch {
          // 单条事件解析失败：跳过该帧，不中断整流
        }
      }
      sep = buf.indexOf('\n\n')
    }
  }
}

/** 到点提醒单条消息结构：文本 + 可选的合成语音地址 */
export interface DueMessage {
  text: string
  audio_url: string | null
}

/** 到点提醒：GET /api/due → {"messages": [{"text", "audio_url"}]}（无到点为空数组） */
export async function getDueMessages(): Promise<DueMessage[]> {
  const data = await fetchJson<{ messages: DueMessage[] }>('/api/due')
  return Array.isArray(data.messages) ? data.messages : []
}

/**
 * 当前正在播放的 Audio 实例（全局唯一注册点）。
 * playAudio 每次开播前停掉上一个——同一时刻只应有一个 Nolan 在出声，
 * 这也是「打断」的物理执行点：stopAllAudio() 一调，浏览器通道立即静音。
 */
let currentAudio: HTMLAudioElement | null = null

/**
 * 句级流式播放队列（Gap1 流式化）：/api/chat/stream 合成好一句推一句，
 * 前端 enqueueAudio 按序连播——playInSequence 的「边收边播」动态队列版。
 * stopAllAudio 一并清空队列：手动/语音打断时，未播的句子不再出声。
 */
let audioQueue: string[] = []
let audioQueueBusy = false

/** 停掉当前正在播放的 Audio（不动播放队列；playAudio 开播前的内部清理点） */
function stopCurrentAudio(): void {
  if (currentAudio) {
    try {
      currentAudio.pause()
      currentAudio.removeAttribute('src')
      currentAudio.load()
    } catch {
      /* 忽略 */
    }
    currentAudio = null
  }
}

/**
 * 立即停止浏览器通道的全部播报（手动/语音打断共用）。
 * 清空句级队列 + pause 当前 + 摘掉 src 双保险：部分内嵌 webview 对单独 pause 响应迟缓。
 */
export function stopAllAudio(): void {
  audioQueue = []
  audioQueueBusy = false
  stopCurrentAudio()
}

/** 把一句合成好的音频地址追加进流式播放队列；队列空闲时立即开播 */
export function enqueueAudio(url: string): void {
  if (!url) return
  audioQueue.push(url)
  pumpAudioQueue()
}

/** 队列泵：串行播放，ended/error 推进下一条；某条失败跳过，保证队列不死锁 */
function pumpAudioQueue(): void {
  if (audioQueueBusy) return
  const url = audioQueue.shift()
  if (!url) return
  audioQueueBusy = true
  const audio = playAudio(url)
  if (!audio) {
    audioQueueBusy = false
    pumpAudioQueue()
    return
  }
  let advanced = false
  const advance = () => {
    if (advanced) return
    advanced = true
    audioQueueBusy = false
    pumpAudioQueue()
  }
  audio.addEventListener('ended', advance, { once: true })
  audio.addEventListener('error', advance, { once: true })
  // 兜底：自动播放被浏览器拒绝时 ended/error 都不触发，
  // 1.5 秒后若仍 paused 且未开始过，视为未出声，直接推进队列（与 playInSequence 同思路）
  window.setTimeout(() => {
    if (audioQueueBusy && audio.paused && audio.currentTime === 0) advance()
  }, 1500)
}

/**
 * 播放一段音频：url 非空时创建 Audio 并尝试播放。
 * play() 返回的 promise 被浏览器自动播放策略拒绝时静默吞掉（用户交互过页面后通常允许），
 * 返回 Audio 实例，方便调用方用 ended 事件串联多段音频。
 * 开播前自动停掉上一条（注册表语义；不清流式队列——队列泵正是靠它逐条开播），
 * 播完自动出清注册表。
 */
export function playAudio(url: string | null): HTMLAudioElement | null {
  if (!url) return null
  stopCurrentAudio()
  const audio = new Audio(url)
  currentAudio = audio
  audio.addEventListener('ended', () => {
    if (currentAudio === audio) currentAudio = null
  })
  audio.addEventListener('error', () => {
    if (currentAudio === audio) currentAudio = null
  })
  audio.play().catch(() => {
    // 自动播放被拦截：静默失败，字幕已展示文本，不影响主流程
    if (currentAudio === audio) currentAudio = null
  })
  return audio
}

/** 主动晨报：GET /api/greeting → {greeted, text, audio_url}（每天首次有问候语） */
export async function getGreeting(): Promise<{ greeted: boolean; text: string | null; audio_url: string | null }> {
  return fetchJson('/api/greeting')
}

/** 唤醒词状态：GET /api/wake/state → {enabled, listening} */
export async function getWakeState(): Promise<{ enabled: boolean; listening: boolean }> {
  return fetchJson('/api/wake/state')
}

/** 唤醒词开关：POST /api/wake/toggle {enabled} */
export async function setWake(enabled: boolean): Promise<{ enabled: boolean; listening: boolean }> {
  return fetchJson('/api/wake/toggle', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
}

/** 唤醒/打断事件：GET /api/wake/events → {events: [{kind, text, audio_url}]}（出队即清空）。
 * kind='wake'（缺省兼容）：唤醒词命中，text 为确认音文案；
 * kind='bargein'：主人打断了播报，text 为听到的指令原文，audio_url 为 null */
export async function getWakeEvents(): Promise<{
  events: { kind?: 'wake' | 'bargein'; text: string; audio_url: string | null }[]
}> {
  return fetchJson('/api/wake/events')
}

/** 提醒列表：GET /api/reminders → {"text": "口语化提醒列表"} */
export async function getRemindersText(): Promise<string> {  const data = await fetchJson<{ text: string }>('/api/reminders')
  return data.text
}

/**
 * 语音识别：POST /api/asr，请求体为原始音频字节（audio/webm|ogg|wav）
 * 响应 {"text": "..."}，无语音或识别失败时 text 为空串
 */
export async function transcribe(blob: Blob): Promise<string> {
  const res = await fetch('/api/asr', {
    method: 'POST',
    body: blob,
    headers: { 'Content-Type': blob.type || 'audio/webm' },
  })
  if (!res.ok) {
    throw new Error(`请求失败：/api/asr，状态码 ${res.status}`)
  }
  const data = (await res.json()) as { text?: string }
  return typeof data.text === 'string' ? data.text : ''
}

/**
 * 服务端开始录音：GET /api/mic/start → {"ok": true}
 * 麦克风由服务端（sounddevice）直采，浏览器只当遥控器，无需任何浏览器权限。
 * 双通道点火：先走 vite 代理，失败再走 7101 直连（绕过代理中间人）——
 * 服务端录音幂等可重开，重复 start 无害；每通道 2 次尝试、3 秒上界。
 * 全程黑匣子落痕，响应丢失也能从后端日志还原链路。
 */
export async function micStart(): Promise<boolean> {
  for (const base of ['', DIRECT_BASE]) {
    const via = base === '' ? 'proxy' : 'direct'
    for (let attempt = 1; attempt <= 2; attempt++) {
      clientLog(`micStart 发起(${via}#${attempt})`)
      try {
        const res = await fetchWithTimeout(base + '/api/mic/start', undefined, 3000)
        clientLog(`micStart 响应(${via}#${attempt}) status=${res.status}`)
        if (res.ok) {
          const data = (await res.json()) as { ok?: boolean }
          if (data.ok === true) return true
        }
      } catch (e) {
        clientLog(`micStart 异常(${via}#${attempt}) ${e instanceof Error ? e.message : String(e)}`)
      }
      await new Promise((r) => setTimeout(r, 300))
    }
  }
  return false
}

/**
 * 服务端录音状态：GET /api/mic/state → {"recording": bool}
 * 前端发起 start 后轮询本端点确认服务端真实录音状态——不依赖单次 start 请求的
 * 响应本身，响应丢失也能自愈。代理失败自动切直连；null 表示两通道都失败。
 */
export async function micState(): Promise<boolean | null> {
  for (const base of ['', DIRECT_BASE]) {
    try {
      const res = await fetchWithTimeout(base + '/api/mic/state', undefined, 2000)
      if (!res.ok) continue
      const data = (await res.json()) as { recording?: boolean }
      return data.recording === true
    } catch {
      // 本通道失败，换直连通道
    }
  }
  return null
}

/**
 * 服务端停止录音并识别：POST /api/mic/stop → {"text": "..."}
 * 无语音 / 未在录音时返回空串；代理失败自动切直连；两通道都失败抛错。
 * medium 模型在 CPU 上识别长录音可能耗时数十秒，给 60 秒上界。
 */
export async function micStop(): Promise<string> {
  let lastErr: unknown = null
  for (const base of ['', DIRECT_BASE]) {
    const via = base === '' ? 'proxy' : 'direct'
    clientLog(`micStop 发起(${via})`)
    try {
      const res = await fetchWithTimeout(
        base + '/api/mic/stop',
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
        60_000,
      )
      clientLog(`micStop 响应(${via}) status=${res.status}`)
      if (!res.ok) {
        lastErr = new Error(`请求失败：/api/mic/stop，状态码 ${res.status}`)
        continue
      }
      const data = (await res.json()) as { text?: string }
      return typeof data.text === 'string' ? data.text : ''
    } catch (e) {
      clientLog(`micStop 异常(${via}) ${e instanceof Error ? e.message : String(e)}`)
      lastErr = e
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error('mic/stop 两通道均失败')
}

/**
 * 声音测试：GET /api/sound_test → {"audio_url": string | null, "speaker": true}
 * 双通道同时发声：服务端音箱播报（speaker 恒为 true），
 * audio_url 非空时前端在浏览器里同步播放同一句话（GLM-TTS 合成的 wav）
 */
export async function soundTest(): Promise<{ audio_url: string | null; speaker: boolean }> {
  return fetchJson<{ audio_url: string | null; speaker: boolean }>('/api/sound_test')
}

/** 长期记忆：GET /api/memory → {"text": "口语化记忆列表"} */
export async function getMemoryText(): Promise<string> {
  const data = await fetchJson<{ text: string }>('/api/memory')
  return data.text
}

/**
 * 网页背景：GET /api/background → {"image_url": string | null}
 * image_url 是可直接用于 <img>/background-image 的地址（/api/files/<相对路径>）；
 * 若后端只给了相对 files 的路径，这里统一补上 /api/files/ 前缀；
 * null 表示未设置背景，返回 null 让调用方恢复纯黑底。
 */
export async function getBackground(): Promise<string | null> {
  const data = await fetchJson<{ image_url: string | null }>('/api/background')
  const url = data.image_url
  if (typeof url !== 'string' || url === '') return null
  return url.startsWith('/') ? url : `/api/files/${url}`
}

/** 上传结果（POST /api/upload 响应，契约见 server.py 文件头） */
export interface UploadResult {
  ok: boolean
  /** 存储文件名（时间戳前缀 + 净化后的原名） */
  name: string
  /** 抽取文本总字数 */
  chars: number
  /** 前 2000 字摘要 */
  excerpt: string
  /** 全量抽取文本（发送时拼进对话 payload 用，前端按 8000 字截断） */
  text: string
  truncated?: boolean
  /** 文件柜下载地址（/api/files/uploads/<存储名>） */
  file_url: string
}

/**
 * 文件上传：POST /api/upload（base64 JSON 契约——标准库后端无 multipart 解析器）。
 * FileReader 读成 dataURL，取逗号后的 base64 段上送；失败抛带后端说明的错误。
 */
export async function uploadFile(file: File): Promise<UploadResult> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('文件读取失败'))
    reader.readAsDataURL(file)
  })
  const comma = dataUrl.indexOf(',')
  const base64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl
  const res = await fetch('/api/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: file.name, data_base64: base64 }),
  })
  const data = (await res.json()) as UploadResult & { error?: string }
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `上传失败：状态码 ${res.status}`)
  }
  return data
}

/** 文件柜条目（GET /api/files_list） */
export interface CabinetFile {
  /** 相对 files 目录的正斜杠路径（如 uploads/20260804-120000_报告.pdf） */
  name: string
  /** 字节数 */
  size: number
  /** 修改时间（epoch 秒） */
  mtime: number
  /** 分类：文档 / 图片 / 表格 / 音频 / 其他 */
  kind: string
}

/** 文件柜列表：GET /api/files_list（每次打开面板时刷新） */
export async function getFilesList(): Promise<CabinetFile[]> {
  const data = await fetchJson<{ files: CabinetFile[] }>('/api/files_list')
  return Array.isArray(data.files) ? data.files : []
}

/** 文件柜条目的查看/下载地址（name 为相对 files 的路径，逐段编码防中文/空格） */
export function cabinetFileUrl(name: string): string {
  return '/api/files/' + name.split('/').map(encodeURIComponent).join('/')
}
