// 聊天消息类型定义
export interface Message {
  /** 唯一 ID */
  id: string
  /** 发言角色：user = 先生，nolan = Nolan */
  role: 'user' | 'nolan'
  /** 消息正文 */
  text: string
  /** 发送时间，格式 HH:MM */
  time: string
  /** 是否为「Nolan 思考中…」占位消息 */
  pending?: boolean
  /** 随消息发送的附件芯片（仅展示：文件名 + 类别 + 抽取字数；正文已拼入发给后端的 payload） */
  attachments?: { name: string; chars: number; kind?: string; note?: string }[]
  /** 大项目进度步骤（SSE progress 事件逐条追加；done=false 的为当前进行步骤） */
  progress?: { step: string; i?: number; n?: number; done: boolean }[]
  /** 工具完成（done/fallback/出错收尾）：全部步骤转完成态，进度区折叠为一行 */
  progressDone?: boolean
  /** 完成态下步骤列表是否展开（默认收起——克制原则） */
  progressExpanded?: boolean
}
