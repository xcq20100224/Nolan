// markdown.tsx 解析器的 node 侧验证（无测试基建，独立脚本）
// 原理：用 typescript.transpileModule 把 src/lib/markdown.tsx 就地转译为 CJS 加载，
//       直接对 parseMarkdown / parseInline 的 AST 输出做断言。
// 运行：node scripts/verify-markdown.cjs
const fs = require('fs')
const path = require('path')
const Module = require('module')
const ts = require('typescript')

const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'lib', 'markdown.tsx'), 'utf8')
const js = ts.transpileModule(src, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
}).outputText
const m = new Module('markdown.tsx', module)
m.paths = module.paths
m._compile(js, path.join(__dirname, 'markdown.tsx'))
const { parseMarkdown, parseInline } = m.exports

let passed = 0
let failed = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) {
    passed += 1
    console.log(`PASS  ${name}`)
  } else {
    failed += 1
    console.log(`FAIL  ${name}\n  expected: ${e}\n  actual:   ${a}`)
  }
}

// 1. 粗体 / 斜体 / 行内代码
check('粗体', parseInline('这是 **重点** 内容'), [
  { t: 'text', s: '这是 ' },
  { t: 'bold', s: '重点' },
  { t: 'text', s: ' 内容' },
])
check('斜体', parseInline('这是 *强调* 内容'), [
  { t: 'text', s: '这是 ' },
  { t: 'italic', s: '强调' },
  { t: 'text', s: ' 内容' },
])
check('行内代码', parseInline('执行 `npm run dev` 即可'), [
  { t: 'text', s: '执行 ' },
  { t: 'code', s: 'npm run dev' },
  { t: 'text', s: ' 即可' },
])

// 2. 嵌套符号：粗体优先于斜体（** 不被 * 吃掉）；列表项内嵌粗体+代码
check('粗体不被斜体误吞', parseInline('**加粗** 与 *斜体*'), [
  { t: 'bold', s: '加粗' },
  { t: 'text', s: ' 与 ' },
  { t: 'italic', s: '斜体' },
])
check('列表项内嵌粗体+行内代码', parseMarkdown('- 修改 **config** 里的 `port` 字段'), [
  {
    t: 'ul',
    items: [[
      { t: 'text', s: '修改 ' },
      { t: 'bold', s: 'config' },
      { t: 'text', s: ' 里的 ' },
      { t: 'code', s: 'port' },
      { t: 'text', s: ' 字段' },
    ]],
  },
])

// 3. 无序列表（- 与 * 混用）与有序列表
check('无序列表', parseMarkdown('- 第一项\n* 第二项\n- 第三项'), [
  { t: 'ul', items: [[{ t: 'text', s: '第一项' }], [{ t: 'text', s: '第二项' }], [{ t: 'text', s: '第三项' }]] },
])
check('有序列表', parseMarkdown('1. 先做这个\n2. 再做那个'), [
  { t: 'ol', items: [[{ t: 'text', s: '先做这个' }], [{ t: 'text', s: '再做那个' }]] },
])

// 4. 小节标题
check('## 标题', parseMarkdown('## 总体方案'), [{ t: 'h2', inline: [{ t: 'text', s: '总体方案' }] }])
check('### 标题', parseMarkdown('### 细节'), [{ t: 'h3', inline: [{ t: 'text', s: '细节' }] }])

// 5. 围栏代码块（内部符号不解析，未闭合吃到末尾）
check(
  '代码块',
  parseMarkdown('```\nconst a = **不是粗体**\n```\n结束语'),
  [
    { t: 'code', text: 'const a = **不是粗体**' },
    { t: 'para', lines: [[{ t: 'text', s: '结束语' }]] },
  ],
)
check('未闭合代码块', parseMarkdown('```\nline1\nline2'), [{ t: 'code', text: 'line1\nline2' }])

// 6. HTML 原样显示（React 文本节点天然转义，解析器不得吞掉 <script>）
check('HTML 转义（<script> 原样保留为文本）', parseMarkdown('<script>alert(1)</script>'), [
  { t: 'para', lines: [[{ t: 'text', s: '<script>alert(1)</script>' }]] },
])

// 7. 段落与换行：连续行并入一段，空行分段
check('段落与换行', parseMarkdown('第一行\n第二行\n\n第二段'), [
  { t: 'para', lines: [[{ t: 'text', s: '第一行' }], [{ t: 'text', s: '第二行' }]] },
  { t: 'para', lines: [[{ t: 'text', s: '第二段' }]] },
])

// 8. 纯文本（无 markdown 符号）：单 para，渲染层会退化为 pre-wrap
check('纯文本', parseMarkdown('先生，Nolan 在线，请讲。'), [
  { t: 'para', lines: [[{ t: 'text', s: '先生，Nolan 在线，请讲。' }]] },
])

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
