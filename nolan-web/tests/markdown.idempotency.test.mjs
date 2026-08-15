// markdown.tsx 的 normalize/parse 幂等性 + Bug 2 机制回归测试。
// 项目无 vitest 基建，用 node_modules 里的 typescript 把 markdown.tsx 转译为
// 临时 ESM 再导入（react 打桩为恒等 memo，本测试只验纯函数，不碰渲染）。
// 运行：node tests/markdown.idempotency.test.mjs
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import ts from 'typescript'

const here = dirname(fileURLToPath(import.meta.url))
const srcPath = join(here, '..', 'src', 'lib', 'markdown.tsx')
const tmpPath = join(here, '.tmp-markdown.test.mjs')

let js = ts.transpileModule(readFileSync(srcPath, 'utf8'), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ES2022,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText
// 打桩 react / react/jsx-runtime：纯函数测试不需要真实渲染
writeFileSync(
  join(here, '.tmp-react-stub.mjs'),
  'export const memo = (f) => f\nexport const Fragment = Symbol("Fragment")\nexport const jsx = () => null\nexport const jsxs = () => null\n',
)
js = js
  .replace(/from\s+["']react\/jsx-runtime["']/, 'from "./.tmp-react-stub.mjs"')
  .replace(/from\s+["']react["']/, 'from "./.tmp-react-stub.mjs"')
writeFileSync(tmpPath, js)

const { normalizeStructure, parseMarkdown } = await import(pathToFileURL(tmpPath).href)

const structured =
  '## 三个要点\n- 第一，**结构**清晰。\n- 第二，分层说明。\n- 第三，结尾总结。'

// 1) normalizeStructure 幂等：二次规范化输出不变
const once = normalizeStructure(structured)
const twice = normalizeStructure(once)
assert.equal(twice, once, 'normalizeStructure 必须幂等')

// 2) 结构化文本能解出 h2 / ul / 粗体（流式期间的渲染依据）
const blocks = parseMarkdown(once)
assert.ok(blocks.some((b) => b.t === 'h2'), '应解析出 ## 标题块')
assert.ok(blocks.some((b) => b.t === 'ul'), '应解析出无序列表块')
const ul = blocks.find((b) => b.t === 'ul')
assert.ok(
  ul.items[0].some((tok) => tok.t === 'bold' && tok.s === '结构'),
  '列表项内 **粗体** 应解析为 bold token',
)

// 3) Bug 2 机制复现：speakable 折叠成单行后，列表/分段全部消失——
//    '- ' 不再是行首无法识别为列表，整个回复塌成孤零零一个块
//    （若折叠后行首恰好是 '## '，还会整行变成一个巨型标题，同样是结构塌方）。
const folded =
  '## 三个要点 - 第一，**结构**清晰。 - 第二，分层说明。 - 第三，结尾总结。'
const foldedBlocks = parseMarkdown(normalizeStructure(folded))
assert.equal(foldedBlocks.length, 1, '单行折叠文本只剩一个块')
assert.ok(
  !foldedBlocks.some((b) => b.t === 'ul' || b.t === 'ol'),
  '列表结构全丢：'- ' 不在行首则永不识别',
)

// 4) parse ∘ normalize 组合幂等：对规范化结果再跑一遍，块结构不变
assert.deepEqual(
  parseMarkdown(normalizeStructure(normalizeStructure(structured))),
  parseMarkdown(normalizeStructure(structured)),
  'parse∘normalize 必须幂等',
)

// 5) 代码围栏内的 --- 与 ** 不被 normalize 误伤
const withCode = '上文。\n```\n--- **不是标题**\n```\n下文。'
const codeOut = normalizeStructure(withCode)
assert.ok(codeOut.includes('--- **不是标题**'), '围栏内容必须原样保留')

rmSync(tmpPath, { force: true })
rmSync(join(here, '.tmp-react-stub.mjs'), { force: true })
console.log('markdown.idempotency.test.mjs: 5/5 通过')
