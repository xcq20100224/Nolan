// Nolan 回复的轻量 markdown 渲染（零依赖，手写解析器）
// 支持：**粗体** / *斜体* / `行内代码` / - 或 * 无序列表 / 1. 有序列表 /
//       ## 与 ### 小节标题 / ``` 围栏代码块 / 其余按段落与换行原样
// 安全：全程 React 文本节点渲染（天然转义，<script> 等原样显示），无 dangerouslySetInnerHTML。
// 性能：MarkdownContent 以 React.memo 包裹，历史消息不因流式 delta 重复解析。
import { memo, type ReactNode } from 'react'

/** 行内片段：纯文本 / 粗体 / 斜体 / 行内代码 */
export interface InlineToken {
  t: 'text' | 'bold' | 'italic' | 'code'
  s: string
}

/** 块级结构 */
export type Block =
  | { t: 'para'; lines: InlineToken[][] }
  | { t: 'code'; text: string }
  | { t: 'h2'; inline: InlineToken[] }
  | { t: 'h3'; inline: InlineToken[] }
  | { t: 'ul'; items: InlineToken[][] }
  | { t: 'ol'; items: InlineToken[][] }

/** 行内解析：先 `code`，再 **bold**，最后 *italic*（bold 优先避免被 italic 吃掉） */
export function parseInline(line: string): InlineToken[] {
  const tokens: InlineToken[] = []
  const re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) tokens.push({ t: 'text', s: line.slice(last, m.index) })
    if (m[1] !== undefined) tokens.push({ t: 'code', s: m[1] })
    else if (m[2] !== undefined) tokens.push({ t: 'bold', s: m[2] })
    else tokens.push({ t: 'italic', s: m[3] })
    last = m.index + m[0].length
  }
  if (last < line.length) tokens.push({ t: 'text', s: line.slice(last) })
  if (tokens.length === 0) tokens.push({ t: 'text', s: '' })
  return tokens
}

const RE_UL = /^[-*]\s+/
const RE_OL = /^\d+\.\s+/

/** 块级解析：逐行扫描，围栏代码块优先，其次标题 / 列表，最后段落（空行分段） */
export function parseMarkdown(src: string): Block[] {
  const lines = src.split('\n')
  const blocks: Block[] = []
  let i = 0
  /** 收集连续段落行（遇到其它块级起始或空行停止） */
  const flushPara = (start: number): number => {
    const buf: InlineToken[][] = []
    let j = start
    while (j < lines.length) {
      const l = lines[j]
      if (
        l.trim() === '' ||
        l.startsWith('```') ||
        l.startsWith('### ') ||
        l.startsWith('## ') ||
        RE_UL.test(l) ||
        RE_OL.test(l)
      )
        break
      buf.push(parseInline(l))
      j += 1
    }
    if (buf.length > 0) blocks.push({ t: 'para', lines: buf })
    return j
  }
  while (i < lines.length) {
    const l = lines[i]
    if (l.trim() === '') {
      i += 1
      continue
    }
    // 围栏代码块：``` 起，到下一个 ``` 止（未闭合则吃到末尾）
    if (l.startsWith('```')) {
      const buf: string[] = []
      i += 1
      while (i < lines.length && !lines[i].startsWith('```')) {
        buf.push(lines[i])
        i += 1
      }
      i += 1 // 跳过闭合 ```（越界时自然结束）
      blocks.push({ t: 'code', text: buf.join('\n') })
      continue
    }
    if (l.startsWith('### ')) {
      blocks.push({ t: 'h3', inline: parseInline(l.slice(4)) })
      i += 1
      continue
    }
    if (l.startsWith('## ')) {
      blocks.push({ t: 'h2', inline: parseInline(l.slice(3)) })
      i += 1
      continue
    }
    if (RE_UL.test(l)) {
      const items: InlineToken[][] = []
      while (i < lines.length && RE_UL.test(lines[i])) {
        items.push(parseInline(lines[i].replace(RE_UL, '')))
        i += 1
      }
      blocks.push({ t: 'ul', items })
      continue
    }
    if (RE_OL.test(l)) {
      const items: InlineToken[][] = []
      while (i < lines.length && RE_OL.test(lines[i])) {
        items.push(parseInline(lines[i].replace(RE_OL, '')))
        i += 1
      }
      blocks.push({ t: 'ol', items })
      continue
    }
    i = flushPara(i)
  }
  return blocks
}

