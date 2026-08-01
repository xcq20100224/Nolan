# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 网页版后端（server.py）
职责：用 Python 标准库 http.server 暴露 JSON API，供 React+Vite 前端调用。

第一性原理：不引任何后端框架——标准库 ThreadingHTTPServer 足够；
不做用户系统、不做数据库、不做静态文件服务（前端由 vite dev server 提供，
vite 把 /api 代理到本服务）。

端口：sys.argv[1]，默认 7101。

单实例守卫（最高优先级）：
    根因——Kimi 预览反复拉起新后端而旧后端不死，Windows 默认允许同端口双绑定，
    请求被新旧进程随机瓜分，声音/功能时好时坏。对策三件套：
    1) 启动时先读 pidfile（jarvis/files/server.pid），若记录的 PID 仍存活且不是自己，
       用 taskkill /F /PID 清理旧进程并等待 1 秒让端口释放（权限不足只告警继续）；
    2) listen socket 设置 SO_EXCLUSIVEADDRUSE（Windows 独占绑定，从机制上杜绝双绑定）；
    3) 绑定成功后写入当前 PID，进程退出时清理 pidfile（只删自己写的那份）。

API 契约（一字不差）：
    GET  /api/health     → {"ok": true, "name": "Nolan"}
    GET  /api/version    → {"version": "2026-07-23-smartgui", "pid": 整数}
                         （后端代码版本标识 + 当前进程 PID；
                         用于验证运行中的后端是否为最新代码，排查陈旧进程）
    POST /api/chat       请求 {"text": "..."} → 响应 {"reply": str, "audio_url": str|null}
                         若 brain 返回 '__EXIT__' → {"reply": "道别语", "audio_url": str|null, "exit": true}
                         （audio_url 为 TTS 合成音频的 URL，合成失败为 null）
    GET  /api/due        → {"messages": [{"text": str, "audio_url": str|null}]}（到点提醒，无则 []）
                         到点消息服务端音箱连续播报两遍（闹钟式，间隔 0.5 秒）
    GET  /api/sound_test → {"audio_url": str|null, "speaker": true}
                         声音必达自检：固定文本走 synth_for 链合成（浏览器通道），
                         同时后台线程 mouth.speak 从音箱播报同一句话（音箱通道）
    GET  /api/tts/<sha1>.mp3|wav → audio/mpeg|audio/wav（TTS 缓存目录 jarvis/files/tts_cache/，
                         只允许纯文件名，路径穿越一律 404）
    GET  /api/reminders  → {"text": "口语化提醒列表"}
    GET  /api/memory     → {"text": "口语化记忆列表"}
    GET  /api/background → {"image_url": str|null}（聊天网页背景；
                         状态文件 jarvis/files/web_background.json 内容为 {"image": "文件名"}，
                         存在则返回 "/api/files/<文件名>"，无状态文件/内容无效返回 null）
    GET  /api/files/<文件名> → image/jpeg|png|webp（图片目录 jarvis/files/，
                         只允许安全相对路径，路径穿越一律 404）
    POST /api/asr        请求体为原始音频字节（audio/webm|ogg|wav）
                         → {"text": "识别文本"}；无语音 {"text": ""}
    POST /api/mic/start  → {"ok": true}（服务端直接开麦录音，绕开浏览器权限）
    POST /api/mic/stop   → {"text": "..."}（停止并识别；无语音/未录音 {"text": ""}）
全部 JSON UTF-8（ensure_ascii=False）、CORS 允许 *、处理 OPTIONS 预检；
异常返回 500 + {"error": 中文说明}；未知路径 404。

运行：python server.py [端口]
"""

import asyncio
import atexit
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# == 把 ../jarvis 加入 sys.path（用 __file__ 定位，与启动目录无关）==
_JARVIS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jarvis")
_JARVIS_DIR = os.path.normpath(_JARVIS_DIR)
if _JARVIS_DIR not in sys.path:
    sys.path.insert(0, _JARVIS_DIR)

import brain       # noqa: E402  大脑：think(user_text, history) -> str
import reminders   # noqa: E402  提醒：check_due / list_pending / add
import memory      # noqa: E402  记忆：recall / load / remember / forget

# == 后端代码版本标识 ==
# 用途：曾出现『GUI 失败源于陈旧后端进程（旧代码仍在内存中运行）』的问题，
# 仅靠单实例守卫清理旧进程还不够直观——需要让『当前跑的是不是新代码』一眼可验。
# GET /api/version 返回本常量与当前进程 PID；改代码后务必同步更新本常量。
_VERSION = "2026-08-01-stage1"

# mouth 惰性导入且失败降级为 None（GLM-TTS 主通道 + edge-tts 备用 + SAPI 离线兜底，
# 网页版后端不能让播报失败拖垮 API）
try:
    import mouth
except Exception as e:
    print(f"[server] mouth 导入失败，播报降级为静默：{e}")
    mouth = None

# == 单实例守卫（最高优先级：根治旧进程双绑定）==
# 机制：pidfile 记录当前后端 PID；启动时先清理存活旧实例，再独占绑定端口。
_PID_FILE = os.path.normpath(os.path.join(_JARVIS_DIR, "files", "server.pid"))

# == 网页背景与图片文件服务 ==
# set_web_background 工具写状态文件 web_background.json（{"image": "<相对 files 的路径>"}），
# 前端轮询 /api/background 拿到图片 URL 后应用为聊天背景。
_FILES_DIR = os.path.normpath(os.path.join(_JARVIS_DIR, "files"))
_WEB_BG_FILE = os.path.join(_FILES_DIR, "web_background.json")
# 允许的图片后缀 → MIME（其余后缀一律 404，不泄露任意文件）
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _pid_alive(pid: int) -> bool:
    """
    用 tasklist 探测 PID 是否仍存活。
    第一性原理：不猜不死等——tasklist 是 Windows 自带、无新依赖、结果确定。
    注意绝不能用 os.kill(pid, 0)：Windows 上 os.kill 任意信号都会终止进程。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        # CSV 输出形如 "python.exe","1234","Console",...；命中引号包裹的 PID 即存活
        return f'"{pid}"' in (r.stdout or "")
    except Exception as e:
        print(f"[server] 警告：无法探测进程 {pid} 是否存活（按已退出处理）：{e}")
        return False


