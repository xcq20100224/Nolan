# Nolan 阶段 1 · 可靠执行闭环 —— 执行蓝图

> 第一性原理：助手价值 = 任务成功率ⁿ × 响应速度 × 信任度。
> 本阶段只回答一个问题：「它做成没有？」—— 不许加新功能。

## 北极星指标
- 50 题真实指令题库成功率 ≥ 98%，且每次改动不许回退。
- 演示三件套（网易云播放 / AI 日报 / 1 分钟叫醒）100% 闭环。

## Stage A · 侦察（explore 子代理）
摸清执行层现状，产出精确改动点：
- jarvis/hands.py：工具表、execute 分发、open_app/_try_startfile、_run_shell（阻塞点）、_gui_control（确认闸）、沙盒文件三件套
- jarvis/brain.py：_parse_intent、_execute_tool、_pending_shell 状态机、LLM Agent 循环（≤4 轮）、聊天主流程入口
- nolan-web/server.py：/api/chat 处理链、是否有全局锁、TTS 合成锁
输出：每个工具的「验证器该挂哪」「换路顺序」「队列该在哪一层串行化」。

## Stage B · 实现闭环（coder 子代理）
四个改动，全部落在 jarvis/ 与 nolan-web/：
1. **执行后自检（验证器注册表）**：每个动作类工具配 verify 函数——
   open_app → 窗口/进程存在；write_file → 读回内容一致；run_shell → 返回码+输出；
   gui_control → 结果话术非失败前缀（已有）+ eyes 内部步进验证。
2. **自动换路重试**：验证失败按降级链再试一次——
   gui_control → open_app 直达 → run_shell 构造命令；失败如实报，不谎称。
3. **任务队列串行化**：/api/chat 执行层互斥锁（brain 级），杜绝两条指令排队打架；
   挂起时返回「先生，上一件事还没办完」。
4. **进程非阻塞**：open_app 与 run_shell 拉起 GUI 一律 start/Popen 式，永不 subprocess.run 等待；
   run_shell 对 GUI 程序自动补 start。

## Stage C · 高考题库（coder 子代理）
jarvis/selftest_gaokao.py：50 条真实指令，覆盖 时间/打开应用/写读文件/搜索/提醒/媒体/复合任务/闲聊不该动手/危险命令确认。
每条断言 = 话术不含失败前缀 + 物理验证（文件存在/进程存在/状态文件）。
安全：GUI 类只跑白名单（记事本/计算器），真实打开真实关闭。

## Stage D · 迭代（主代理亲自）
跑题库 → 失败题逐条修 → 再跑，直到 ≥98%。

## Stage E · 三件套实测（主代理亲自）
真机：网易云播放喜欢第一首 / AI 日报写文件 / 1 分钟叫醒，全部闭环成功。

## Stage F · 提交推送
git commit；github.com 直连若仍不通，走 Git Data API（本次已验证的通道）。

---

# 阶段 2 · 感知升级（2026-08-01 开工）

> 阶段 1 回答「做成没有」；阶段 2 回答「看见没有」。
> 北极星：界面操作确定性——坐标不再靠 VLM 蒙像素。

## 2A · UIA 元素树感知（眼睛升级）
- 新模块 jarvis/uia.py：comtypes 直调 UIAutomationCore（已验证 comtypes 1.4.16 可用），
  导出目标窗口控件树（Name/ControlType/BoundingRectangle/IsEnabled），紧凑文本化；
  find_element(名称) → 矩形中心坐标。任何失败返回空，绝不拖垮 eyes。
- eyes.py 集成：观测 = 截图 + UIA 控件清单（可交互控件优先，限量防爆 prompt）；
  执行点击/输入时，目标点落入或匹配到控件 → 吸附到控件矩形中心（确定性坐标）。
- VLM 决策循环不变，UIA 只是「更准的眼睛」，不是新的大脑。

## 2B · 可打断（嘴巴升级）
- mouth：播放循环每 50ms 查中断事件，mouth.interrupt() 立即止播。
- server：/api/chat 入口先 mouth.interrupt()（新指令自动打断旧话）；
  新增 /api/stop；网页版加「打断」按钮。
- 砍：唤醒词与全双工——always-on 听音引擎 + 误触发调优是独立工程，移出本阶段。

## 2C · 题库扩充
- UIA dump  Sanity（记事本控件树含 文件/编辑）；interrupt 单元题；gui_control 记事本实操题（VLM 可用才跑）。

## 2D · 打包发布（主代理亲自）
- dist/Nolan-1.x.zip → 桌面副本 + 桌面快捷方式；
- GitHub Release（API + uploads.github.com 已验证可达）给公开下载链接。
