// NEGA 底部输入区：极简细线输入框（透明底、底部 1px 边线、聚焦变亮）+ 细线 🎤 按钮
// 语音方案（服务端录音）：麦克风属于机器不属于浏览器，浏览器只当遥控器
//   点击 🎤 → POST /api/mic/start 服务端开始录音；再点一次 → POST /api/mic/stop 停止并识别
// 不再需要任何浏览器麦克风权限，🎤 永不置灰（除非会话结束禁用）
// 录音状态通过 onRecordingChange 上报给父组件，中央声波以模拟律动呈现（无真实波形流）
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Mic, SendHorizontal, Square } from 'lucide-react'
import { micStart, micStop, micState, clientLog, stopSpeak, stopAllAudio } from '@/lib/api'

/** 「没听清」等占位提示的显示时长（毫秒） */
const HINT_MS = 3000

interface NegaInputProps {
  /** 会话是否已结束（结束后禁用全部输入） */
  disabled: boolean
  /** 发送文本消息 */
  onSend: (text: string) => void
  /** 录音状态变化上报，驱动中央声波切换为模拟律动 */
  onRecordingChange: (recording: boolean) => void
  /** 语音状态播报（以 Nolan 身份插入对话，让每一步都看得见） */
  onStatus?: (text: string) => void
}

