# -*- coding: utf-8 -*-
"""
Nolan 自录自配演示视频生成器（tools/record_video.py）

概念：Nolan 给自己拍宣传片——edge-tts 男声旁白 + 真机操作录屏 + ffmpeg 合成。

分镜：
  1. 片头字幕卡 + 旁白开场
  2. 演示一：语音让 Nolan 打开网易云放喜欢第一首（真机录屏）
  3. 演示二：联网搜 AI 新闻写日报.txt（真机录屏）
  4. 片尾字幕卡 + 旁白收尾

依赖：edge_tts（已装）、imageio_ffmpeg 提供的 ffmpeg、PIL。
前置：Nolan 后端在 7101 端口运行；录制期间请勿碰键鼠。

产物：docs/nolan-demo.mp4（1280 宽，H.264 + AAC）。
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

_BACKEND = "http://127.0.0.1:7101"
_VOICE = "zh-CN-YunjianNeural"   # 与 Nolan 备用声线一致（浑厚男声）
_FPS = 2                          # 截屏帧率
_WIDTH = 1280
_FONT = r"C:\Windows\Fonts\STSONG.TTF"
_COUNTDOWN = 5

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "docs")
_WORK = os.path.join(_HERE, "..", "launch", "video-work")

# 分镜脚本：(类型, 旁白, 指令, 最短录屏秒数, 收尾秒数)
# 第一性原理：演示必须 100% 可复现。 gui_control 视觉操作震撼但受界面状态影响大，
# 所以分镜只承诺「确定性动作」——能打开、能播放、能写文件、能自己用记事本展示。
SEGMENTS = [
    ("card", "你好，我是 Nolan，一个住在你电脑里的 AI 管家。接下来这台电脑的鼠标，不归人类管。", None, 0, 0),
    ("demo", "比如你说一句：打开网易云音乐，播放我喜欢列表里的第一首歌。剩下的，看它表演。",
     "打开网易云音乐，播放我喜欢列表里的第一首歌", 22, 5),
    ("demo", "它还能随叫随到。一句话，记事本就在眼前，全程不用人碰键盘。",
     "打开记事本", 20, 5),
    ("card", "Nolan，本地优先，开源免费。GitHub 搜索 Nolan，十分钟，让它也住进你的电脑。", None, 0, 0),
]

_sender_done = threading.Event()


def _post_chat(text: str) -> str:
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _BACKEND + "/api/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        reply = json.loads(r.read().decode("utf-8"))
    return reply.get("reply", "")


def _send_with_confirm(text: str) -> None:
    """发单条指令；若 Nolan 走 gui_control 安全闸请求确认，自动替观众点「确认」。"""
    reply = _post_chat(text)
    print(f"[seg] Nolan 回复：{reply[:80]}")
    for _ in range(3):
        if "您确认执行吗" not in reply:
            break
        print("[seg] 触发界面操作安全闸，自动确认……")
        reply = _post_chat("确认")
        print(f"[seg] Nolan 回复：{reply[:80]}")


def _send_command(command) -> None:
    """执行一条或一组指令（列表时按序执行，步间留 3 秒等应用就绪）。"""
    try:
        steps = list(command) if isinstance(command, (list, tuple)) else [command]
        for i, step in enumerate(steps):
            if i:
                time.sleep(3)
            _send_with_confirm(step)
    except Exception as e:
        print(f"[seg] 指令失败（继续录）：{e}")
    finally:
        _sender_done.set()


def _probe_duration(path: str) -> float:
    """用 ffmpeg 读音频时长（秒），读不到给 6 秒兜底。"""
    p = subprocess.run(
        [_ffmpeg(), "-i", path], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    for line in (p.stderr or "").splitlines():
        if "Duration" in line:
            hms = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = hms.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 6.0


def _tts(text: str, out_mp3: str):
    """合成旁白，返回 (时长秒, 实际音频路径)。

    声线必须与 Nolan 本人一致且全片统一：
      1) GLM-TTS 主通道（智谱 glm-tts，male，wav，与 Nolan 主声线同源）
      2) edge-tts 备用（zh-CN-YunjianNeural，mp3，重试两次）
      3) SAPI 离线兜底（wav，宁可降级也不让视频失声）
    """
    wav_path = os.path.splitext(out_mp3)[0] + ".wav"

    # 通道一：GLM-TTS 主通道
    try:
        jarvis_dir = os.path.normpath(os.path.join(_HERE, "..", "jarvis"))
        if jarvis_dir not in sys.path:
            sys.path.insert(0, jarvis_dir)
        import mouth
        audio = mouth._synthesize_glm_tts(text)
        with open(wav_path, "wb") as f:
            f.write(audio)
        if os.path.getsize(wav_path) > 0:
            return _probe_duration(wav_path), wav_path
        raise RuntimeError("GLM-TTS 合成结果为空文件")
    except Exception as e:
        print(f"[seg] GLM-TTS 主通道失败：{e}，降级 edge-tts。")

    # 通道二：edge-tts 备用
    import edge_tts

    async def _run():
        c = edge_tts.Communicate(text, _VOICE)
        await c.save(out_mp3)

    last_exc = None
    for attempt in range(3):
        try:
            asyncio.run(_run())
            if os.path.getsize(out_mp3) > 0:
                return _probe_duration(out_mp3), out_mp3
            raise RuntimeError("edge-tts 合成结果为空文件")
        except Exception as e:
            last_exc = e
            print(f"[seg] edge-tts 第 {attempt + 1} 次失败：{e}")
            time.sleep(1.5)

    # 通道三：SAPI 离线兜底
    print("[seg] 在线通道均失败，降级 SAPI 离线语音（wav）。")
    import pyttsx3
    engine = pyttsx3.init()
    try:
        for voice in engine.getProperty("voices"):
            meta = f"{voice.id} {voice.name}".lower()
            if "zh" in meta or "chinese" in meta or "huihui" in meta or "xiaoxiao" in meta:
                engine.setProperty("voice", voice.id)
                break
        engine.save_to_file(text, wav_path)
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass
    if not (os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0):
        raise last_exc or RuntimeError("SAPI 离线合成也失败了")
    return _probe_duration(wav_path), wav_path


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _card_frames(text_lines, seconds: float, out_dir: str) -> int:
    """生成字幕卡帧序列，返回帧数。"""
    from PIL import Image, ImageDraw, ImageFont

    n = max(1, int(seconds * _FPS))
    w, h = _WIDTH, int(_WIDTH * 9 / 16)
    img = Image.new("RGB", (w, h), (14, 16, 20))
    d = ImageDraw.Draw(img)
    fonts = {
        "big": ImageFont.truetype(_FONT, 90),
        "small": ImageFont.truetype(_FONT, 40),
    }
    y = h // 2 - 120
    for i, (txt, font_name, color) in enumerate(text_lines):
        font = fonts[font_name] if isinstance(font_name, str) else font_name
        box = d.textbbox((0, 0), txt, font=font)
        d.text(((w - (box[2] - box[0])) / 2, y), txt, font=font, fill=color)
        y += 130 if i == 0 else 70
    os.makedirs(out_dir, exist_ok=True)
    for i in range(n):
        img.save(os.path.join(out_dir, f"f{i:05d}.png"))
    return n


def _record_screen(seconds_min: float, command, grace: float, out_dir: str) -> float:
    """截屏录 action，返回实际录制秒数。"""
    from PIL import ImageGrab, Image

    os.makedirs(out_dir, exist_ok=True)
    _sender_done.clear()
    if command:
        threading.Thread(target=_send_command, args=(command,), daemon=True).start()
    else:
        _sender_done.set()

    started = time.time()
    done_at = None
    idx = 0
    while True:
        elapsed = time.time() - started
        if _sender_done.is_set() and done_at is None:
            done_at = elapsed
        if elapsed >= max(seconds_min, (done_at or 0) + grace) and elapsed >= seconds_min:
            if done_at is not None or not command:
                break
        if elapsed > 240:  # 单段硬上限（视觉操作可能很慢，但绝不能拖死下一段）
            break
        shot = ImageGrab.grab()
        ratio = _WIDTH / shot.width
        shot = shot.resize((_WIDTH, int(shot.height * ratio)), Image.LANCZOS)
        shot.save(os.path.join(out_dir, f"f{idx:05d}.png"))
        idx += 1
        time.sleep(1 / _FPS)
    # 到下一段之前，必须等本条指令彻底收尾，杜绝两段指令在后端排队打架
    if command and not _sender_done.is_set():
        print("[seg] 录屏到上限，等待指令收尾……")
        _sender_done.wait(timeout=120)
    return time.time() - started


def _mux(seg_idx: int, frames_dir: str, audio: str, out_mp4: str) -> None:
    """把帧序列 + 旁白合成一段 mp4。

    时长必须钉死为 帧数/帧率：edge-tts 的 mp3 时长元数据不可靠，
    「apad + -shortest」会被错误元数据拖出 11 分钟的废片（已实测踩坑）。
    """
    frames = [f for f in os.listdir(frames_dir) if f.startswith("f") and f.endswith(".png")]
    video_seconds = max(1, len(frames)) / _FPS
    ff = _ffmpeg()
    subprocess.run([
        ff, "-y",
        "-framerate", str(_FPS), "-i", os.path.join(frames_dir, "f%05d.png"),
        "-i", audio,
        "-t", f"{video_seconds:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_mp4,
    ], check=True, capture_output=True)


def main() -> None:
    os.makedirs(_OUT_DIR, exist_ok=True)
    os.makedirs(_WORK, exist_ok=True)

    print("=" * 56)
    print(" Nolan 自录自配演示视频")
    print(" 录制期间请勿碰鼠标键盘（Nolan 要操作它们）")
    print("=" * 56)
    for i in range(_COUNTDOWN, 0, -1):
        print(f" {i} 秒后开始……")
        time.sleep(1)

    # 清场：用 Win+M（全部最小化，幂等），不用 Win+D（桌面/还原是开关，
    # 续录时若已在桌面会被误翻回 Kimi 窗口，污染录屏）
    try:
        import pyautogui
        pyautogui.hotkey("win", "m")
        time.sleep(1.2)
    except Exception as e:
        print(f"[warn] 清场失败：{e}")

    parts = []
    for idx, (kind, narration, command, min_sec, grace) in enumerate(SEGMENTS):
        part = os.path.join(_WORK, f"part{idx}.mp4")
        # 断点续录：已合成的分段绝不重做（保住已完成工作，也避免重复 TTS 再踩网络坑）
        if os.path.isfile(part) and os.path.getsize(part) > 0:
            print(f"\n[seg {idx + 1}/{len(SEGMENTS)}] 已存在，跳过：{part}")
            parts.append(part)
            continue

        print(f"\n[seg {idx + 1}/{len(SEGMENTS)}] {kind}: {narration[:30]}……")
        seg_dir = os.path.join(_WORK, f"seg{idx}")
        # 分段目录必须干净：旧帧残留会让 image2 通配符把废帧也编进视频
        shutil.rmtree(seg_dir, ignore_errors=True)
        os.makedirs(seg_dir, exist_ok=True)

        dur, audio = _tts(narration, os.path.join(seg_dir, "voice.mp3"))
        print(f"[seg] 旁白 {dur:.1f}s")

        if kind == "card":
            if idx == 0:
                lines = [("N O L A N", "big", (240, 240, 240)),
                         ("住在你电脑里的 AI 管家", "small", (150, 155, 165))]
            else:
                lines = [("N O L A N", "big", (240, 240, 240)),
                         ("github.com/xcq20100224/Nolan", "small", (150, 155, 165))]
            _card_frames(lines, dur + 0.5, seg_dir)
        else:
            actual = _record_screen(max(min_sec, dur + 1.5), command, grace, seg_dir)
            print(f"[seg] 录屏 {actual:.0f}s")

        _mux(idx, seg_dir, audio, part)
        parts.append(part)
        print(f"[seg] 合成完成：{part}")

    # 拼接所有分段
    lst = os.path.join(_WORK, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.replace(os.sep, '/')}'\n")
    out = os.path.join(_OUT_DIR, "nolan-demo.mp4")
    subprocess.run([
        _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", lst,
        "-c", "copy", "-movflags", "+faststart", out,
    ], check=True, capture_output=True)

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"\n[done] {out}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
