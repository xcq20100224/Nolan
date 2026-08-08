// 历史浮层（Kimi 规范 Modal）：完整聊天记录，点击遮罩 / 关闭按钮 / Esc 收起
// 视觉与文件柜一致：mask.base 遮罩 + background.primary 面板（radius 12px，无描边）
import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import type { Message } from '@/types/message'

interface HistoryOverlayProps {
  messages: Message[]
  /** 关闭浮层 */
  onClose: () => void
}

export default function HistoryOverlay({ messages, onClose }: HistoryOverlayProps) {
  // 打开时滚动到底部，定位最新一轮
  const bottomRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView()
  }, [])

  /** 出场动画进行中（140ms 后真正卸载） */
  const [closing, setClosing] = useState(false)
  const closeTimerRef = useRef<number | null>(null)

  /** 请求关闭：先播出场动画，再通知父组件卸载 */
  const requestClose = () => {
    if (closing) return
    setClosing(true)
    closeTimerRef.current = window.setTimeout(onClose, 140)
  }
  useEffect(
    () => () => {
      if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current)
    },
    [],
  )

  // Esc 关闭
  const requestCloseRef = useRef(requestClose)
  requestCloseRef.current = requestClose
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') requestCloseRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div
      className={`fixed inset-0 z-[1000] flex items-center justify-center ${closing ? 'kimi-closing' : ''}`}
      role="dialog"
      aria-modal="true"
      aria-label="完整对话记录"
    >
      {/* 遮罩：mask.base，点击关闭 */}
      <div
        className="kimi-mask absolute inset-0"
        style={{ background: 'var(--mask)' }}
        onClick={requestClose}
      />
      {/* 面板：bg primary / radius 12px / 无描边 / 居中 max-w 640px / max-h 75vh */}
      <div
        className="kimi-modal relative mx-4 flex max-h-[75vh] w-full max-w-[640px] flex-col rounded-[12px] bg-[var(--bg-primary)] shadow-[var(--shadow-small)]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题行：16/24 字重 500 + 24px 关闭按钮 */}
        <div className="flex h-14 shrink-0 items-center justify-between px-5">
          <span className="text-[16px] font-medium leading-6 text-[var(--label-primary)]">
            对话记录
          </span>
          <button
            type="button"
            onClick={requestClose}
            className="icon-btn h-6 w-6"
            title="关闭"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 可滚动消息区（与主对话区同款排版：用户右气泡 / Nolan 左通栏） */}
        <div className="kimi-scroll flex-1 overflow-y-auto px-5 pb-4">
          <div className="flex flex-col gap-4">
            {messages.map((msg) => {
              const isUser = msg.role === 'user'
              return (
                <div key={msg.id} className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
                  <div className={`flex max-w-[85%] flex-col ${isUser ? 'items-end' : 'items-start'}`}>
                    <span className="mb-1 text-[12px] leading-[18px] text-[var(--label-tertiary)]">
                      {isUser ? '你' : 'Nolan'} · {msg.time}
                    </span>
                    {isUser ? (
                      <p className="whitespace-pre-wrap rounded-[12px] bg-[var(--bubble-user-bg)] px-3.5 py-2.5 text-[14px] leading-5 text-[var(--bubble-user-text)]">
                        {msg.text}
                      </p>
                    ) : (
                      <p
                        className={`whitespace-pre-wrap text-[14px] leading-5 ${
                          msg.pending ? 'text-[var(--label-tertiary)]' : 'text-[var(--label-primary)]'
                        }`}
                      >
                        {msg.text}
                      </p>
                    )}
                    {/* 附件芯片：随消息发送的文件（文件名 + 类别 + 抽取字数，正文已拼入 payload） */}
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className={`mt-1 flex flex-wrap gap-1 ${isUser ? 'justify-end' : ''}`}>
                        {msg.attachments.map((a, i) => (
                          <span
                            key={`${msg.id}-att-${i}`}
                            title={a.note || undefined}
                            className="rounded-[8px] bg-[var(--fill-f1)] px-2 py-0.5 text-[12px] leading-[18px] text-[var(--label-tertiary)]"
                          >
                            📎 {a.name} · {a.chars} 字{a.kind ? ` · ${a.kind}` : ''}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
            {messages.length === 0 && (
              <p className="py-8 text-center text-[14px] leading-5 text-[var(--label-tertiary)]">
                暂无对话记录
              </p>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
      </div>
    </div>
  )
}