export default function NegaInput({ disabled, onSend, onRecordingChange, onStatus }: NegaInputProps) {
  // 全局错误浮层：任何未捕获的 JS 错误都显示在屏幕上（调试期 instrumentation）
  const [jsError, setJsError] = useState('')
  useEffect(() => {
    const onErr = (e: ErrorEvent) => setJsError(`JS错误: ${e.message}`)
    const onRej = (e: PromiseRejectionEvent) =>
      setJsError(`Promise错误: ${e.reason instanceof Error ? e.reason.message : String(e.reason)}`)
    window.addEventListener('error', onErr)
    window.addEventListener('unhandledrejection', onRej)
    return () => {
      window.removeEventListener('error', onErr)
      window.removeEventListener('unhandledrejection', onRej)
    }
  }, [])
  const [text, setText] = useState('')
  const [recording, setRecording] = useState(false)
  /** 录音秒数计时 */
  const [seconds, setSeconds] = useState(0)
  /** 占位提示覆盖（如『没听清，请再说一次』），空串则用默认文案 */
  const [placeholderHint, setPlaceholderHint] = useState('')

  const timerRef = useRef<number | null>(null)
  const hintTimerRef = useRef<number | null>(null)
  // 组件卸载后忽略迟到的识别回调
  const unmountedRef = useRef(false)
  // stop 请求进行中（识别可能耗时），防止连点重复触发
  const stoppingRef = useRef(false)
  // start 流程进行中（发起请求 + 轮询确认），防止连点重复触发
  const startingRef = useRef(false)

  // 回调入 ref，保证异步回调里取到的永远是最新闭包
  const onSendRef = useRef(onSend)
  const onRecordingChangeRef = useRef(onRecordingChange)
  const onStatusRef = useRef(onStatus)
  onSendRef.current = onSend
  onRecordingChangeRef.current = onRecordingChange
  onStatusRef.current = onStatus
  // 状态播报：未提供回调时静默
  const reportStatus = (text: string) => onStatusRef.current?.(text)
  // 卸载清理时需读取最新录音状态
  const recordingRef = useRef(recording)
  recordingRef.current = recording

  /** 停止秒数计时 */
  const stopTimer = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  /** 退出录音态：停秒表、复位状态并上报父组件（声波退回闲置 / 忙碌） */
  const leaveRecording = () => {
    stopTimer()
    setRecording(false)
    onRecordingChangeRef.current(false)
  }

  /** 短暂展示占位提示后还原默认文案 */
  const flashHint = (hint: string) => {
    if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current)
    setPlaceholderHint(hint)
    hintTimerRef.current = window.setTimeout(() => setPlaceholderHint(''), HINT_MS)
  }

  /** 点击麦克风：录音中则停止并识别，否则通知服务端开始录音 */
  const handleMic = async () => {
    if (disabled || stoppingRef.current || startingRef.current) return

    if (recording) {
      // 停止：服务端停止录音并返回识别文本（识别需要几秒，先播报状态）
      clientLog('点击-停止录音')
      stoppingRef.current = true
      reportStatus('正在识别，请稍候……')
      try {
        const result = (await micStop()).trim()
        clientLog(`识别返回 ${result.length} 字: ${result.slice(0, 30)}`)
        if (unmountedRef.current) return
        leaveRecording()
        if (result) {
          // 识别成功：填入输入框并自动发送
          setText(result)
          onSendRef.current(result)
        } else {
          // 空结果：仅提示，不发送
          flashHint('没听清，请再说一次')
          reportStatus('没听清，请再说一次。')
        }
      } catch {
        // stop 请求失败：恢复空闲态并提示改用键盘
        if (!unmountedRef.current) {
          leaveRecording()
          flashHint('先生，麦克风暂时不可用，请直接打字')
          reportStatus('先生，识别服务出错了，请直接打字告诉我。')
        }
      } finally {
        stoppingRef.current = false
      }
      return
    }

    // 开始：主人要说话，Nolan 必须先闭嘴——
    // 手动打断（物理直觉：拿起麦克风 = 别说了听我说）：
    // 双通道静音（浏览器 audio 注册表 + 服务端音箱），再发起录音。
    stopAllAudio()
    stopSpeak()
    // 双保险机制——
    // ① 发起 start 请求（内部最多重试 3 次，服务端录音幂等可重开）；
    // ② 不依赖单次请求的响应，随后轮询 /api/mic/state 确认服务端真实录音状态，
    //    响应丢失也能自愈。6 秒内未确认才判定失败（保持空闲态，可重试，不置灰）。
    startingRef.current = true
    clientLog('点击-开始录音')
    reportStatus('已收到指令，正在启动麦克风……')
    try {
      void micStart() // 点火即可，确认靠下方的状态轮询
      const deadline = Date.now() + 6000
      let started = false
      while (Date.now() < deadline && !unmountedRef.current) {
        const state = await micState()
        clientLog(`轮询录音状态: ${state === null ? '查询失败' : state}`)
        if (state === true) {
          started = true
          break
        }
        await new Promise((r) => setTimeout(r, 400))
      }
      if (unmountedRef.current) return
      if (!started) {
        clientLog('启动失败：6秒内未确认录音开始')
        flashHint('先生，麦克风暂时不可用，请直接打字')
        reportStatus('先生，麦克风启动失败——服务端没有确认录音开始，请重试，或直接打字告诉我。')
        return
      }
      clientLog('已确认服务端录音中，进入聆听态')
      setSeconds(0)
      setRecording(true)
      // 上报录音状态，中央声波切换为模拟律动
      onRecordingChangeRef.current(true)
      reportStatus('🎤 正在聆听，先生。说完请再点一次麦克风。')
      // 秒数计时
      timerRef.current = window.setInterval(() => setSeconds((s) => s + 1), 1000)
    } finally {
      startingRef.current = false
    }
  }

  // 组件卸载：清理计时器；若仍在录音，尽力通知服务端停止（服务端未录音时返回空串，调用安全）
  useEffect(() => {
    // 关键：setup 里必须把卸载标记复位——StrictMode 开发模式会执行
    // setup → cleanup → setup，cleanup 会把标记置 true，不复位的话
    // 组件实例（ref 随实例存活）将永远认为『已卸载』，
    // 导致录音流程每次都在 micStart 成功后被静默丢弃（本次卡死的根因）
    unmountedRef.current = false
    return () => {
      unmountedRef.current = true
      stopTimer()
      if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current)
      if (recordingRef.current) void micStop().catch(() => undefined)
      onRecordingChangeRef.current(false)
    }
  }, [])

  // Alt 键切换录音（按一下开始，再按一下结束）：
  // 用「按下→松开期间没碰其他键」判定单独使用 Alt，
  // 避免 Alt+Tab 切窗口时被误当成录音快捷键
  const handleMicRef = useRef(handleMic)
  handleMicRef.current = handleMic
  useEffect(() => {
    let altHeld = false
    let altCombo = false
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Alt') {
        if (!e.repeat) {
          e.preventDefault()  // 阻止浏览器菜单栏抢焦点
          altHeld = true
          altCombo = false
        }
        return
      }
      if (altHeld) altCombo = true
    }
    const onKeyUp = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Alt') return
      if (altHeld && !altCombo) void handleMicRef.current()
      altHeld = false
      altCombo = false
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [])

  // 发送并清空输入框
  const submit = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  // 回车发送（中文输入法组合中不触发）
  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
      submit()
    }
  }

  return (
    <footer className="shrink-0 px-6 pb-8 pt-4">
      {jsError && (
        <div className="mx-auto mb-2 max-w-2xl rounded border border-[#c05b4d] px-3 py-1 text-xs text-[#c05b4d]">
          {jsError}
        </div>
      )}
      <div className="mx-auto flex max-w-2xl items-center gap-4">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={placeholderHint || (disabled ? '会话已结束' : '请讲，先生…')}
          className="flex-1 border-b border-[#2e2e33] bg-transparent py-2 text-sm font-light text-[#e8e0d0] outline-none transition-colors placeholder:text-[#4a4a50] focus:border-[#e8e0d0] disabled:cursor-not-allowed disabled:opacity-50"
        />
        <button
          type="button"
          onClick={handleMic}
          disabled={disabled}
          title={recording ? '停止录音（或再按一次 Alt）' : '语音输入（点击或按 Alt 键）'}
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-30 ${
            recording
              ? 'nega-recording border-[#c05b4d] text-[#c05b4d]'
              : 'border-[#2e2e33] text-[#8a8578] hover:border-[#5a5a60] hover:text-[#e8e0d0]'
          }`}
        >
          <Mic className="h-4 w-4" />
        </button>
        {recording && (
          <span className="w-8 shrink-0 text-center text-sm tabular-nums text-[#c05b4d]">
            {seconds}s
          </span>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !text.trim()}
          title="发送"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-[#2e2e33] text-[#8a8578] transition-colors hover:border-[#5a5a60] hover:text-[#e8e0d0] disabled:cursor-not-allowed disabled:opacity-30"
        >
          <SendHorizontal className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => {
            clientLog('点击-停止说话')
            stopAllAudio() // 浏览器通道静音（此前只停服务端音箱，浏览器照播）
            stopSpeak()
          }}
          disabled={disabled}
          title="停止说话（打断当前播报）"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-[#2e2e33] text-[#8a8578] transition-colors hover:border-[#5a5a60] hover:text-[#e8e0d0] disabled:cursor-not-allowed disabled:opacity-30"
        >
          <Square className="h-4 w-4" />
        </button>
      </div>
    </footer>
  )
}
