// Kimi 风格头部：56px 高通栏，左「Nolan」字标（16/24 字重 500），右侧图标按钮组
// 右组：在线状态点 + 唤醒 / 记忆 / 提醒 / 声音测试 + 文件柜（红点角标）/ 历史 / 主题切换
// 底部分隔用 separator.s1（surface over stroke 原则下的极细分隔线）
import { Brain, Bell, History, Volume2, Ear, FolderOpen, Sun, Moon } from 'lucide-react'

/** 主题：亮色为默认 */
export type Theme = 'light' | 'dark'

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
  /** 点击「声音测试」按钮（浏览器 + 音箱双通道同时发声） */
  onSoundTest: () => void
  /** 唤醒词耳蜗是否开启 */
  wakeOn: boolean
  /** 点击「唤醒词」开关 */
  onWakeToggle: () => void
  /** 点击「文件柜」按钮（打开面板即清除红点） */
  onOpenCabinet: () => void
  /** 文件柜有未读新文件（按钮右上角红点角标） */
  cabinetHasNew: boolean
  /** 当前主题 */
  theme: Theme
  /** 点击主题切换 */
  onToggleTheme: () => void
}

export default function NegaHeader({
  online,
  exited,
  onMemory,
  onReminders,
  onHistory,
  onSoundTest,
  wakeOn,
  onWakeToggle,
  onOpenCabinet,
  cabinetHasNew,
  theme,
  onToggleTheme,
}: NegaHeaderProps) {
  return (
    <header className="relative z-[500] flex h-14 shrink-0 items-center justify-between border-b border-[var(--separator)] bg-[var(--bg-primary)] px-4 transition-colors duration-200">
      {/* 左：克制的字标（16/24，字重 500） */}
      <span className="select-none text-[16px] font-medium leading-6 text-[var(--label-primary)]">
        Nolan
      </span>

      {/* 右：在线状态点 + 功能图标按钮组（32px 容器 / 18px 图标） */}
      <div className="flex items-center gap-1">
        <span
          className="mr-1 h-1.5 w-1.5 rounded-full transition-colors duration-200"
          style={{ background: online ? 'var(--positive-green)' : 'var(--fill-f2)' }}
          title={online ? '后端在线' : '后端离线'}
        />
        <button
          type="button"
          onClick={onWakeToggle}
          disabled={exited}
          className="icon-btn"
          style={wakeOn ? { color: 'var(--kimi-blue)' } : undefined}
          title={wakeOn ? '唤醒词耳蜗已开启（说「诺兰」即可唤醒），点击关闭' : '开启唤醒词：对麦克风说「诺兰」即可唤醒 Nolan'}
          aria-label={wakeOn ? '关闭唤醒词' : '开启唤醒词'}
        >
          <Ear className="h-[18px] w-[18px]" />
        </button>
        <button
          type="button"
          onClick={onMemory}
          disabled={exited}
          className="icon-btn"
          title="查看长期记忆"
          aria-label="查看长期记忆"
        >
          <Brain className="h-[18px] w-[18px]" />
        </button>
        <button
          type="button"
          onClick={onReminders}
          disabled={exited}
          className="icon-btn"
          title="查看提醒列表"
          aria-label="查看提醒列表"
        >
          <Bell className="h-[18px] w-[18px]" />
        </button>
        <button
          type="button"
          onClick={onSoundTest}
          disabled={exited}
          className="icon-btn"
          title="声音测试（浏览器 + 音箱同时发声）"
          aria-label="声音测试"
        >
          <Volume2 className="h-[18px] w-[18px]" />
        </button>
        <button
          type="button"
          onClick={onOpenCabinet}
          className="icon-btn"
          title={cabinetHasNew ? '文件柜有新文件，点击查看' : '文件柜（Nolan 生成的文件在这里查看/下载）'}
          aria-label="文件柜"
        >
          <FolderOpen className="h-[18px] w-[18px]" />
          {/* 未读红点角标：status.danger，打开面板后由父组件清除 */}
          {cabinetHasNew && (
            <span
              className="absolute right-1 top-1 h-2 w-2 rounded-full"
              style={{ background: 'var(--danger)' }}
            />
          )}
        </button>
        <button
          type="button"
          onClick={onHistory}
          className="icon-btn"
          title="展开完整对话记录"
          aria-label="展开完整对话记录"
        >
          <History className="h-[18px] w-[18px]" />
        </button>
        <button
          type="button"
          onClick={onToggleTheme}
          className="icon-btn"
          title={theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
          aria-label={theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
        >
          {theme === 'dark' ? (
            <Sun className="h-[18px] w-[18px]" />
          ) : (
            <Moon className="h-[18px] w-[18px]" />
          )}
        </button>
      </div>
    </header>
  )
}
