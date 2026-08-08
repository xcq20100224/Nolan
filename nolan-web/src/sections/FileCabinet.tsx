// 文件柜面板（Kimi 规范 Modal）：Nolan 生成 / 先生上传的文件都在这里查看与下载
// 数据来自 GET /api/files_list（每次打开面板刷新），点击条目新标签页打开/下载
// 视觉：mask.base 遮罩 + background.primary 面板（radius 12px，无描边，遮罩提供隔离），
// 入场 opacity+scale(0.96)→1 180ms，出场 140ms（animation.md §4.5 / §4.2）
import { useEffect, useRef, useState } from 'react'
import {
  File,
  FileAudio,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  RefreshCw,
  X,
} from 'lucide-react'
import type { CabinetFile } from '@/lib/api'
import { cabinetFileUrl, getFilesList } from '@/lib/api'

interface FileCabinetProps {
  /** 关闭面板 */
  onClose: () => void
}

/** kind → 图标（细线风格，与 lucide 其余按钮一致） */
const KIND_ICON: Record<string, typeof File> = {
  文档: FileText,
  图片: ImageIcon,
  表格: FileSpreadsheet,
  音频: FileAudio,
}

/** 字节数 → 人类可读大小 */
function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** epoch 秒 → YYYY-MM-DD HH:MM */
function fmtTime(mtime: number): string {
  const d = new Date(mtime * 1000)
  const p = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export default function FileCabinet({ onClose }: FileCabinetProps) {
  const [files, setFiles] = useState<CabinetFile[] | null>(null)
  const [failed, setFailed] = useState(false)
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

  // 每次打开面板刷新列表（手动刷新按钮沿用此函数）
  const refresh = () => {
    getFilesList()
      .then((list) => {
        setFiles(list)
        setFailed(false)
      })
      .catch(() => {
        setFiles([])
        setFailed(true)
      })
  }

  // 打开即刷新 + 面板存续期间每 3 秒自动轮询：
  // Nolan 后台新生成的文件最长 3 秒后自动出现在列表里（原来必须关了重开）。
  // 关闭面板（组件卸载）时 clearInterval，杜绝计时器泄漏；
  // cancelled 标记挡住卸载后迟到的响应，避免对尸体组件 setState。
  useEffect(() => {
    let cancelled = false
    refresh()
    const timer = window.setInterval(() => {
      getFilesList()
        .then((list) => {
          if (cancelled) return
          setFiles(list)
          setFailed(false)
        })
        .catch(() => {
          // 轮询失败静默保留当前列表——不把面板闪成空，下一轮自愈
        })
    }, 3_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  // Esc 关闭（与历史浮层一致）
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
      aria-label="文件柜"
    >
      {/* 遮罩：mask.base，点击关闭 */}
      <div
        className="kimi-mask absolute inset-0"
        style={{ background: 'var(--mask)' }}
        onClick={requestClose}
      />
      {/* 面板：bg primary / radius 12px / 无描边（遮罩提供隔离）/ 居中 max-w 640px / max-h 75vh */}
      <div
        className="kimi-modal relative mx-4 flex max-h-[75vh] w-full max-w-[640px] flex-col rounded-[12px] bg-[var(--bg-primary)] shadow-[var(--shadow-small)]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题行：16/24 字重 500 + 24px 关闭按钮 */}
        <div className="flex h-14 shrink-0 items-center justify-between px-5">
          <span className="text-[16px] font-medium leading-6 text-[var(--label-primary)]">
            文件柜
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={refresh}
              className="icon-btn h-6 w-6"
              title="刷新列表"
              aria-label="刷新列表"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
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
        </div>

        {/* 可滚动文件列表：列表项 hover fills.f1 */}
        <div className="kimi-scroll flex-1 overflow-y-auto px-3 pb-4">
          {files === null && (
            <p className="py-8 text-center text-[14px] leading-5 text-[var(--label-tertiary)]">
              读取中…
            </p>
          )}
          {files !== null && files.length === 0 && (
            <p className="py-8 text-center text-[14px] leading-5 text-[var(--label-tertiary)]">
              {failed ? '先生，文件柜读取失败了，请检查后端是否在线。' : 'Nolan 生成的文件会出现在这里'}
            </p>
          )}
          {files !== null && files.length > 0 && (
            <div className="flex flex-col gap-0.5">
              {files.map((f) => {
                const Icon = KIND_ICON[f.kind] ?? File
                return (
                  <button
                    key={f.name}
                    type="button"
                    onClick={() => window.open(cabinetFileUrl(f.name), '_blank')}
                    className="flex items-center gap-3 rounded-[8px] px-3 py-2 text-left transition-colors duration-150 hover:bg-[var(--fill-f1)]"
                    title="在新标签页打开 / 下载"
                  >
                    <Icon className="h-4 w-4 shrink-0 text-[var(--label-secondary)]" />
                    <span className="min-w-0 flex-1 truncate text-[14px] leading-5 text-[var(--label-primary)]">
                      {f.name}
                    </span>
                    <span className="shrink-0 text-[12px] leading-[18px] text-[var(--label-tertiary)]">
                      {f.kind} · {fmtSize(f.size)} · {fmtTime(f.mtime)}
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