def _kill_pid(pid: int) -> bool:
    """taskkill /F /PID 强杀指定进程；失败只打印警告返回 False，绝不抛异常。"""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print(f"[server] 旧后端进程 PID={pid} 已清理。")
            return True
        detail = (r.stdout or r.stderr or "").strip()
        print(f"[server] 警告：清理旧进程 PID={pid} 失败（可能权限不足，继续启动）：{detail}")
        return False
    except Exception as e:
        print(f"[server] 警告：taskkill 调用异常（继续启动）：{e}")
        return False


def _single_instance_guard() -> None:
    """
    启动前清理旧实例：读 pidfile，若记录的 PID 仍存活且不是自己，则强杀并等待 1 秒。
    失效 pidfile（进程已退出）是常态（上次异常退出/被 taskkill），静默继续即可。
    """
    try:
        with open(_PID_FILE, "r", encoding="utf-8") as f:
            old_pid = int(f.read().strip())
    except FileNotFoundError:
        return  # 没有 pidfile：首次启动，无需清理
    except (ValueError, OSError) as e:
        print(f"[server] pidfile 内容无效（忽略，按正常启动处理）：{e}")
        return
    if old_pid == os.getpid():
        return  # 理论上不会发生，防御性跳过
    if _pid_alive(old_pid):
        print(f"[server] 检测到旧后端进程 PID={old_pid} 仍存活，正在清理……")
        if _kill_pid(old_pid):
            time.sleep(1)  # 等旧进程完全退出、端口彻底释放
        else:
            print("[server] 警告：旧进程可能未被清理，若端口绑定失败请手动结束后端进程。")
    else:
        print(f"[server] 发现失效 pidfile（PID={old_pid} 已退出），按正常启动处理。")


