/**
 * Nolan 浏览器播放引擎（Web Audio 统一收编版）。
 *
 * 爆音诊断背景（「滴答滴答」——复数，每句开头一声）：
 *   旧链路全程 <audio> 元素：句级流式队列每播一句 new Audio() 一个、
 *   硬切硬停、无任何增益包络——三个爆音源全中：
 *     ① 硬切入：首采样非零直接全音量 → DC 阶跃 = 咔哒；
 *     ② 句级队列接缝：每句各爆一次，正是用户听到的「滴答滴答」；
 *     ③ audio 元素换 src / pause 瞬间的电平跳变。
 *
 * 本引擎的解法：
 *   - AudioContext 单例 + fetch/arrayBuffer/decodeAudioData + GainNode 包络：
 *     每段开头 12ms 线性淡入、结尾 10ms 淡出（接缝两侧都是软边，不爆）；
 *   - 停播也走包络：gain 先 ~8ms 时间常数拉到 0 再 stop()，打断时不爆第二声；
 *   - autoplay 策略：首次用户交互（pointerdown/keydown）自动 resume；
 *   - 兜底：fetch/decodeAudioData 失败（老 webview）回退原 <audio> 元素路径——
 *     声音永远优先于净化，绝不因净化失败而失声。
 *
 * 对外契约（与旧 api.ts 播放块完全一致）：
 *   playAudio(url) → PlaybackHandle | null（ended/error 事件、paused、currentTime）
 *   enqueueAudio(url) / stopAllAudio()
 */

/** 播放句柄：HTMLAudioElement 天然满足本接口（结构类型），Web Audio 路径用 WebHandle */
export interface PlaybackHandle {
  readonly paused: boolean
  readonly currentTime: number
  addEventListener(type: 'ended' | 'error', listener: () => void, options?: { once?: boolean }): void
  pause(): void
}

/** 句首线性淡入时长（秒）：硬切入 DC 阶跃 = 咔哒的解药 */
const FADE_IN_S = 0.012
/** 句尾淡出时长（秒）：句间接缝的另一半软边 */
const FADE_OUT_S = 0.01
/** 停播时 gain 拉零的时间常数（秒）：打断瞬间不爆第二声 */
const STOP_TAU_S = 0.008
/** 解码缓存上限（句级 URL 一次性使用，防内存膨胀） */
const BUFFER_CACHE_MAX = 8

// ---------------------------------------------------------------------------
// AudioContext 单例 + autoplay 自动恢复
// ---------------------------------------------------------------------------

let ctx: AudioContext | null = null

function getCtx(): AudioContext | null {
  try {
    const AC =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AC) return null
    if (!ctx) ctx = new AC()
    // autoplay 策略下可能是 suspended：每次开播都尝试恢复（用户交互过后通常成功）
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
    return ctx
  } catch {
    return null
  }
}

// 首次用户交互即解锁 AudioContext（capture 尽早拿到手势）
const unlockCtx = () => {
  if (ctx && ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
}
window.addEventListener('pointerdown', unlockCtx, true)
window.addEventListener('keydown', unlockCtx, true)

// ---------------------------------------------------------------------------
// 解码缓存与预取（缩小句间空隙：上一句开播时预取下一句）
// ---------------------------------------------------------------------------

const bufferCache = new Map<string, Promise<AudioBuffer>>()

/** 新旧两式 decodeAudioData 兼容（回调式全实现可用，promise 式双保险） */
function decodeBuffer(c: AudioContext, raw: ArrayBuffer): Promise<AudioBuffer> {
  return new Promise((resolve, reject) => {
    let settled = false
    const ok = (b: AudioBuffer) => {
      if (!settled) {
        settled = true
        resolve(b)
      }
    }
    const bad = (e?: unknown) => {
      if (!settled) {
        settled = true
        reject(e instanceof Error ? e : new Error('decodeAudioData 失败'))
      }
    }
    try {
      const p = c.decodeAudioData(raw, ok, bad)
      if (p && typeof p.then === 'function') p.then(ok, bad)
    } catch (e) {
      bad(e)
    }
  })
}

function loadBuffer(c: AudioContext, url: string): Promise<AudioBuffer> {
  const hit = bufferCache.get(url)
  if (hit) {
    // 用后出清：句级 URL 一次性，不长期占内存
    bufferCache.delete(url)
    return hit
  }
  const p = (async () => {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`音频拉取失败：${url}，状态码 ${res.status}`)
    return decodeBuffer(c, await res.arrayBuffer())
  })()
  bufferCache.set(url, p)
  // 失败不留毒缓存（下次重试走网络）
  p.catch(() => {
    if (bufferCache.get(url) === p) bufferCache.delete(url)
  })
  // 超容量逐出最旧条目（Map 保持插入序）
  while (bufferCache.size > BUFFER_CACHE_MAX) {
    const oldest = bufferCache.keys().next().value
    if (oldest === undefined) break
    bufferCache.delete(oldest)
  }
  return p
}

