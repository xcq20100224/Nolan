// NEGA 底部输入区：极简细线输入框（透明底、底部 1px 边线、聚焦变亮）+ 细线 🎤 按钮
// 语音方案（服务端录音）：麦克风属于机器不属于浏览器，浏览器只当遥控器
//   点击 🎤 → POST /api/mic/start 服务端开始录音；再点一次 → POST /api/mic/stop 停止并识别
// 不再需要任何浏览器麦克风权限，🎤 永不置灰（除非会话结束禁用）
// 录音状态通过 onRecordingChange 上报给父组件，中央声波以模拟律动呈现（无真实波形流）
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Mic, SendHorizontal } from 'lucide-react'
import { micStart, micStop } from '@/lib/api'

/** 「没听清」等占位提示的显示时长（毫秒） */
const HINT_MS = 3000

interface NegaInputProps {
  /** 会话是否已结束（结束后禁用全部输入） */
  disabled: boolean
  /** 发送文本消息 */
  onSend: (text: string) => void
  /** 录音状态变化上报，驱动中央声波切换为模拟律动 */
  onRecordingChange: (recording: boolean) => void
}

export default function NegaInput({ disabled, onSend, onRecordingChange }: NegaInputProps) {
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

  // 回调入 ref，保证异步回调里取到的永远是最新闭包
  const onSendRef = useRef(onSend)
  const onRecordingChangeRef = useRef(onRecordingChange)
  onSendRef.current = onSend
  onRecordingChangeRef.current = onRecordingChange
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
    if (disabled || stoppingRef.current) return

    if (recording) {
      // 停止：服务端停止录音并返回识别文本
      stoppingRef.current = true
      try {
        const result = (await micStop()).trim()
        if (unmountedRef.current) return
        leaveRecording()
        if (result) {
          // 识别成功：填入输入框并自动发送
          setText(result)
          onSendRef.current(result)
        } else {
          // 空结果：仅提示，不发送
          flashHint('没听清，请再说一次')
        }
      } catch {
        // stop 请求失败：恢复空闲态并提示改用键盘
        if (!unmountedRef.current) {
          leaveRecording()
          flashHint('先生，麦克风暂时不可用，请直接打字')
        }
      } finally {
        stoppingRef.current = false
      }
      return
    }

    // 开始：通知服务端录音，失败则保持空闲态并提示（不置灰，可重试）
    const ok = await micStart()
    if (unmountedRef.current) return
    if (!ok) {
      flashHint('先生，麦克风暂时不可用，请直接打字')
      return
    }
    setSeconds(0)
    setRecording(true)
    // 上报录音状态，中央声波切换为模拟律动
    onRecordingChangeRef.current(true)
    // 秒数计时
    timerRef.current = window.setInterval(() => setSeconds((s) => s + 1), 1000)
  }

  // 组件卸载：清理计时器；若仍在录音，尽力通知服务端停止（服务端未录音时返回空串，调用安全）
  useEffect(() => {
    return () => {
      unmountedRef.current = true
      stopTimer()
      if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current)
      if (recordingRef.current) void micStop().catch(() => undefined)
      onRecordingChangeRef.current(false)
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
          title={recording ? '停止录音' : '语音输入'}
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
      </div>
    </footer>
  )
}
