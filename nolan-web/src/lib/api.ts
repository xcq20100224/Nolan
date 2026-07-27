// Nolan 后端 API 封装
// 一律使用相对路径 '/api/...'，由 vite dev server 代理到 7101 端口的 Python 后端

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
 * 播放一段音频：url 非空时创建 Audio 并尝试播放。
 * play() 返回的 promise 被浏览器自动播放策略拒绝时静默吞掉（用户交互过页面后通常允许），
 * 返回 Audio 实例，方便调用方用 ended 事件串联多段音频。
 */
export function playAudio(url: string | null): HTMLAudioElement | null {
  if (!url) return null
  const audio = new Audio(url)
  audio.play().catch(() => {
    // 自动播放被拦截：静默失败，字幕已展示文本，不影响主流程
  })
  return audio
}

/** 提醒列表：GET /api/reminders → {"text": "口语化提醒列表"} */
export async function getRemindersText(): Promise<string> {
  const data = await fetchJson<{ text: string }>('/api/reminders')
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
 * 服务端开始录音：POST /api/mic/start → {"ok": true}
 * 麦克风由服务端（sounddevice）直采，浏览器只当遥控器，无需任何浏览器权限
 * 返回是否成功开始（请求失败 / ok 非 true 均视为不可用）
 */
export async function micStart(): Promise<boolean> {
  try {
    const data = await fetchJson<{ ok: boolean }>('/api/mic/start', { method: 'POST' })
    return data.ok === true
  } catch {
    return false
  }
}

/**
 * 服务端停止录音并识别：POST /api/mic/stop → {"text": "..."}
 * 无语音 / 未在录音时返回空串；请求失败抛错，由调用方兜底提示
 */
export async function micStop(): Promise<string> {
  const data = await fetchJson<{ text?: string }>('/api/mic/stop', { method: 'POST' })
  return typeof data.text === 'string' ? data.text : ''
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