/** 预取一句音频的解码结果进缓存（队列泵在下一句开播前调用） */
function primeAudio(url: string | undefined): void {
  if (!url) return
  const c = getCtx()
  if (!c) return
  if (bufferCache.has(url)) return
  try {
    void loadBuffer(c, url)
  } catch {
    /* 预取失败无碍：开播时还有完整回退链 */
  }
}

// ---------------------------------------------------------------------------
// Web Audio 播放句柄：同步返回，异步开播/兜底，对调用方屏蔽两条路径的差异
// ---------------------------------------------------------------------------

class WebHandle implements PlaybackHandle {
  private endedListeners = new Set<() => void>()
  private errorListeners = new Set<() => void>()
  private c: AudioContext
  private startedAt: number | null = null
  private done = false
  private fallbackEl: HTMLAudioElement | null = null

  constructor(c: AudioContext) {
    this.c = c
  }

  addEventListener(type: 'ended' | 'error', listener: () => void, options?: { once?: boolean }): void {
    const set = type === 'ended' ? this.endedListeners : this.errorListeners
    if (options?.once) {
      const wrap = () => {
        set.delete(wrap)
        listener()
      }
      set.add(wrap)
    } else {
      set.add(listener)
    }
  }

  get paused(): boolean {
    if (this.fallbackEl) return this.fallbackEl.paused
    // 加载/解码待播期视为「意图播放中」：慢网络下不被队列泵 1.5 秒兜底误判跳过
    return this.done
  }

  get currentTime(): number {
    if (this.fallbackEl) return this.fallbackEl.currentTime
    if (this.startedAt === null) return 0
    return Math.max(0, this.c.currentTime - this.startedAt)
  }

  /** 引擎停播的执行点（注册表语义；本句柄不出声即止） */
  pause(): void {
    this.done = true
  }

  markStarted(): void {
    this.startedAt = this.c.currentTime
  }

  markStopped(): void {
    this.done = true
  }

  /** 自然播完：发 ended；解码/网络失败且兜底也失败：发 error */
  finish(kind: 'ended' | 'error'): void {
    if (this.done) return
    this.done = true
    const set = kind === 'ended' ? this.endedListeners : this.errorListeners
    set.forEach((fn) => {
      try {
        fn()
      } catch {
        /* 监听器异常不阻断播放链 */
      }
    })
  }

  /** Web Audio 链路失败，回退到 <audio> 元素：事件与 paused/currentTime 全部转发 */
  adoptFallback(el: HTMLAudioElement): void {
    if (this.done) return
    this.fallbackEl = el
    el.addEventListener('ended', () => this.finish('ended'))
    el.addEventListener('error', () => this.finish('error'))
  }
}

// ---------------------------------------------------------------------------
// 播放注册表（同一时刻只应有一个 Nolan 在出声）
// ---------------------------------------------------------------------------

interface ActiveWebPlayback {
  handle: WebHandle
  gain: GainNode
  source: AudioBufferSourceNode | null
  stopped: boolean
  c: AudioContext
}

let currentWeb: ActiveWebPlayback | null = null
let currentAudio: HTMLAudioElement | null = null

