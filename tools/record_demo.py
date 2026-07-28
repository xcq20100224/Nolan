# -*- coding: utf-8 -*-
"""
Nolan 演示 GIF 一键录制器（tools/record_demo.py）

用途：自动向 Nolan 后端发一条指令（默认：打开网易云音乐放歌），
同时全屏截帧，把"鼠标自己动起来干活"的 25 秒录成 README 演示 GIF。

第一性原理：录制 = 截屏序列 + 拼 GIF，PIL 全搞定，不装 ffmpeg、不装新依赖。

用法（先确保 Nolan 网页版后端在 7101 端口运行）：
    python tools/record_demo.py                 # 默认指令 + 5 秒倒计时
    python tools/record_demo.py "1分钟后叫醒我"  # 自定义指令
    python tools/record_demo.py --no-send       # 只录屏不发指令（手动操作演示）

录制中会占用你的鼠标（Nolan 在操作 GUI），请勿碰键鼠。
产物：docs/demo.gif（约 25 秒，800px 宽，适合放在 README 顶部）。
"""

import io
import json
import os
import sys
import threading
import time
import urllib.request

_BACKEND = "http://127.0.0.1:7101"
_DEFAULT_CMD = "打开网易云音乐，播放我喜欢列表里的第一首歌"
_COUNTDOWN = 5            # 录制前倒计时（秒），给你把手离开键鼠的时间
_CAPTURE_SECONDS = 25     # 总录制时长
_INTERVAL = 0.4           # 截帧间隔（2.5 fps，GIF 体积与流畅度的平衡点）
_WIDTH = 800              # 输出 GIF 宽度（等比缩放）


def _send_command(text: str) -> None:
    """把指令 POST 给 Nolan 后端（后台线程执行，不阻塞截帧）。"""
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _BACKEND + "/api/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            reply = json.loads(r.read().decode("utf-8"))
        print(f"[record] Nolan 回复：{reply.get('reply', '')[:100]}")
    except Exception as e:
        print(f"[record] 指令发送失败（录制继续）：{e}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    command = args[0] if args else _DEFAULT_CMD
    no_send = "--no-send" in sys.argv

    from PIL import ImageGrab, Image  # 延迟导入，--help 类场景不碰 GUI 库

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "demo.gif")

    print("=" * 56)
    print(" Nolan 演示 GIF 录制器")
    print(f" 指令：{command if not no_send else '（只录屏，请手动演示）'}")
    print(f" 时长：{_CAPTURE_SECONDS} 秒 → {out_path}")
    print(" 录制期间请勿碰鼠标键盘（Nolan 要操作它们）")
    print("=" * 56)

    for i in range(_COUNTDOWN, 0, -1):
        print(f" {i} 秒后开始……")
        time.sleep(1)

    if not no_send:
        threading.Thread(target=_send_command, args=(command,), daemon=True).start()

    frames = []
    deadline = time.time() + _CAPTURE_SECONDS
    print("[record] 录制中……")
    while time.time() < deadline:
        shot = ImageGrab.grab()
        ratio = _WIDTH / shot.width
        frames.append(shot.resize((_WIDTH, int(shot.height * ratio)), Image.LANCZOS))
        time.sleep(_INTERVAL)

    print(f"[record] 截到 {len(frames)} 帧，正在合成 GIF……")
    # 调色板量化到 128 色：体积与观感的平衡点
    paletted = [f.convert("P", palette=Image.ADAPTIVE, colors=128) for f in frames]
    paletted[0].save(
        out_path, save_all=True, append_images=paletted[1:],
        duration=int(_INTERVAL * 1000), loop=0, optimize=True,
    )
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"[record] 完成：{out_path}（{size_mb:.1f} MB）")
    if size_mb > 10:
        print("[record] 提示：超过 10 MB，可缩短 _CAPTURE_SECONDS 或减小 _WIDTH 再录。")


if __name__ == "__main__":
    main()
