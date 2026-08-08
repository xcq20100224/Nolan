// Kimi 风格底部输入区：768px 居中容器，radius 20px，bg primary + separator.s1 边框，
// 聚焦时 effect.shadow.inputDefault 阴影；顶部自动增高 textarea（16/24，上限 200px），
// 底部工具栏：左 附件（回形针）+ 麦克风 + 停止说话，右 32px 圆形发送（kimiDark 底 + ArrowUp）
// 语音方案（服务端录音）：麦克风属于机器不属于浏览器，浏览器只当遥控器
//   点击 🎤 → POST /api/mic/start 服务端开始录音；再点一次 → POST /api/mic/stop 停止并识别
// 不再需要任何浏览器麦克风权限，🎤 永不置灰（除非会话结束禁用）
// 录音状态通过 onRecordingChange 上报给父组件，声波条以模拟律动呈现（无真实波形流）
import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'
import { Mic, Paperclip, ArrowUp, Square, X, FileText, FileSpreadsheet, FileAudio, FileVideo, FileArchive, File, Image as ImageIcon, Presentation } from 'lucide-react'
import { micStart, micStop, micState, clientLog, stopSpeak, stopAllAudio } from '@/lib/api'

/** 「没听清」等占位提示的显示时长（毫秒） */
const HINT_MS = 3000

/** textarea 自动增高上限（px） */
const TEXTAREA_MAX_H = 200

/** 待发送附件芯片的展示结构（正文存在父组件，发送时拼进 payload） */
export interface AttachmentChip {
  id: string
  name: string
  chars: number
  /** 类别：文本/表格/文档/演示文稿/图片/音频/视频/压缩包/二进制 */
  kind?: string
  /** 诚实说明（如「扫描版PDF，无文本层」），空串表示通道正常 */
  note?: string
}

/** 附件类别 → 芯片图标（lucide 细线风格，与输入区其余图标一致） */
function kindIcon(kind: string | undefined) {
  switch (kind) {
    case '文本':
    case '文档':
      return FileText
    case '表格':
      return FileSpreadsheet
    case '演示文稿':
      return Presentation
    case '图片':
      return ImageIcon
    case '音频':
      return FileAudio
    case '视频':
      return FileVideo
    case '压缩包':
      return FileArchive
    case '二进制':
      return File
    default:
      return Paperclip
  }
}

interface NegaInputProps {
  /** 会话是否已结束（结束后禁用全部输入） */
  disabled: boolean
  /** 发送文本消息 */
  onSend: (text: string) => void
  /** 录音状态变化上报，驱动声波条切换为模拟律动 */
  onRecordingChange: (recording: boolean) => void
  /** 语音状态播报（以 Nolan 身份插入对话，让每一步都看得见） */
  onStatus?: (text: string) => void
  /** 待发送的附件芯片（上传成功后由父组件维护，最多 3 个） */
  attachments?: AttachmentChip[]
  /** 移除一个待发送附件 */
  onRemoveAttachment?: (id: string) => void
  /** 回形针按钮：弹出文件选择框，选中后交给父组件上传暂存 */
  onAddFiles?: (files: FileList | File[]) => void
}

