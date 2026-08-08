// 字幕条（保留组件，当前未挂载）：
// 新版 Kimi 对话区改为完整消息流（用户气泡 / Nolan 通栏排版），
// 「最近一轮对话」的展示角色已由消息流接管，「点击展开完整记录」由头部历史按钮接管。
// 组件保留并已完成双主题适配（全部颜色吃 CSS 变量），需要字幕形态时可直接挂回。
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
        <p className="truncate text-right text-[12px] leading-[18px] text-[var(--label-tertiary)]">
          你：{lastUser.text}
        </p>
      )}
      {lastNolan && (
        <p
          className={`line-clamp-2 text-[16px] leading-6 ${
            lastNolan.pending
              ? 'text-[var(--label-tertiary)]'
              : 'text-[var(--label-primary)]'
          }`}
        >
          {lastNolan.text}
        </p>
      )}
      {!lastNolan && !lastUser && (
        <p className="text-[12px] leading-[18px] text-[var(--label-quaternary)]">…</p>
      )}
    </button>
  )
}