/** 停掉当前播放（不动队列；playAudio 开播前的内部清理点）。两条路径都走增益包络，防「停的时候又爆一声」 */
function stopCurrentPlayback(): void {
  if (currentWeb) {
    const p = currentWeb
    currentWeb = null
    p.stopped = true
    p.handle.markStopped()
    try {
      const t = p.c.currentTime
      p.gain.gain.cancelScheduledValues(t)
      // 指数快速拉零（~8ms 时间常数），再延迟 stop——硬 stop 本身也是一次电平跳变
      p.gain.gain.setTargetAtTime(0, t, STOP_TAU_S)
      p.source?.stop(t + 0.05)
    } catch {
      /* 已停止/未开播：忽略 */
    }
  }
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

/** 兜底：<audio> 元素路径（老 webview / decodeAudioData 失败）。行为与旧实现完全一致 */
function playViaElement(url: string): HTMLAudioElement {
  const audio = new Audio(url)
  currentAudio = audio
  const release = () => {
    if (currentAudio === audio) currentAudio = null
  }
  audio.addEventListener('ended', release)
  audio.addEventListener('error', release)
  audio.play().catch(() => {
    // 自动播放被拦截：静默失败，字幕已展示文本，不影响主流程
    release()
  })
  return audio
}

/** Web Audio 主路径：解码缓存 → BufferSource → GainNode 包络 → destination */
function tryPlayWeb(url: string): WebHandle | null {
  const c = getCtx()
  if (!c) return null
  const handle = new WebHandle(c)
  const gain = c.createGain()
  gain.connect(c.destination)
  const playback: ActiveWebPlayback = { handle, gain, source: null, stopped: false, c }
  currentWeb = playback

  void loadBuffer(c, url)
    .then((buffer) => {
      // 加载期间被打断/被新句顶掉：静默丢弃（含 stopAllAudio 后不得补出声）
      if (playback.stopped || currentWeb !== playback) return
      const source = c.createBufferSource()
      source.buffer = buffer
      source.connect(gain)
      playback.source = source

      // GainNode 包络：开头 12ms 线性淡入（解硬切入咔哒），结尾 10ms 淡出（解接缝爆音）
      const now = c.currentTime
      const dur = buffer.duration
      const fadeIn = Math.min(FADE_IN_S, dur / 2)
      const fadeOut = Math.min(FADE_OUT_S, Math.max(0, dur - fadeIn))
      gain.gain.setValueAtTime(0, now)
      gain.gain.linearRampToValueAtTime(1, now + fadeIn)
      if (dur > fadeIn + fadeOut + 0.001) {
        gain.gain.setValueAtTime(1, now + dur - fadeOut)
      }
      gain.gain.linearRampToValueAtTime(0, now + dur)

      source.onended = () => {
        // 手动 stop 也触发 onended：stopped 标记区分，不冒假 ended（对齐 <audio> pause 语义）
        if (playback.stopped) return
        playback.stopped = true
        if (currentWeb === playback) currentWeb = null
        try {
          gain.disconnect()
        } catch {
          /* 忽略 */
        }
        handle.finish('ended')
      }
      handle.markStarted()
      source.start()
    })
    .catch(() => {
      // Web Audio 链路失败（拉取/解码/老 webview）：回退 <audio> 元素——声音优先于净化
      if (playback.stopped) return
      if (currentWeb === playback) currentWeb = null
      try {
        gain.disconnect()
      } catch {
        /* 忽略 */
      }
      handle.adoptFallback(playViaElement(url))
    })
  return handle
}

/**
 * 播放一段音频：url 非空时优先走 Web Audio（增益包络消爆音），失败自动回退 <audio> 元素。
 * 开播前自动停掉上一条（注册表语义；不清流式队列——队列泵正是靠它逐条开播）。
 * 返回播放句柄，调用方用 ended/error 事件串联多段音频；自动播放被拦静默吞掉。
 */
export function playAudio(url: string | null): PlaybackHandle | null {
  if (!url) return null
  stopCurrentPlayback()
  return tryPlayWeb(url) ?? playViaElement(url)
}

// ---------------------------------------------------------------------------
// 句级流式播放队列（/api/chat/stream 边收边播；stopAllAudio 一并清空）
// ---------------------------------------------------------------------------

let audioQueue: string[] = []
let audioQueueBusy = false

/** 立即停止浏览器通道的全部播报（手动/语音打断共用）：清空队列 + 包络化停当前 */
export function stopAllAudio(): void {
  audioQueue = []
  audioQueueBusy = false
  stopCurrentPlayback()
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
  // 当前句开播后预取下一句的解码结果：缩小句间空隙，接缝只剩包络软边
  primeAudio(audioQueue[0])
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
  // 1.5 秒后若仍 paused 且未开始过，视为未出声，直接推进队列
  window.setTimeout(() => {
    if (audioQueueBusy && audio.paused && audio.currentTime === 0) advance()
  }, 1500)
}
