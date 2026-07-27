# -*- coding: utf-8 -*-
"""Nolan 语音助手 · tkinter 聊天窗口（nolan_app.py）· 阶段四

职责（界面工程师_UI 维护）：
    把 Nolan 包装成可双击的软件窗口：上方只读对话区（区分『你』『Nolan』与时间戳），
    底部输入框 + 『发送』+ 『🎤 说话』+ 状态栏。回车即发送；brain.think 在后台线程
    执行，结果经队列由主线程回显；mouth.speak 在后台线程串行播报（threading.Lock），
    UI 永不冻结。后台提醒线程每 20 秒检查 reminders.check_due()，到点项以 Nolan
    身份追加对话并播报。

设计约束（第一性原理）：
    - 仅用标准库 tkinter，不引 Web 框架、不加 pip 依赖；
    - ears / mouth 惰性导入（后台线程预加载），导入失败时麦克风按钮置灰并提示；
    - reminders 由并行工程师编写，按契约防御导入，缺失时提醒功能静默降级；
    - import 本模块不弹窗、不启动主循环（入口在 __main__ 守卫内）。

跨模块接口契约（一字不差）：
    brain.think(user_text: str, history: list[dict]) -> str   # 退出返回 '__EXIT__'
    ears.listen_once(timeout: float = 30.0) -> str | None
    mouth.speak(text: str) -> None
    reminders.check_due() -> list[str]
"""

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import scrolledtext

# —— 视觉常量：低饱和暖色调 ——
BG_COLOR = "#f7f4ef"      # 背景：暖米白
FG_COLOR = "#3d3d3d"      # 正文：深灰
ACCENT_COLOR = "#8a6d4b"  # 点缀：暖棕（按钮、Nolan 名）
TIME_COLOR = "#a89f91"    # 时间戳：浅灰棕
ENTRY_BG = "#fffdf9"      # 输入框底色

EXIT_TOKEN = "__EXIT__"          # 与 brain 约定的退出哨兵
GREETING = "先生，Nolan 在线，请讲。"
FAREWELL = "先生，Nolan 已下线，再见。"
REMINDER_INTERVAL = 20           # 提醒轮询间隔（秒）
MAX_HISTORY_MESSAGES = 20 * 2    # 对话历史最多保留最近 20 轮

# reminders 由并行工程师编写：防御导入，缺失时提醒功能静默降级
try:
    import reminders
except ImportError:  # pragma: no cover - reminders 未就绪时窗口仍可运行
    reminders = None


