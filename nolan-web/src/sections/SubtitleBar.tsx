// NEGA 字幕区：声波下方显示最近一轮对话
//   你   —— 淡灰小字右对齐，单行截断
//   Nolan —— 暖白正常字居中，最多两行，超出省略
// 点击字幕区展开完整聊天记录浮层
import type { Message } from '@/types/message'

interface SubtitleBarProps {
  messages: Message[]
  /** 点击字幕区，展开完整历史浮层 */
  onOpen: () => void
}

export default function SubtitleBar({ messages, onOpen }: SubtitleBarProps) {
  // 取最近一轮：最后一条 Nolan 消息与其之前的最后一条用户消息
  const lastNolan = [...messages].reverse().find((m) => m.role === 'nolan')
  const lastUser = [...messages].reverse().find((m) => m.role === 'user')

  return (
    <button
      type="button"
      onClick={onOpen}
      className="mx-auto mt-6 flex w-full max-w-2xl cursor-pointer flex-col gap-2 px-6 text-center"
      title="点击展开完整对话记录"
    >
      {lastUser && (
        <p className="truncate text-right text-xs font-light text-[#6b6b70]">
          你：{lastUser.text}
        </p>
      )}
      {lastNolan && (
        <p
          className={`text-base leading-relaxed sm:text-lg ${
            lastNolan.pending
              ? 'italic text-[#6b6b70]'
              : 'text-[#e8e0d0]'
          } line-clamp-2`}
        >
          {lastNolan.text}
        </p>
      )}
      {!lastNolan && !lastUser && <p className="text-xs text-[#3f3f45]">…</p>}
    </button>
  )
}