export default function NegaInput({ disabled, onSend, onRecordingChange, onStatus, attachments = [], onRemoveAttachment, onAddFiles }: NegaInputProps) {
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
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
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

  /** textarea 随内容自动增高（上限 TEXTAREA_MAX_H，超出内部滚动） */
  const autoGrow = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, TEXTAREA_MAX_H)}px`
  }

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
      // 上报录音状态，声波条切换为模拟律动
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

  // 发送并清空输入框（有待发送附件时允许空文本——附件正文由父组件拼进 payload）
  const canSend = text.trim().length > 0 || attachments.length > 0
  const submit = () => {
    if (!canSend || disabled) return
    onSend(text.trim())
    setText('')
    // 清空后收回 textarea 高度
    requestAnimationFrame(() => autoGrow())
  }

  // 回车发送（Shift+Enter 换行；中文输入法组合中不触发）
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const handleTextChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    autoGrow()
  }

  /** 回形针：弹出文件选择框（多选），选中即走与拖拽相同的上传暂存链路 */
  const handlePickFiles = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) onAddFiles?.(e.target.files)
    e.target.value = '' // 允许连续选择同一个文件
  }

  const sendEnabled = canSend && !disabled

  return (
    <footer className="shrink-0 px-4 pb-4 pt-1">
      {jsError && (
        <div
          className="mx-auto mb-2 max-w-[768px] rounded-[8px] border px-3 py-1 text-[12px] leading-[18px]"
          style={{ borderColor: 'var(--danger)', color: 'var(--danger)' }}
        >
          {jsError}
        </div>
      )}
      {/* 隐藏的文件选择框（回形针触发，多选，与拖拽同一条上传链路） */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={handlePickFiles}
      />
      {/* 输入容器：radius 20px / bg primary / separator.s1 边框 / 聚焦阴影 inputDefault */}
      <div
        className="mx-auto w-full max-w-[768px] rounded-[20px] border border-[var(--separator)] bg-[var(--bg-primary)] transition-shadow duration-200 focus-within:shadow-[var(--input-focus-shadow)]"
      >
        {/* 待发送附件芯片：上传成功后出现，发送时随消息一起交付（最多 3 个） */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-4 pt-3">
            {attachments.map((a) => {
              const Icon = kindIcon(a.kind)
              return (
                <span
                  key={a.id}
                  title={a.note || undefined}
                  className="flex items-center gap-1.5 rounded-[8px] bg-[var(--fill-f1)] px-2 py-1 text-[12px] leading-[18px] text-[var(--label-secondary)]"
                >
                  <Icon className="h-4 w-4" />
                  {a.name} · {a.chars} 字{a.kind ? ` · ${a.kind}` : ''}
                  {/* 通道说明（如「扫描版PDF，无文本层」）内联提示，悬停看全文；status.orange 警示色 */}
                  {a.note && <span style={{ color: '#ff9500' }}>⚠ {a.note}</span>}
                  <button
                    type="button"
                    onClick={() => onRemoveAttachment?.(a.id)}
                    className="text-[var(--label-tertiary)] transition-colors duration-150 hover:text-[var(--danger)]"
                    title="移除附件"
                    aria-label={`移除附件 ${a.name}`}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </span>
              )
            })}
          </div>
        )}

        {/* 输入文本区：16/24 主文本，自动增高上限 200px */}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder={placeholderHint || (disabled ? '会话已结束' : '请讲，先生…（可直接拖文件进来）')}
          className="kimi-scroll max-h-[200px] w-full resize-none bg-transparent px-4 pb-1 pt-3 text-[16px] leading-6 text-[var(--label-primary)] outline-none placeholder:text-[var(--label-quaternary)] disabled:cursor-not-allowed disabled:opacity-50"
        />

        {/* 工具栏：左 附件 / 麦克风 / 停止说话，右 发送 */}
        <div className="flex items-center gap-1 px-3 pb-2.5 pt-1">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            className="icon-btn"
            title="添加附件（上传后随下一条消息一起发送，最多 3 个）"
            aria-label="添加附件"
          >
            <Paperclip className="h-[18px] w-[18px]" />
          </button>
          <button
            type="button"
            onClick={handleMic}
            disabled={disabled}
            title={recording ? '停止录音（或再按一次 Alt）' : '语音输入（点击或按 Alt 键）'}
            aria-label={recording ? '停止录音' : '语音输入'}
            className={`icon-btn ${recording ? 'kimi-recording' : ''}`}
            style={recording ? { color: 'var(--danger)' } : undefined}
          >
            <Mic className="h-[18px] w-[18px]" />
          </button>
          {recording && (
            <span
              className="w-8 shrink-0 text-center text-[12px] leading-[18px] tabular-nums"
              style={{ color: 'var(--danger)' }}
            >
              {seconds}s
            </span>
          )}
          <button
            type="button"
            onClick={() => {
              clientLog('点击-停止说话')
              stopAllAudio() // 浏览器通道静音（此前只停服务端音箱，浏览器照播）
              stopSpeak()
            }}
            disabled={disabled}
            className="icon-btn"
            title="停止说话（打断当前播报）"
            aria-label="停止说话"
          >
            <Square className="h-[18px] w-[18px]" />
          </button>

          {/* 发送：32px 圆形主按钮（kimiDark 底 + ArrowUp），空文本 disabled（labels.quaternary） */}
          <button
            type="button"
            onClick={submit}
            disabled={!sendEnabled}
            title="发送"
            aria-label="发送"
            className="ml-auto flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-[background-color,color,transform] duration-150 active:scale-[0.96] disabled:cursor-not-allowed disabled:active:scale-100"
            style={{
              background: sendEnabled ? 'var(--btn-primary-bg)' : 'var(--fill-f1)',
              color: sendEnabled ? 'var(--btn-primary-text)' : 'var(--label-quaternary)',
            }}
          >
            <ArrowUp className="h-[18px] w-[18px]" />
          </button>
        </div>
      </div>
    </footer>
  )
}