class NolanApp:
    """Nolan 聊天窗口。所有控件更新只发生在主线程；后台线程一律经队列通信。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Nolan")
        self.root.geometry("500x640")
        self.root.configure(bg=BG_COLOR)

        self._closed = False               # 窗口关闭标志，后台线程据此停止回显
        self._events: queue.Queue = queue.Queue()  # 后台线程 → 主线程事件队列
        self._speak_lock = threading.Lock()        # 播报串行锁，防止多句叠音
        self._history: list[dict] = []             # 对话历史（与 brain.think 契约一致）
        self._pending = 0                          # 正在思考的条数
        self._ears = None
        self._mouth = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动：显示问候，后台预加载语音模块（完成后播报问候），并启动提醒轮询
        self._append("Nolan", GREETING)
        threading.Thread(target=self._load_voice, daemon=True).start()
        threading.Thread(target=self._reminder_loop, daemon=True).start()
        self.root.after(100, self._poll_events)

    # —— 界面搭建 ——

    def _build_ui(self) -> None:
        """搭建对话区、输入区与状态栏。"""
        self.chat = scrolledtext.ScrolledText(
            self.root, wrap="word", state="disabled",
            bg=BG_COLOR, fg=FG_COLOR, relief="flat",
            font=("Microsoft YaHei UI", 10), padx=10, pady=10,
        )
        self.chat.tag_config("you", foreground=FG_COLOR, font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_config("nolan", foreground=ACCENT_COLOR, font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_config("time", foreground=TIME_COLOR, font=("Microsoft YaHei UI", 8))
        self.chat.tag_config("text", foreground=FG_COLOR)
        self.chat.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        bottom = tk.Frame(self.root, bg=BG_COLOR)
        bottom.pack(fill="x", padx=10, pady=(0, 4))

        self.entry = tk.Entry(
            bottom, bg=ENTRY_BG, fg=FG_COLOR, relief="solid", bd=1,
            font=("Microsoft YaHei UI", 10), insertbackground=FG_COLOR,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self.on_send())
        self.entry.focus_set()

        self.send_btn = tk.Button(
            bottom, text="发送", command=self.on_send,
            bg=ACCENT_COLOR, fg="#ffffff", relief="flat",
            activebackground="#7a5f3f", activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9), padx=12,
        )
        self.send_btn.pack(side="left", padx=(0, 6))

        self.voice_btn = tk.Button(
            bottom, text="🎤 加载中…", command=self.on_voice, state="disabled",
            bg=ACCENT_COLOR, fg="#ffffff", relief="flat",
            disabledforeground="#d8cfc2",
            activebackground="#7a5f3f", activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9), padx=10,
        )
        self.voice_btn.pack(side="left")

        self.status = tk.Label(
            self.root, text="就绪", anchor="w",
            bg=BG_COLOR, fg=TIME_COLOR, font=("Microsoft YaHei UI", 8),
        )
        self.status.pack(fill="x", padx=12, pady=(0, 6))

    # —— 对话区输出（仅主线程调用）——

    def _append(self, who: str, text: str) -> None:
        """向只读对话区追加一条带时间戳的消息。who 为『你』或『Nolan』。"""
        stamp = datetime.now().strftime("%H:%M")
        self.chat.configure(state="normal")
        self.chat.insert("end", f"[{stamp}] ", "time")
        self.chat.insert("end", f"{who}：", "you" if who == "你" else "nolan")
        self.chat.insert("end", f"{text}\n", "text")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # —— 发送与大脑思考 ——

    def on_send(self) -> None:
        """发送按钮 / 回车：回显用户消息，后台线程跑 brain.think。"""
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._append("你", text)
        self._pending += 1
        self._set_status("Nolan 思考中……")
        snapshot = list(self._history)  # 历史快照，避免后台线程读到写途中的列表
        threading.Thread(target=self._think_worker, args=(text, snapshot), daemon=True).start()

    def _think_worker(self, text: str, history: list[dict]) -> None:
        """后台线程：调用大脑拿回复，异常时降级为提示文本。"""
        try:
            import brain
            reply = brain.think(text, history)
        except Exception as exc:  # noqa: BLE001 —— 大脑异常不应冻结窗口
            reply = f"先生，Nolan 的大脑出了点状况：{exc}"
        self._events.put(("reply", text, reply))

    def _on_reply(self, text: str, reply: str) -> None:
        """主线程：回显 Nolan 回复，维护历史，后台播报。"""
        self._pending = max(0, self._pending - 1)
        if reply == EXIT_TOKEN:
            self._append("Nolan", FAREWELL)
            self._set_status("Nolan 已下线")
            self._speak_async(FAREWELL)
            self.root.after(1500, self._on_close)  # 道别后自动关窗
            return
        self._append("Nolan", reply)
        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": reply})
        if len(self._history) > MAX_HISTORY_MESSAGES:
            self._history = self._history[-MAX_HISTORY_MESSAGES:]
        if self._pending == 0:
            self._set_status("就绪")
        self._speak_async(reply)

    # —— 语音输入 ——

    def _load_voice(self) -> None:
        """后台线程：惰性导入 ears / mouth；失败则通知主线程置灰按钮。"""
        try:
            import ears
            import mouth
            self._ears, self._mouth = ears, mouth
            self._events.put(("voice_ready", True, ""))
        except Exception as exc:  # noqa: BLE001 —— 含 ImportError 与设备初始化失败
            self._events.put(("voice_ready", False, str(exc)))

    def on_voice(self) -> None:
        """🎤 按钮：后台线程听一句话，识别结果自动填入并发送。"""
        if self._ears is None:
            return
        self.voice_btn.configure(state="disabled", text="🎤 聆听中…")
        self._set_status("正在聆听，请讲……")
        threading.Thread(target=self._listen_worker, daemon=True).start()

    def _listen_worker(self) -> None:
        """后台线程：识别一句话；超时或静音返回 None。"""
        try:
            text = self._ears.listen_once()
        except Exception:  # noqa: BLE001 —— 识别失败按未听清处理
            text = None
        self._events.put(("voice_result", text))

    def _on_voice_result(self, text: "str | None") -> None:
        """主线程：识别成功则自动发送，失败则提示重说。"""
        self.voice_btn.configure(state="normal", text="🎤 说话")
        if text:
            self.entry.delete(0, "end")
            self.entry.insert(0, text)
            self.on_send()
        else:
            self._set_status("没听清，请再试一次。")

    # —— 语音播报（后台线程串行）——

    def _speak_async(self, text: str) -> None:
        """把一次播报丢给后台线程；threading.Lock 保证多句排队串行播放。"""
        if self._mouth is None:
            return

        def _run() -> None:
            with self._speak_lock:
                try:
                    self._mouth.speak(text)
                except Exception:  # noqa: BLE001 —— 播报失败静默降级为纯文字
                    pass

        threading.Thread(target=_run, daemon=True).start()

    # —— 主动提醒 ——

    def _reminder_loop(self) -> None:
        """后台守护线程：每 20 秒弹出到点提醒，交给主线程回显并播报。"""
        while not self._closed:
            time.sleep(REMINDER_INTERVAL)
            if self._closed or reminders is None:
                continue
            try:
                due = reminders.check_due()
            except Exception:  # noqa: BLE001 —— 存储异常时本轮跳过
                continue
            for item in due:
                self._events.put(("reminder", item))

    # —— 事件泵：后台线程的唯一回显通道 ——

    def _poll_events(self) -> None:
        """主线程每 100ms 排空事件队列，保证控件更新只发生在主线程。"""
        if self._closed:
            return
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "reply":
                    self._on_reply(event[1], event[2])
                elif kind == "voice_ready":
                    ok, err = event[1], event[2]
                    if ok:
                        self.voice_btn.configure(state="normal", text="🎤 说话")
                        self._speak_async(GREETING)
                    else:
                        self.voice_btn.configure(state="disabled", text="🎤 不可用")
                        self._set_status(f"语音不可用（{err}），可使用文字对话。")
                elif kind == "voice_result":
                    self._on_voice_result(event[1])
                elif kind == "reminder":
                    self._append("Nolan", event[1])
                    self._speak_async(event[1])
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(100, self._poll_events)

    # —— 退出 ——

    def _on_close(self) -> None:
        """关闭窗口：置标志位让守护线程自然退出，随后销毁窗口。"""
        if self._closed:
            return
        self._closed = True
        self.root.destroy()


def main() -> None:
    """创建窗口并进入主循环。仅在作为脚本运行时调用。"""
    root = tk.Tk()
    NolanApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
