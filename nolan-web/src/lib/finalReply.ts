// 流式终态文本取舍（Bug：长回答在 done 事件落地后 markdown 结构塌成单行纯文字）
//
// 根因链路：服务端 /api/chat/stream 的 done.reply 过了显示层卫生
// （server.py _display_clean → jarvis speak_filter.speakable），speakable 是
// 「可念性」过滤器，末尾把 \s+ 折叠成单空格——口播不需要排版，但显示层需要。
// 于是流式期间逐字累积的原文（带 ## 标题 / - 列表 / 换行）在 done 到达时被
// 单行清洗版整体替换，结构全塌。切出页面只是让这一步发生在用户不在场时。
//
// 取舍规则：若服务端版与流式累积原文「去空白后完全一致」（清洗只动了排版），
// 保留流式原文的结构；内容真有差异（工具 JSON / 代码块 / URL / 路径被剥离）
// 时以服务端权威版为准——卫生语义不破，排版结构不丢。

/** 去全部空白（换行/空格/制表），用于「内容是否一致」的排版无关比较 */
function squeeze(s: string): string {
  return s.replace(/\s+/g, '')
}

/**
 * 决定流式收尾时屏幕上的最终文本。
 * @param streamed     前端逐 delta 累积的原始全文
 * @param serverReply  服务端 done 事件的权威回复（已过显示层卫生）
 */
export function resolveFinalReply(streamed: string, serverReply: string): string {
  if (!serverReply) return streamed
  return squeeze(streamed) === squeeze(serverReply) ? streamed : serverReply
}
