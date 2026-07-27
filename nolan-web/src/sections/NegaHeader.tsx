// NEGA 顶部区：顶部居中细体大名「NOLAN」+ 副标题，右上角细线按钮与在线状态点
import { Brain, Bell, History, Volume2 } from 'lucide-react'

interface NegaHeaderProps {
  /** 后端是否在线（/api/health 成功） */
  online: boolean
  /** 会话是否已结束（结束后禁用按钮） */
  exited: boolean
  /** 点击「记忆」按钮 */
  onMemory: () => void
  /** 点击「提醒」按钮 */
  onReminders: () => void
  /** 点击「历史」按钮，展开完整聊天记录浮层 */
  onHistory: () => void
  /** 点击「声音测试」按钮（🔊，浏览器 + 音箱双通道同时发声） */
  onSoundTest: () => void
}

/** 细线边框小按钮的统一样式 */
const btnClass =
  'flex items-center gap-1.5 rounded-sm border border-[#2e2e33] bg-transparent px-3 py-1.5 ' +
  'text-xs tracking-[0.2em] text-[#8a8578] transition-colors ' +
  'hover:border-[#5a5a60] hover:text-[#e8e0d0] disabled:cursor-not-allowed disabled:opacity-40'

export default function NegaHeader({ online, exited, onMemory, onReminders, onHistory, onSoundTest }: NegaHeaderProps) {
  return (
    <header className="relative shrink-0 px-6 pb-4 pt-10">
      {/* 中央：细体大名 + 副标题（letter-spacing 会在末字后留白，用等量左 padding 补偿视觉居中） */}
      <div className="pointer-events-none select-none text-center">
        <h1 className="pl-[0.5em] text-4xl font-extralight tracking-[0.5em] text-[#e8e0d0] sm:text-5xl">
          NOLAN
        </h1>
        <p className="mt-3 pl-[0.35em] text-[11px] font-light tracking-[0.35em] text-[#6b6b70]">
          私人 AI 管家
        </p>
      </div>

      {/* 右上角：在线状态点 + 记忆 / 提醒 / 历史 */}
      <div className="absolute right-4 top-10 flex items-center gap-2 sm:right-6">
        <span
          className={`mr-1 h-1.5 w-1.5 rounded-full transition-colors ${
            online ? 'bg-[#7d9b76]' : 'bg-[#3f3f45]'
          }`}
          title={online ? '后端在线' : '后端离线'}
        />
        <button type="button" onClick={onMemory} disabled={exited} className={btnClass} title="查看长期记忆">
          <Brain className="h-3.5 w-3.5" />
          记忆
        </button>
        <button type="button" onClick={onReminders} disabled={exited} className={btnClass} title="查看提醒列表">
          <Bell className="h-3.5 w-3.5" />
          提醒
        </button>
        <button type="button" onClick={onSoundTest} disabled={exited} className={btnClass} title="声音测试（浏览器 + 音箱同时发声）">
          <Volume2 className="h-3.5 w-3.5" />
          声音
        </button>
        <button type="button" onClick={onHistory} className={btnClass} title="展开完整对话记录">
          <History className="h-3.5 w-3.5" />
          历史
        </button>
      </div>
    </header>
  )
}