def _write_pidfile() -> None:
    """绑定成功后把当前 PID 写入 pidfile。"""
    os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)
    with open(_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _cleanup_pidfile() -> None:
    """退出时清理 pidfile；只删自己写的那份，避免误删新实例的记录。"""
    try:
        if os.path.isfile(_PID_FILE):
            with open(_PID_FILE, "r", encoding="utf-8") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(_PID_FILE)
    except OSError:
        pass  # 清理失败无害：下次启动按失效 pidfile 处理


def _read_background_url():
    """
    读网页背景状态文件，返回图片 URL（形如 '/api/files/<路径>'）；
    无状态文件、内容无效或 image 为空一律返回 None（契约：image_url 可为 null）。
    """
    try:
        with open(_WEB_BG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        image = (data.get("image") or "").strip() if isinstance(data, dict) else ""
        if image:
            return "/api/files/" + image
    except FileNotFoundError:
        pass  # 尚未设置过背景，正常情况
    except Exception as e:
        print(f"[server] 读取 web_background.json 失败（按无背景处理）：{e}")
    return None


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Windows 独占绑定的 HTTP 服务：SO_EXCLUSIVEADDRUSE 从机制上杜绝同端口双绑定。"""

    # 关键：SO_EXCLUSIVEADDRUSE 与 SO_REUSEADDR 在 Winsock 互斥（同设报 WSAEINVAL 10022）。
    # 独占绑定语义已覆盖『快速重用端口』需求，故关掉基类的 allow_reuse_address。
    allow_reuse_address = False

    def server_bind(self):
        try:
            # 必须在 bind() 之前设置；独占绑定后第二个进程 bind 同端口必失败
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError as e:
            # 非 Windows 平台或设置失败：只告警，不阻断启动
            print(f"[server] 警告：SO_EXCLUSIVEADDRUSE 设置失败（继续普通绑定）：{e}")
        super().server_bind()

# == TTS 缓存（第一性原理：同一句话只合成一次，sha1(文本) 即文件名）==
# 网页端 <audio> 不能直接调用 TTS，服务端把合成结果落成文件缓存，
# 响应里只带 URL，浏览器再按 URL 取音频播放。
# 发声链（与 mouth.py 同一顺序，声音必达）：
#   1) GLM-TTS 主通道（智谱 glm-tts，wav，与大脑同一把 API Key）
#   2) edge-tts 备用（微软在线，mp3）
#   3) SAPI 离线兜底（pyttsx3，wav）
# 缓存命中三种后缀产物任一即复用。
_TTS_VOICE = "zh-CN-YunjianNeural"   # edge-tts 备用通道音色，与 mouth 保持一致
_TTS_CACHE_DIR = os.path.normpath(os.path.join(_JARVIS_DIR, "files", "tts_cache"))
_tts_lock = threading.Lock()         # 串行化合成，避免并发重复合成同一文本

# 声音自检固定文本（/api/sound_test 专用，一字不差）
_SOUND_TEST_TEXT = "先生，我是 Nolan，我的声音已经就绪。"


def _glm_tts_config():
    """读取 jarvis/llm_config.json 的 api_key/base_url；读不到返回 (None, None)。"""
    cfg_path = os.path.join(_JARVIS_DIR, "llm_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("api_key"), (cfg.get("base_url") or "").rstrip("/")
    except Exception as e:
        print(f"[server] 读取 llm_config.json 失败，GLM-TTS 主通道不可用：{e}")
        return None, None


def _glm_tts_to_file(text: str, path: str) -> bool:
    """
    GLM-TTS 主通道：POST {base_url}/audio/speech，把返回的 wav 字节写入 path。
    与大脑同一把 API Key（jarvis/llm_config.json）。成功返回 True，任何失败返回 False。
    """
    api_key, base_url = _glm_tts_config()
    if not api_key or not base_url:
        return False
    try:
        import httpx  # 既有依赖，不加新 pip
        resp = httpx.post(
            base_url + "/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "glm-tts", "input": text, "voice": "male",
                  "response_format": "wav"},
            timeout=12,  # 硬上界：主通道最多 12 秒，超时即放弃换备用通道
        )
        if resp.status_code == 200 and resp.content:
            with open(path, "wb") as f:
                f.write(resp.content)
            return os.path.getsize(path) > 0
        print(f"[server] GLM-TTS 返回异常：status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        print(f"[server] GLM-TTS 合成失败：{e}")
    return False


def _tts_cached_url(text: str):
    """缓存命中查询：sha1(文本) 对应的 .wav/.mp3 任一存在即返回 URL，否则 None（毫秒级，不联网）。"""
    text = (text or "").strip()
    if not text:
        return None
    base = hashlib.sha1(text.encode("utf-8")).hexdigest()
    for ext in (".wav", ".mp3"):
        p = os.path.join(_TTS_CACHE_DIR, base + ext)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return "/api/tts/" + base + ext
        except OSError:
            pass
    return None


def _run_with_deadline(fn, timeout: float):
    """
    在守护线程里执行 fn()，最多等 timeout 秒。
    按时完成返回 fn 的返回值；超时返回 None（线程可能残留，但绝不阻塞调用方）。
    这是修复『TTS 合成偶发挂死拖垮整个后端』的关键：发声链任何一步都有明确上界——
    此前 edge-tts 无超时（aiohttp 默认 300 秒）、SAPI 在工作线程偶发挂死，
    它们攥着合成锁不放，会把 /api/chat、/api/due 全部拖死，进而占满浏览器连接池。
    """
    box = {}
    done = threading.Event()

    def _run():
        try:
            box["result"] = fn()
        except Exception as e:
            print(f"[server] TTS 通道执行异常：{e}")
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    if done.wait(timeout):
        return box.get("result")
    return None


def _warm_tts_async(text: str) -> None:
    """后台守护线程暖缓存：为 text 预先合成音频，同文本下次直接命中。"""
    if not (text or "").strip():
        return
    threading.Thread(target=lambda: synth_for(text), daemon=True).start()


def synth_for(text: str):
    """
    把 text 合成为音频存入缓存目录，返回音频 URL（形如 '/api/tts/<sha1>.wav|.mp3'）；
    缓存命中直接复用；全部通道失败返回 None。

    发声链顺序（与 mouth.py 一致）：GLM-TTS 主通道（wav，12s 上界）→ edge-tts 备用
    （mp3，18s 上界）→ SAPI 离线兜底（wav，12s 上界）。每一步都有硬性时间上界，
    任何通道挂死都只表现为『该通道放弃』，绝不阻塞请求、绝不拖垮 API。
    """
    text = (text or "").strip()
    if not text:
        return None
    base = hashlib.sha1(text.encode("utf-8")).hexdigest()
    wav_name = base + ".wav"
    mp3_name = base + ".mp3"
    wav_path = os.path.join(_TTS_CACHE_DIR, wav_name)
    mp3_path = os.path.join(_TTS_CACHE_DIR, mp3_name)

    try:
        os.makedirs(_TTS_CACHE_DIR, exist_ok=True)
    except OSError as e:
        print(f"[server] TTS 缓存目录创建失败：{e}")
        return None

    # 锁外先查一次缓存，命中不联网
    hit = _tts_cached_url(text)
    if hit:
        return hit

    with _tts_lock:
        # 持锁后再查一次：并发下别的线程可能已合成好
        hit = _tts_cached_url(text)
        if hit:
            return hit

        # 通道一：GLM-TTS 主通道（智谱 glm-tts，wav；httpx 自带 12s 超时，有界）
        if _glm_tts_to_file(text, wav_path):
            return "/api/tts/" + wav_name

        # 通道二：edge-tts 备用（mp3，单次尝试 18s 硬上界；
        # 不重试同一路径——超时线程可能仍在后台写该文件，并发双写会损坏缓存）
        def _edge_once():
            import edge_tts

            async def _synth():
                communicate = edge_tts.Communicate(text, _TTS_VOICE)
                await communicate.save(mp3_path)

            asyncio.run(_synth())
            return os.path.isfile(mp3_path) and os.path.getsize(mp3_path) > 0

        if _run_with_deadline(_edge_once, 18):
            return "/api/tts/" + mp3_name
        print("[server] edge-tts 未在 18 秒内完成，放弃本通道（残留线程写完即成正常缓存，无害）。")
        # 清理空壳半成品，避免下次误判为缓存命中
        try:
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) == 0:
                os.remove(mp3_path)
        except OSError:
            pass

        # 通道三：SAPI 离线兜底（wav，12s 硬上界）——
        # 第一性原理：浏览器必须必定发声，音色可降级，声音不能缺席；
        # 但 SAPI 在工作线程里偶发挂死，必须沙盒化，宁可放弃也不阻塞。
        def _sapi_once():
            import pyttsx3

            engine = pyttsx3.init()
            try:
                engine.save_to_file(text, wav_path)
                engine.runAndWait()
            finally:
                try:
                    engine.stop()
                except Exception:
                    pass
            return os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0

        if _run_with_deadline(_sapi_once, 12):
            print("[server] 在线通道均不可用，浏览器音频已降级为离线语音（wav）")
            return "/api/tts/" + wav_name
        return None

# == 语音识别（faster-whisper 懒加载单例）==
# 第一性原理：模型只在真正要用时才加载（冷启动不拖慢 API 就绪）；
# 但启动后立刻在后台守护线程里预加载，避免先生第一次录音等 5 秒。
# CPU + int8 的 small 模型已本地缓存，无需联网。
_whisper_model = None            # faster_whisper.WhisperModel 单例
_whisper_lock = threading.Lock() # 保护模型加载与 transcribe 串行化
_whisper_error = None            # 预加载失败的中文说明（懒加载时重试）


def _load_whisper() -> None:
    """真正加载模型；加锁保证全局只加载一次。"""
    global _whisper_model, _whisper_error
    with _whisper_lock:
        if _whisper_model is not None:
            return
        try:
            from faster_whisper import WhisperModel
            print("[server] 正在加载 faster-whisper medium 模型（CPU+int8，中文识别精度升级）……")
            _whisper_model = WhisperModel("medium", device="cpu", compute_type="int8")
            _whisper_error = None
            print("[server] faster-whisper 模型加载完成，语音输入就绪。")
        except Exception as e:
            _whisper_error = f"语音模型加载失败：{e}"
            print(f"[server] {_whisper_error}")


def _preload_whisper() -> None:
    """在后台守护线程里预加载模型，不阻塞服务启动。"""
    t = threading.Thread(target=_load_whisper, daemon=True, name="whisper-preload")
    t.start()


# Content-Type → 临时文件后缀；不认识的按 .webm 处理（浏览器 MediaRecorder 默认）
_EXT_BY_CONTENT_TYPE = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
}


def _transcribe_bytes(audio: bytes, content_type: str) -> str:
    """
    把原始音频字节写入临时文件，用 faster-whisper 识别为简体中文文本。
    空音频返回空串；无有效语音（VAD 过滤后无片段）返回空串。
    识别异常抛 RuntimeError，由调用方转成 500。
    """
    # 空音频直接返回，不碰模型
    if not audio:
        return ""

    _load_whisper()  # 懒加载兜底（预加载未完成时同步等待）
    if _whisper_model is None:
        raise RuntimeError(_whisper_error or "语音模型不可用。")

    ext = _EXT_BY_CONTENT_TYPE.get((content_type or "").split(";")[0].strip().lower(), ".webm")
    tmp_path = None
    try:
        # 写入系统临时文件（faster-whisper 走 PyAV 18 按路径解码，webm/opus 无需 ffmpeg）
        fd, tmp_path = tempfile.mkstemp(prefix="nolan_asr_", suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(audio)

        with _whisper_lock:
            segments, _info = _whisper_model.transcribe(
                tmp_path,
                language="zh",
                initial_prompt="以下是主人对中文语音助手 Nolan 说的普通话指令。",
                beam_size=5,
                vad_filter=True,  # 过滤静音段，无语音时自然得到空结果
            )
            text = "".join(seg.text for seg in segments).strip()
        return text
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"语音识别失败了：{e}")
    finally:
        # 全程保证删掉临时文件
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

# == 服务端麦克风录音（第一性原理：麦克风属于机器，不属于浏览器）==
# 浏览器 webview 的麦克风权限反复受阻，干脆由 server.py 用 sounddevice 直接
# 录音，前端只当遥控器发 start/stop。复用上方 faster-whisper 单例做识别。
_MIC_SAMPLERATE = 16000          # 采样率，与 faster-whisper 期望一致
_MIC_MAX_SECONDS = 60            # 安全上限：超过 60 秒只取前 60 秒识别

_mic_stream = None               # sounddevice.InputStream 实例
_mic_frames = []                 # 回调攒下的音频帧（float32 numpy 数组）
_mic_lock = threading.Lock()     # 保护录音状态，防止 start/stop 并发打架
_mic_recording = False           # 是否正在录音
_mic_started_at = 0.0            # 本次录音开始的时间戳（time.time）


def _mic_callback(indata, frames, time_info, status):
    """sounddevice 回调：每来一帧就复制一份攒进 _mic_frames。"""
    if status:
        print(f"[server] 录音回调状态提示：{status}")
    _mic_frames.append(indata.copy())


def _mic_stop_locked() -> None:
    """在持锁状态下关掉当前流（若有）。假定调用方已持有 _mic_lock。"""
    global _mic_stream, _mic_recording
    if _mic_stream is not None:
        try:
            _mic_stream.stop()
            _mic_stream.close()
        except Exception as e:
            print(f"[server] 关闭录音流出错（已忽略）：{e}")
        _mic_stream = None
    _mic_recording = False


# == 麦克风事件诊断日志（写文件，便于排查"点击没反应"时请求是否到达后端）==
_MIC_DEBUG_FILE = os.path.normpath(os.path.join(_JARVIS_DIR, "files", "mic_debug.log"))


def _mic_debug_log(msg: str) -> None:
    """把麦克风端点事件追加到诊断日志文件；任何写失败静默忽略。"""
    try:
        os.makedirs(os.path.dirname(_MIC_DEBUG_FILE), exist_ok=True)
        with open(_MIC_DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


# == 前端黑匣子（关键诊断设施）==
# 已证实：浏览器端 fetch 的『响应丢失』只在用户的内嵌 webview 里发生，
# curl/node 全部正常。/api/clientlog 是 fire-and-forget 上报通道——
# 前端把每一步痕迹发过来（只依赖『请求能到达』，不依赖响应），
# 落盘到 client_debug.log，下次卡死可精确还原断在哪一步。
_CLIENT_DEBUG_FILE = os.path.normpath(os.path.join(_JARVIS_DIR, "files", "client_debug.log"))


def _client_debug_log(msg: str) -> None:
    """把前端上报的诊断事件追加到黑匣子日志；去换行防注入，截断防膨胀。"""
    msg = (msg or "").replace("\r", " ").replace("\n", " ")[:300]
    try:
        os.makedirs(os.path.dirname(_CLIENT_DEBUG_FILE), exist_ok=True)
        with open(_CLIENT_DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _mic_start() -> None:
    """
    开始服务端录音。已在录音则先停掉重来。
    麦克风被占用等异常抛 RuntimeError（中文说明），由路由转成 500。
    """
    global _mic_stream, _mic_frames, _mic_recording, _mic_started_at
    with _mic_lock:
        if _mic_recording:
            # 已在录音：先停掉重来，保证一次 start 对应一段干净音频
            _mic_stop_locked()
        _mic_frames = []
        try:
            import sounddevice as sd
            stream = sd.InputStream(
                samplerate=_MIC_SAMPLERATE,
                channels=1,
                dtype="float32",
                callback=_mic_callback,
            )
            stream.start()
        except Exception as e:
            _mic_stream = None
            _mic_recording = False
            raise RuntimeError(f"先生，麦克风打开失败了（可能被其他程序占用）：{e}")
        _mic_stream = stream
        _mic_recording = True
        _mic_started_at = time.time()
        print("[server] 服务端麦克风开始录音。")


def _mic_stop() -> str:
    """
    停止录音并识别。未在录音或没有有效语音返回空串。
    超过 60 秒只取前 60 秒。识别异常抛 RuntimeError，由路由转成 500。
    """
    global _mic_frames
    with _mic_lock:
        if not _mic_recording:
            # 未在录音时调用：契约要求返回空文本而不是报错
            return ""
        _mic_stop_locked()
        frames = _mic_frames
        _mic_frames = []

    if not frames:
        _mic_debug_log("mic/stop 无音频帧（回调未采到声音，麦克风可能被独占）")
        return ""

    import numpy as np
    audio = np.concatenate(frames, axis=0).flatten()
    _mic_debug_log(f"mic/stop 采到 {len(frames)} 帧，共 {audio.shape[0] / _MIC_SAMPLERATE:.1f} 秒音频")
    # 安全上限：只取前 60 秒音频（正常识别，不报错）
    max_samples = _MIC_MAX_SECONDS * _MIC_SAMPLERATE
    if audio.shape[0] > max_samples:
        audio = audio[:max_samples]
    if audio.shape[0] == 0:
        return ""

    # 峰值归一化：笔记本阵列麦克风录音偏小（实测峰值约 0.09），
    # 拉到 0.9 峰值再识别，避免模型因音量过低而幻听
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = audio * (0.9 / peak)

    _load_whisper()  # 懒加载兜底（预加载未完成时同步等待）
    if _whisper_model is None:
        raise RuntimeError(_whisper_error or "语音模型不可用。")

    try:
        with _whisper_lock:
            # float32 单声道 16kHz 的 numpy 数组可直接喂给 faster-whisper，无需临时文件
            segments, _info = _whisper_model.transcribe(
                audio,
                language="zh",
                initial_prompt="以下是主人对中文语音助手 Nolan 说的普通话指令。",
                beam_size=5,
                vad_filter=True,  # 过滤静音段，无语音时自然得到空结果
            )
            text = "".join(seg.text for seg in segments).strip()
        return text
    except Exception as e:
        raise RuntimeError(f"语音识别失败了：{e}")


# == 全局状态 ==
_brain_lock = threading.Lock()   # brain 调用与 mouth 播报共用同一把锁串行化
_history = []                    # 服务端维护的对话历史，裁剪到 20 轮（40 条）
_HISTORY_MAX_TURNS = 20


def _speak_async(text: str) -> None:
    """在后台守护线程里用同一把 Lock 串行播报；mouth 为 None 静默跳过。"""
    if mouth is None or not text:
        return

    def _worker():
        try:
            with _brain_lock:
                mouth.speak(text)
        except Exception as e:
            print(f"[server] 播报失败（已静默）：{e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _speak_alarm_async(text: str) -> None:
    """
    闹钟式播报：到点提醒在服务端音箱连续播报两遍（中间间隔 0.5 秒）。
    第一性原理：闹钟的价值在于『必被听见』，一遍可能错过，两遍才可靠。
    后台守护线程执行，不阻塞 /api/due 响应；mouth 为 None 静默跳过。
    """
    if mouth is None or not text:
        return

    def _worker():
        try:
            with _brain_lock:
                print(f"[server] 闹钟播报（连续两遍）：{text}")
                mouth.speak(text)
                time.sleep(0.5)
                mouth.speak(text)
        except Exception as e:
            print(f"[server] 闹钟播报失败（已静默）：{e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _chat(user_text: str) -> dict:
    """
    处理一轮对话：串行调用 brain.think，维护 history（裁剪 20 轮）。
    返回 {"reply": str, "audio_url": str|null}，exit 时附加 "exit": True。
    audio_url 为 TTS 发声链合成的回复语音（缓存复用），合成失败为 None。
    非 exit 回复在后台线程播报。
    """
    global _history
    user_text = (user_text or "").strip()
    if not user_text:
        reply = "先生，您似乎还没有说话，请输入内容后再发送。"
        return {"reply": reply, "audio_url": synth_for(reply)}

    with _brain_lock:
        reply = brain.think(user_text, list(_history))
        # 历史写入与裁剪也在同一临界区内：与 brain.think 串行化，
        # 杜绝并发请求把对话历史交错、裁剪互相覆盖
        if reply != "__EXIT__":
            _history.append({"role": "user", "content": user_text})
            _history.append({"role": "assistant", "content": reply})
            if len(_history) > _HISTORY_MAX_TURNS * 2:
                _history = _history[-_HISTORY_MAX_TURNS * 2:]

    if reply == "__EXIT__":
        farewell = "好的先生，我先去休息了，随时叫我的名字就能唤醒我。"
        _speak_async(farewell)
        return {"reply": farewell, "audio_url": synth_for(farewell), "exit": True}

    _speak_async(reply)
    return {"reply": reply, "audio_url": synth_for(reply)}


# == HTTP 处理器 ==

class NolanHandler(BaseHTTPRequestHandler):
    """Nolan JSON API 请求处理器。"""

    server_version = "NolanWeb/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础输出工具 --

    def _send_json(self, status: int, payload: dict) -> None:
        """发送 JSON UTF-8 响应（ensure_ascii=False），带 CORS 头。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开，静默忽略

    def _send_error_json(self, status: int, message: str) -> None:
        """统一的 JSON 错误响应。"""
        self._send_json(status, {"error": message})

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        """发送二进制响应（用于 mp3 音频），带 CORS 头。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开，静默忽略

    def _serve_tts(self, raw_filename: str) -> None:
        """
        从 TTS 缓存目录返回 mp3（audio/mpeg）。
        防路径穿越：URL 解码后只允许纯文件名（不含任何路径分隔符与 '..'），
        且解析后的绝对路径必须落在缓存目录内；不合规一律 404。
        """
        name = unquote(raw_filename)
        if (not name or name != os.path.basename(name) or ".." in name
                or not name.endswith((".mp3", ".wav"))):
            self._send_error_json(404, f"未知路径：/api/tts/{raw_filename}")
            return
        full = os.path.join(_TTS_CACHE_DIR, name)
        # 双重保险：解析后的绝对路径必须仍在缓存目录内
        if os.path.dirname(os.path.abspath(full)) != os.path.abspath(_TTS_CACHE_DIR):
            self._send_error_json(404, f"未知路径：/api/tts/{raw_filename}")
            return
        if not os.path.isfile(full):
            self._send_error_json(404, f"音频不存在：{name}")
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_error_json(500, f"读取音频失败了：{e}")
            return
        mime = "audio/wav" if name.endswith(".wav") else "audio/mpeg"
        self._send_bytes(200, data, mime)

    def _serve_file(self, raw_name: str) -> None:
        """
        从 jarvis/files/ 目录返回图片（image/jpeg|png|webp）。
        防路径穿越：拒绝 '..'、绝对路径、盘符（':'）；解析后的绝对路径必须落在
        files 目录内；只允许白名单图片后缀。不合规一律 404，绝不泄露任意文件。
        """
        name = unquote(raw_name)
        if (not name or ".." in name or os.path.isabs(name) or ":" in name):
            self._send_error_json(404, f"未知路径：/api/files/{raw_name}")
            return
        full = os.path.normpath(os.path.join(_FILES_DIR, name))
        try:
            # 双重保险：解析后的绝对路径必须仍在 files 目录内（含子目录）
            inside = os.path.commonpath(
                [os.path.abspath(full), os.path.abspath(_FILES_DIR)]
            ) == os.path.abspath(_FILES_DIR)
        except ValueError:
            inside = False  # 不同盘符等异常，一律视为不合法
        if not inside:
            self._send_error_json(404, f"未知路径：/api/files/{raw_name}")
            return
        mime = _IMAGE_MIME.get(os.path.splitext(name)[1].lower())
        if mime is None:
            self._send_error_json(404, f"不支持的图片类型：{name}")
            return
        if not os.path.isfile(full):
            self._send_error_json(404, f"图片不存在：{name}")
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_error_json(500, f"读取图片失败了：{e}")
            return
        self._send_bytes(200, data, mime)

    def _read_json_body(self) -> dict:
        """读取并解析请求体 JSON；失败抛 ValueError。"""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("请求体不是合法的 JSON。")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return data

    def _discard_body(self) -> None:
        """
        读取并丢弃请求体（上限 1MB）。
        有的 POST 端点不需要请求体，但客户端仍可能带 body（如 mic/stop 带 '{}'）：
        不读干净会让残留字节污染 keep-alive 连接上的下一个请求，造成协议错位。
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        remaining = max(0, min(length, 1024 * 1024))
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    # -- 静默访问日志（保持控制台干净，可按需打开）--
    def log_message(self, fmt, *args):
        pass

    # -- 方法分发 --

    def do_OPTIONS(self):
        """CORS 预检。"""
        self._send_json(200, {"ok": True})

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._send_json(200, {"ok": True, "name": "Nolan"})
            elif path == "/api/mic/start":
                # GET 变体：内嵌 webview 对 POST 响应有兼容问题时改走 GET（幂等可重开）
                _mic_debug_log("mic/start(GET) 收到请求")
                try:
                    _mic_start()
                except RuntimeError as e:
                    _mic_debug_log(f"mic/start(GET) 失败: {e}")
                    self._send_error_json(500, str(e))
                    return
                _mic_debug_log("mic/start(GET) 已开始录音")
                self._send_json(200, {"ok": True})
            elif path == "/api/mic/state":
                # 录音状态查询（关键修复）：前端发起 start 后轮询本端点确认服务端
                # 真实录音状态，不再依赖单次 start 请求的响应——响应丢失也能自愈
                with _mic_lock:
                    rec = _mic_recording
                self._send_json(200, {"recording": rec})
            elif path == "/api/clientlog":
                # 前端黑匣子上报：fire-and-forget，只负责落痕（GET 简单请求，无预检）
                msg = parse_qs(urlparse(self.path).query).get("m", [""])[0]
                _client_debug_log(msg)
                self._send_json(200, {"ok": True})
            elif path == "/api/version":
                # 版本端点：让『当前后端跑的是不是最新代码』一眼可验
                # （陈旧进程返回旧版本号，PID 可对照任务管理器核对）
                self._send_json(200, {"version": _VERSION, "pid": os.getpid()})
            elif path == "/api/due":
                messages = reminders.check_due() or []
                out = []
                for msg in messages:
                    # 音频异步化（关键修复）：响应只带缓存命中（毫秒级，通常首轮为 None），
                    # 未命中交给后台线程合成暖缓存——此前在这里同步 synth_for，
                    # TTS 一慢/一挂，15 秒轮询就挂住，占满浏览器连接池，
                    # 连累麦克风等所有请求排队卡死。闹钟声音由音箱通道保证必达。
                    out.append({"text": msg, "audio_url": _tts_cached_url(msg)})
                    _warm_tts_async(msg)
                    # 闹钟必响：服务端音箱连续播报两遍
                    _speak_alarm_async(msg)
                self._send_json(200, {"messages": out})
            elif path == "/api/sound_test":
                # 声音必达自检：浏览器通道（synth_for 合成返回 audio_url）与
                # 音箱通道（后台线程 mouth.speak 播报同一句话）同时发声
                audio_url = synth_for(_SOUND_TEST_TEXT)
                _speak_async(_SOUND_TEST_TEXT)
                self._send_json(200, {"audio_url": audio_url, "speaker": True})
            elif path.startswith("/api/tts/"):
                self._serve_tts(path[len("/api/tts/"):])
            elif path == "/api/reminders":
                self._send_json(200, {"text": reminders.list_pending()})
            elif path == "/api/memory":
                self._send_json(200, {"text": memory.recall()})
            elif path == "/api/background":
                # 网页背景：无状态文件/内容无效时 image_url 为 None
                self._send_json(200, {"image_url": _read_background_url()})
            elif path.startswith("/api/files/"):
                self._serve_file(path[len("/api/files/"):])
            else:
                self._send_error_json(404, f"未知路径：{path}")
        except Exception as e:
            self._send_error_json(500, f"服务器处理请求时出错了：{e}")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/chat":
                try:
                    data = self._read_json_body()
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                result = _chat(str(data.get("text", "") or ""))
                self._send_json(200, result)
            elif path == "/api/asr":
                # 请求体为原始音频字节（audio/webm|ogg|wav，统统接受）
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    length = 0
                audio = self.rfile.read(length) if length > 0 else b""
                content_type = self.headers.get("Content-Type", "") or ""
                try:
                    text = _transcribe_bytes(audio, content_type)
                except RuntimeError as e:
                    self._send_error_json(500, str(e))
                    return
                self._send_json(200, {"text": text})
            elif path == "/api/mic/start":
                # 服务端直接开麦录音，绕开浏览器麦克风权限
                _mic_debug_log("mic/start 收到请求")
                self._discard_body()  # 读净请求体，防止残留字节污染 keep-alive 连接
                try:
                    _mic_start()
                except RuntimeError as e:
                    _mic_debug_log(f"mic/start 失败: {e}")
                    self._send_error_json(500, str(e))
                    return
                _mic_debug_log("mic/start 已开始录音")
                self._send_json(200, {"ok": True})
            elif path == "/api/mic/stop":
                # 停止录音并识别；未在录音/无语音返回 {"text": ""}
                _mic_debug_log("mic/stop 收到请求")
                self._discard_body()  # 读净请求体（前端会带 '{}'），防止污染 keep-alive 连接
                try:
                    text = _mic_stop()
                except RuntimeError as e:
                    _mic_debug_log(f"mic/stop 失败: {e}")
                    self._send_error_json(500, str(e))
                    return
                _mic_debug_log(f"mic/stop 识别结果: {text!r}")
                self._send_json(200, {"text": text})
            else:
                self._send_error_json(404, f"未知路径：{path}")
        except Exception as e:
            self._send_error_json(500, f"服务器处理请求时出错了：{e}")


def main() -> None:
    port = 7101
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"[server] 端口参数无效：{sys.argv[1]}，使用默认 7101")

    _single_instance_guard()  # 最高优先级：先清理存活旧实例，再绑定端口

    try:
        server = ExclusiveHTTPServer(("127.0.0.1", port), NolanHandler)
    except OSError as e:
        # 独占绑定后仍失败：说明端口被未登记在 pidfile 的进程占用
        print(f"[server] 端口 {port} 绑定失败（可能被其他程序占用）：{e}")
        print("[server] 先生，后端启动受阻，请检查是否有残留的后端进程。")
        sys.exit(1)
    server.daemon_threads = True

    # 绑定成功才登记自己：写 pidfile + 注册退出清理（atexit 兜底正常退出路径）
    _write_pidfile()
    atexit.register(_cleanup_pidfile)

    _preload_whisper()  # 后台预加载语音模型，避免第一次录音等待
    print(f"[server] Nolan 网页版后端已启动：http://127.0.0.1:{port}（PID={os.getpid()}，端口独占）")
    print(f"[server] 语音播报：{'启用' if mouth is not None else '静默（mouth 不可用）'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 收到中断，正在关闭……")
    finally:
        with _mic_lock:
            _mic_stop_locked()  # 兜底：服务关闭前释放麦克风
        server.server_close()
        _cleanup_pidfile()  # 与 atexit 幂等重复调用无害


if __name__ == "__main__":
    main()
