// NEGA 文件柜面板：Nolan 生成 / 先生上传的文件都在这里查看与下载
// 数据来自 GET /api/files_list（每次打开面板刷新），点击条目新标签页打开/下载
// 视觉对齐历史浮层：深色半透明遮罩 + 细线边框 + 暖白文字，不引入新 UI 库
import { useEffect, useState } from 'react'
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
      aria-label="文件柜"
    >
      <div
        className="mx-4 flex max-h-[75vh] w-full max-w-2xl flex-col rounded-md border border-[#2a2a2e] bg-[#101012]/95 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 面板标题栏 */}
        <div className="flex shrink-0 items-center justify-between border-b border-[#232327] px-5 py-3">
          <span className="text-xs font-light tracking-[0.3em] text-[#8a8578]">文件柜</span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={refresh}
              className="rounded-sm border border-[#2e2e33] p-1 text-[#8a8578] transition-colors hover:border-[#5a5a60] hover:text-[#e8e0d0]"
              title="刷新列表"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-sm border border-[#2e2e33] p-1 text-[#8a8578] transition-colors hover:border-[#5a5a60] hover:text-[#e8e0d0]"
              title="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 可滚动文件列表 */}
        <div className="nega-scroll flex-1 overflow-y-auto px-5 py-4">
          {files === null && <p className="py-8 text-center text-xs text-[#4a4a50]">读取中…</p>}
          {files !== null && files.length === 0 && (
            <p className="py-8 text-center text-xs text-[#4a4a50]">
              {failed ? '先生，文件柜读取失败了，请检查后端是否在线。' : 'Nolan 生成的文件会出现在这里'}
            </p>
          )}
          {files !== null && files.length > 0 && (
            <div className="flex flex-col gap-1">
              {files.map((f) => {
                const Icon = KIND_ICON[f.kind] ?? File
                return (
                  <button
                    key={f.name}
                    type="button"
                    onClick={() => window.open(cabinetFileUrl(f.name), '_blank')}
                    className="flex items-center gap-3 rounded-sm border border-transparent px-3 py-2 text-left transition-colors hover:border-[#2e2e33] hover:bg-[#151517]"
                    title="在新标签页打开 / 下载"
                  >
                    <Icon className="h-4 w-4 shrink-0 text-[#8a8578]" />
                    <span className="min-w-0 flex-1 truncate text-sm font-light text-[#e8e0d0]">
                      {f.name}
                    </span>
                    <span className="shrink-0 text-[10px] tracking-wider text-[#4a4a50]">
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
