# -*- coding: utf-8 -*-
"""Nolan 语音助手 —— 主循环入口。

职责（入口工程师_Launcher 本轮维护）：
    启动横幅 + 语音问候 → 循环监听（ears）→ 思考（brain）→ 播报（mouth），
    维护对话历史，处理退出指令与 Ctrl+C，单次模块异常不中断主循环。
    命令行带 --text 时进入纯文本测试模式：不初始化耳朵和嘴巴，
    用 input 读入、只 print 不语音播报，其余主循环逻辑完全一致。

跨模块接口契约（一字不差）：
    ears.listen_once(timeout: float = 30.0) -> str | None
    mouth.speak(text: str) -> None
    brain.think(user_text: str, history: list[dict]) -> str
"""

import sys

# 约定的退出哨兵：brain 返回该字符串表示用户想结束对话
EXIT_TOKEN = "__EXIT__"
# 唤醒词：句首的「Nolan」（大小写不敏感）或「诺兰」会被剥离；旧名「贾维斯」不再作为唤醒词
WAKE_WORDS = ("nolan", "诺兰")
# 对话历史最多保留最近 20 轮（40 条消息）
MAX_HISTORY_MESSAGES = 20 * 2

# 启动问候与退出道别（用户可见文案，助手自称 Nolan，称呼用户「先生」）
GREETING = "先生，Nolan 在线，请讲。"
FAREWELL = "先生，Nolan 已下线，再见。"

BANNER = r"""
============================================================
   🤖 Nolan 语音助手
   麦克风 → Whisper 识别 → 大脑回复 → 固定音色播报
   （命令行加 --text 进入纯文本测试模式）
============================================================
"""


def _strip_wake(text: str) -> str:
    """大小写不敏感地剥离句首唤醒词『Nolan』或『诺兰』及其后标点。

    命中唤醒词时返回剥离后的干净指令；未命中（含旧名『贾维斯』）时原样返回。
    """
    stripped = text.strip()
    lower = stripped.lower()
    for word in WAKE_WORDS:
        if lower.startswith(word):
            return stripped[len(word):].lstrip("，,。.!！?？:：;；、 ")
    return text


def _trim_history(history: list[dict]) -> list[dict]:
    """裁剪历史，仅保留最近 MAX_HISTORY_MESSAGES 条消息。"""
    if len(history) > MAX_HISTORY_MESSAGES:
        return history[-MAX_HISTORY_MESSAGES:]
    return history


def main() -> None:
    """Nolan 主循环。--text 参数进入纯文本测试模式。"""
    text_mode = "--text" in sys.argv[1:]

    print(BANNER)

    if text_mode:
        # 纯文本测试模式：不导入/初始化耳朵和嘴巴，input 读入、print 输出
        import brain

        def listen() -> str | None:
            try:
                line = input("你说：")
            except EOFError:
                return EXIT_TOKEN  # 输入流关闭视为退出
            return line if line.strip() else None

        def speak(text: str) -> None:
            pass  # 文本模式只 print，不语音播报

        print("（纯文本测试模式：直接打字，Ctrl+C 或输入退出指令结束）")
    else:
        import threading

        import brain
        import ears
        import mouth

        def listen() -> str | None:
            return ears.listen_once()

        def speak(text: str) -> None:
            """可打断播报（P3 全双工）：播放期间后台监听，主人持续开口即打断。

            打断是增强而非必需：监听线程任何异常都静默退出，绝不拖垮播报；
            被打断后主循环自然进入下一轮 listen()，接住主人的新指令。
            """
            try:
                stop = threading.Event()
                interrupted = threading.Event()

                def _on_voice() -> None:
                    interrupted.set()
                    mouth.interrupt()  # mouth 播放轮询 50ms 内响应

                t = threading.Thread(
                    target=ears.watch_for_voice,
                    args=(_on_voice, stop),
                    daemon=True,  # 守护线程：主流程退出不残留
                )
                t.start()
                try:
                    mouth.speak(text)
                finally:
                    stop.set()
                    t.join(timeout=2)  # 等监听流关闭，避免与下一轮 listen 撞流
                if interrupted.is_set():
                    print("⏸️ Nolan：主人打断了播报，请讲。")
            except Exception as exc:  # noqa: BLE001 —— 播报失败不应中断主循环
                print(f"⚠️ 语音播报失败：{exc}")

        speak(GREETING)

    history: list[dict] = []

    try:
        while True:
            # —— 耳朵：监听一句话 ——
            try:
                user_text = listen()
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 监听出错，继续聆听：{exc}")
                continue

            if user_text is None:
                # 超时、纯静音或空输入：顺手弹出到点提醒（阶段四·主动性），
                # 并评估条件触发任务（P4：如果X就Y / 每隔N做Y），再继续听
                try:
                    import reminders
                    due_items = reminders.check_due()
                except Exception:  # noqa: BLE001 —— reminders 未就绪或存储异常时静默跳过
                    due_items = []
                try:
                    import triggers
                    due_items += triggers.check_due(
                        executor=lambda cmd: brain.think(cmd, history),
                        evaluator=brain.eval_condition,
                    )
                except Exception:  # noqa: BLE001 —— triggers 未就绪时静默跳过
                    pass
                for item in due_items:
                    print(f"⏰ Nolan：{item}")
                    speak(item)
                continue

            if user_text == EXIT_TOKEN:
                print("👋 输入流已关闭。")
                speak(FAREWELL)
                break

            print(f"🗣️  你说：{user_text}")

            # 剥离唤醒词
            text = _strip_wake(user_text).strip()
            if not text:
                # 空指令（只喊了名字），继续听
                print("👂 请讲。")
                continue

            # —— 大脑：生成回复 ——
            try:
                reply = brain.think(text, history)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 大脑出错，本轮跳过：{exc}")
                continue

            if not isinstance(reply, str) or not reply.strip():
                print("⚠️ 大脑返回了空回复，本轮跳过。")
                continue

            # —— 退出判定 ——
            if reply == EXIT_TOKEN:
                print("👋 收到退出指令。")
                speak(FAREWELL)
                break

            # —— 嘴巴：播报回复 ——
            print(f"🤖 Nolan：{reply}")
            speak(reply)

            # —— 维护对话历史 ——
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            history = _trim_history(history)

    except KeyboardInterrupt:
        print("\n🛑 检测到 Ctrl+C，正在退出……")
        speak(FAREWELL)
        sys.exit(0)


if __name__ == "__main__":
    main()
