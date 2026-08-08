// 声波可视化（核心视觉）：
//   idle      —— 闲置：低幅慢速呼吸式正弦波
//   recording —— 录音：AnalyserNode 绘制真实麦克风波形（红色 status.danger）
//   busy      —— 等待回复 / 播报：快速律动模拟动画
// 颜色吃 CSS 变量（--wave-rgb = labels.primary 分量，--danger-rgb = status.danger 分量），
// 随亮 / 暗主题自动切换，三层线条透明度递减
import { useEffect, useRef } from 'react'

/** 声波模式：闲置 / 录音 / 等待回复 */
export type WaveMode = 'idle' | 'recording' | 'busy'

interface WaveCanvasProps {
  mode: WaveMode
  /** 录音中的 MediaStream（非录音时为 null），用于挂接 AnalyserNode */
  stream: MediaStream | null
}

/** 主题基色缓存：从 CSS 变量读取的 rgb 分量字符串（如 "0, 0, 0"） */
let cachedTheme: string | undefined
let cachedWaveRgb = '0, 0, 0'
let cachedDangerRgb = '255, 56, 73'

/** 主题变化时重读 CSS 变量（每帧比对 data-theme 属性，仅字符串比较，零成本） */
function refreshColors(): void {
  const theme = document.documentElement.dataset.theme
  if (theme === cachedTheme) return
  cachedTheme = theme
  const style = getComputedStyle(document.documentElement)
  cachedWaveRgb = style.getPropertyValue('--wave-rgb').trim() || '0, 0, 0'
  cachedDangerRgb = style.getPropertyValue('--danger-rgb').trim() || '255, 56, 73'
}

/** 时域采样点数（与分析器 fftSize 对应） */
const FFT_SIZE = 2048

export default function WaveCanvas({ mode, stream }: WaveCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  // 绘制循环每帧读取最新 mode，避免重建动画
  const modeRef = useRef<WaveMode>(mode)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)

  modeRef.current = mode

  // 录音流变化：建立 / 销毁 WebAudio 分析链路
  useEffect(() => {
    if (!stream) return

    let ctx: AudioContext | null = null
    try {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) return
      ctx = new Ctor()
      const source = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = FFT_SIZE
      analyser.smoothingTimeConstant = 0.75
      source.connect(analyser)
      audioCtxRef.current = ctx
      analyserRef.current = analyser
      // 部分浏览器初始为 suspended，尽力恢复
      void ctx.resume().catch(() => undefined)
    } catch {
      // 分析链路不可用时退化为模拟动画，不影响录音本身
      analyserRef.current = null
    }

    return () => {
      analyserRef.current = null
      audioCtxRef.current = null
      if (ctx) void ctx.close().catch(() => undefined)
    }
  }, [stream])

  // 主绘制循环（挂载一次，内部按 modeRef 分支）
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx2d = canvas.getContext('2d')
    if (!ctx2d) return

    let raf = 0
    let cssW = 0
    let cssH = 0

    /** 按容器尺寸与 DPR 调整画布，保证线条锐利 */
    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      cssW = rect.width
      cssH = rect.height
      canvas.width = Math.max(1, Math.round(rect.width * dpr))
      canvas.height = Math.max(1, Math.round(rect.height * dpr))
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    const observer = new ResizeObserver(resize)
    observer.observe(canvas)
    resize()

    const timeDomain = new Uint8Array(FFT_SIZE)
    const t0 = performance.now()

    /** 绘制一条横向波形线：sampler 返回 -1..1 的归一化振幅，rgb 为 "r, g, b" 分量串 */
    const strokeWave = (
      sampler: (x: number, w: number) => number,
      alpha: number,
      width: number,
      rgb: string,
    ) => {
      const cy = cssH / 2
      const amp = cssH * 0.32
      ctx2d.beginPath()
      ctx2d.strokeStyle = `rgba(${rgb}, ${alpha})`
      ctx2d.lineWidth = width
      const step = 2
      for (let x = 0; x <= cssW; x += step) {
        // 两端收束包络：让线条在边缘自然淡出
        const edge = Math.sin((x / cssW) * Math.PI)
        const y = cy + sampler(x, cssW) * amp * edge
        if (x === 0) ctx2d.moveTo(x, y)
        else ctx2d.lineTo(x, y)
      }
      ctx2d.stroke()
    }

    const draw = () => {
      raf = requestAnimationFrame(draw)
      if (cssW === 0 || cssH === 0) return
      const t = (performance.now() - t0) / 1000
      ctx2d.clearRect(0, 0, cssW, cssH)

      refreshColors()
      const current = modeRef.current
      const analyser = analyserRef.current
      // 录音态用 status.danger 红色，其余用 labels.primary 主题基色
      const base = current === 'recording' ? cachedDangerRgb : cachedWaveRgb
      // 亮色下黑线、暗色下白线都需要足够存在感：基线 alpha 按模式区分
      const dim = current === 'recording' ? 1 : cachedTheme === 'dark' ? 0.84 : 0.9

      if (current === 'recording' && analyser) {
        // 真实麦克风波形：时域数据映射到 -1..1
        analyser.getByteTimeDomainData(timeDomain)
        const sample = (x: number, w: number, scale: number, shift: number) => {
          const idx = Math.min(
            FFT_SIZE - 1,
            Math.floor(((x / w) * FFT_SIZE + shift + FFT_SIZE) % FFT_SIZE),
          )
          return ((timeDomain[idx] - 128) / 128) * scale
        }
        strokeWave((x, w) => sample(x, w, 1, 0), 0.9 * dim, 1.5, base)
        strokeWave((x, w) => sample(x, w, 0.5, 90), 0.25 * dim, 1, base)
        strokeWave((x, w) => sample(x, w, 0.25, 180), 0.12 * dim, 1, base)
        return
      }

      if (current === 'busy') {
        // 等待回复 / 播报：快速律动，幅度随时间起伏
        const pulse = 0.65 + 0.35 * Math.sin(t * 5.2)
        strokeWave(
          (x, w) =>
            (Math.sin((x / w) * Math.PI * 6 + t * 7) * 0.7 +
              Math.sin((x / w) * Math.PI * 13 - t * 11) * 0.3) *
            pulse,
          0.85 * dim,
          1.5,
          base,
        )
        strokeWave(
          (x, w) => Math.sin((x / w) * Math.PI * 4 - t * 5.5) * 0.5 * pulse,
          0.25 * dim,
          1,
          base,
        )
        strokeWave(
          (x, w) => Math.sin((x / w) * Math.PI * 9 + t * 9.5) * 0.25 * pulse,
          0.12 * dim,
          1,
          base,
        )
        return
      }

      // idle：低幅慢速呼吸
      const breath = 0.5 + 0.5 * Math.sin(t * 0.7)
      const ampScale = 0.18 + 0.14 * breath
      strokeWave(
        (x, w) => Math.sin((x / w) * Math.PI * 3 + t * 0.9) * ampScale,
        0.4 * dim,
        1.2,
        base,
      )
      strokeWave(
        (x, w) => Math.sin((x / w) * Math.PI * 2 - t * 0.6) * ampScale * 0.6,
        0.16 * dim,
        1,
        base,
      )
      strokeWave(
        (x, w) => Math.sin((x / w) * Math.PI * 5 + t * 0.4) * ampScale * 0.35,
        0.08 * dim,
        1,
        base,
      )
    }

    raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="block h-full w-full"
      aria-label="Nolan 声波可视化"
    />
  )
}
