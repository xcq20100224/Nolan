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
}
