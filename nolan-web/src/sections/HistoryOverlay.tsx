// NEGA 历史浮层：深色半透明遮罩 + 可滚动完整聊天记录，点击遮罩或关闭按钮收起
import { useEffect, useRef } from 'react'
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

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-label="完整对话记录"
    >
      <div
        className="mx-4 flex max-h-[75vh] w-full max-w-2xl flex-col rounded-md border border-[#2a2a2e] bg-[#101012]/95 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 浮层标题栏 */}
        <div className="flex shrink-0 items-center justify-between border-b border-[#232327] px-5 py-3">
          <span className="text-xs font-light tracking-[0.3em] text-[#8a8578]">对话记录</span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-sm border border-[#2e2e33] p-1 text-[#8a8578] transition-colors hover:border-[#5a5a60] hover:text-[#e8e0d0]"
            title="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 可滚动消息区 */}
        <div className="nega-scroll flex-1 overflow-y-auto px-5 py-4">
          <div className="flex flex-col gap-4">
            {messages.map((msg) => {
              const isUser = msg.role === 'user'
              return (
                <div key={msg.id} className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
                  <div className={`flex max-w-[85%] flex-col ${isUser ? 'items-end' : 'items-start'}`}>
                    <span className="mb-1 text-[10px] tracking-[0.2em] text-[#4a4a50]">
                      {isUser ? '你' : 'Nolan'} · {msg.time}
                    </span>
                    <p
                      className={`text-sm leading-relaxed ${
                        msg.pending
                          ? 'italic text-[#6b6b70]'
                          : isUser
                            ? 'text-[#8a8578]'
                            : 'text-[#e8e0d0]'
                      }`}
                    >
                      {msg.text}
                    </p>
                    {/* 附件芯片：随消息发送的文件（文件名 + 类别 + 抽取字数，正文已拼入 payload） */}
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className={`mt-1 flex flex-wrap gap-1 ${isUser ? 'justify-end' : ''}`}>
                        {msg.attachments.map((a, i) => (
                          <span
                            key={`${msg.id}-att-${i}`}
                            title={a.note || undefined}
                            className="rounded-full border border-[#2e2e33] px-2 py-0.5 text-[10px] font-light text-[#6b6b70]"
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
              <p className="py-8 text-center text-xs text-[#4a4a50]">暂无对话记录</p>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
      </div>
    </div>
  )
}
