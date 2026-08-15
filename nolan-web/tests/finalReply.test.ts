// resolveFinalReply 单测（Node 24 原生跑 TS：node tests/finalReply.test.ts）
// 覆盖 Bug 2 修复的取舍规则：仅排版差异保留流式结构，内容差异用服务端权威版。
import assert from 'node:assert/strict'
import { resolveFinalReply } from '../src/lib/finalReply.ts'

const streamed =
  '## 三个要点\n- 第一，**结构**清晰。\n- 第二，分层说明。\n- 第三，结尾总结。'

// 1) 服务端版仅折叠空白（speakable 的 \s+ → 单空格）：保留流式原文的换行结构
const folded =
  '## 三个要点 - 第一，**结构**清晰。 - 第二，分层说明。 - 第三，结尾总结。'
assert.equal(
  resolveFinalReply(streamed, folded),
  streamed,
  '仅空白差异时必须保留流式原文（markdown 结构不塌）',
)

// 2) 服务端版剥了工具 JSON（内容真不同）：采用服务端权威版
const dirtyStream = '好的先生，正在处理。\n{"tool": "make_ppt", "args": {}}'
const cleaned = '好的先生，正在处理。'
assert.equal(
  resolveFinalReply(dirtyStream, cleaned),
  cleaned,
  '内容被剥离时必须以服务端清洗版为准',
)

// 3) 服务端版为空（剥完没剩人话前的边界）：保留流式原文
assert.equal(resolveFinalReply(streamed, ''), streamed, 'serverReply 为空保留流式原文')

// 4) 两端完全一致：返回原文（恒等）
assert.equal(resolveFinalReply(streamed, streamed), streamed)

// 5) 占位话术场景（服务端剥完为空给了占位）：内容不同，用服务端版
assert.equal(
  resolveFinalReply('{"tool":"x"}', '（正在处理，请稍候…）'),
  '（正在处理，请稍候…）',
)

console.log('finalReply.test.ts: 5/5 通过')