/** 行内片段 → React 节点（文本直接作为字符串子节点，React 自动转义） */
function renderInline(tokens: InlineToken[], keyPrefix: string): ReactNode[] {
  return tokens.map((tok, i) => {
    const key = `${keyPrefix}-${i}`
    switch (tok.t) {
      case 'bold':
        return (
          <strong key={key} className="font-semibold">
            {tok.s}
          </strong>
        )
      case 'italic':
        return <em key={key}>{tok.s}</em>
      case 'code':
        return (
          <code
            key={key}
            className="rounded-[4px] bg-[var(--fill-f1)] px-1 py-0.5 font-mono text-[0.875em]"
          >
            {tok.s}
          </code>
        )
      default:
        return tok.s
    }
  })
}

/** 文本为纯段落（无任何块级结构）时退化为 pre-wrap 单段，排版与旧纯文本完全一致 */
function isPlain(blocks: Block[]): boolean {
  return blocks.every((b) => b.t === 'para') && blocks.length <= 1
}

/**
 * Nolan 消息的 markdown 正文。memo 包裹：props.text 不变时不重渲染不重解析，
 * 流式期间只有正在增长的最后一条消息会重复解析（每 delta 一次，可接受）。
 */
export const MarkdownContent = memo(function MarkdownContent({ text }: { text: string }) {
  const blocks = parseMarkdown(text)
  if (isPlain(blocks)) {
    // 纯文本：保持旧版 pre-wrap 观感
    return <span className="whitespace-pre-wrap">{text}</span>
  }
  return (
    <div className="flex flex-col">
      {blocks.map((b, bi) => {
        const gap = bi === 0 ? '' : 'mt-2'
        switch (b.t) {
          case 'code':
            return (
              <pre
                key={bi}
                className={`${gap} overflow-x-auto rounded-[8px] bg-[var(--fill-f1)] px-3 py-2 font-mono text-[13px] leading-5`}
              >
                {b.text}
              </pre>
            )
          case 'h2':
            return (
              <h2 key={bi} className={`${bi === 0 ? '' : 'mt-3'} mb-1 text-[16px] font-semibold leading-6`}>
                {renderInline(b.inline, `h2-${bi}`)}
              </h2>
            )
          case 'h3':
            return (
              <h3 key={bi} className={`${bi === 0 ? '' : 'mt-3'} mb-1 text-[15px] font-semibold leading-6`}>
                {renderInline(b.inline, `h3-${bi}`)}
              </h3>
            )
          case 'ul':
            return (
              <ul key={bi} className={`${gap} list-disc pl-5`}>
                {b.items.map((item, ii) => (
                  <li key={ii} className="text-[16px] leading-6">
                    {renderInline(item, `ul-${bi}-${ii}`)}
                  </li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={bi} className={`${gap} list-decimal pl-5`}>
                {b.items.map((item, ii) => (
                  <li key={ii} className="text-[16px] leading-6">
                    {renderInline(item, `ol-${bi}-${ii}`)}
                  </li>
                ))}
              </ol>
            )
          default:
            return (
              <p key={bi} className={`${gap} text-[16px] leading-6`}>
                {b.lines.map((line, li) => (
                  <span key={li}>
                    {li > 0 && <br />}
                    {renderInline(line, `p-${bi}-${li}`)}
                  </span>
                ))}
              </p>
            )
        }
      })}
    </div>
  )
})
