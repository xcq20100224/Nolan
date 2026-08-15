# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 网页版后端（server.py）
职责：用 Python 标准库 http.server 暴露 JSON API，供 React+Vite 前端调用。

第一性原理：不引任何后端框架——标准库 ThreadingHTTPServer 足够；
不做用户系统、不做数据库、不做静态文件服务（前端由 vite dev server 提供，
vite 把 /api 代理到本服务）。

端口：sys.argv[1]，默认 7901。

单实例守卫（最高优先级）：
    根因——Kimi 预览反复拉起新后端而旧后端不死，Windows 默认允许同端口双绑定，
    请求被新旧进程随机瓜分，声音/功能时好时坏。对策三件套：
    1) 启动时先读 pidfile（jarvis/files/server.pid），若记录的 PID 仍存活且不是自己，
       用 taskkill /F /PID 清理旧进程并等待 1 秒让端口释放（权限不足只告警继续）；
    2) listen socket 设置 SO_EXCLUSIVEADDRUSE（Windows 独占绑定，从机制上杜绝双绑定）；
    3) 绑定成功后写入当前 PID，进程退出时清理 pidfile（只删自己写的那份）。

API 契约（一字不差）：
    GET  /api/health     → {"ok": true, "name": "Nolan"}
    GET  /api/version    → {"version": "2026-08-01-stage2", "pid": 整数}
                         （后端代码版本标识 + 当前进程 PID；
                         用于验证运行中的后端是否为最新代码，排查陈旧进程）
    POST /api/chat       请求 {"text": "..."} → 响应 {"reply": str, "audio_url": str|null}
                         若 brain 返回 '__EXIT__' → {"reply": "道别语", "audio_url": str|null, "exit": true}
                         （audio_url 为 TTS 合成音频的 URL，合成失败为 null）
    POST /api/chat/stream 请求 {"text": "..."} → SSE（text/event-stream，Connection: close 终止）
                         句级流式对话：LLM 边产出 token、后端边切句边合成、合成好一句推一句。
                         事件序列（每事件一行 'data: <json>\n\n'）：
                           {"type":"delta","text":...}                LLM 增量文本（字幕逐字出现）
                           {"type":"sentence","text":...,"audio_url":...}  一句合成完毕（audio_url 可空）
                           {"type":"done","reply":...,"degraded"?:true} 全量收尾
                           {"type":"fallback","reply","audio_url","exit"?} 回退整段
                           {"type":"progress","step":...,"i"?:...,"n"?:...} 长任务进度
                         progress 事件只在回退整段执行工具（如 make_ppt）期间穿插推送，
                         i/n 为可选计数（无计数时省略）；fallback/done 等既有事件不变。
                         回退契约：规则意图（提醒/记忆/工具/复合/退出/待确认）与流式
                         早期失败一律回退到与 /api/chat 完全相同的整段路径（brain.think +
                         synth_for），以 fallback 事件下发——对话绝不因流式化而挂掉。
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
    GET  /api/files/<文件名> → 图片（jpg/jpeg/png/webp）内联返回 image/*；
                         下载白名单（pdf/docx/pptx/txt/md/csv 及文本类后缀）以
                         Content-Disposition: attachment 下载（对应 MIME）；
                         只允许安全相对路径（含 uploads/ 等子目录），路径穿越一律 404
    POST /api/upload     请求 {"name": "原始文件名", "data_base64": "..."}
                         （base64 JSON 契约：标准库无 multipart 解析器，此契约最简单；
                         请求体上限 65MB，解码后文件上限 50MB）
                         → {"ok": true, "name": 存储文件名, "kind": 类别,
                            "chars": 抽取字数, "excerpt": 前 8000 字, "text": 全量抽取文本,
                            "meta": {辅助信息}, "note": 诚实说明,
                            "truncated": bool, "file_url": "/api/files/uploads/<存储名>"}
                         落盘 jarvis/files/uploads/（目录自动创建，时间戳前缀防覆盖，
                         文件名净化只留安全字符 + commonpath 双重防护）；
                         全类型支持：抽取逻辑在 jarvis/file_reader.py 类型路由器
                         （文本直读 / csv·xlsx pandas 分析 / docx 段落+表格 /
                         pptx XML 解析 / pdf pypdf 文字层 / 图片 VLM 解读 /
                         音视频 whisper 转写 / zip 清单+就地抽取 / 二进制魔数+strings）；
                         任何单类失败只降级 note，绝不报错拒收；
                         非图片/二进制类的抽取全文落盘 <存储名>.extracted.txt，
                         供深度追问时回读
    GET  /api/files_list → {"files": [{"name","size","mtime","kind"}]}
                         递归列出 jarvis/files/（含 uploads/ 子目录；排除 tts_cache、
                         pidfile、日志等后端内部文件），按 mtime 倒序，
                         kind ∈ 文档/图片/表格/音频/其他
    POST /api/asr        请求体为原始音频字节（audio/webm|ogg|wav）
                         → {"text": "识别文本"}；无语音 {"text": ""}
    POST /api/mic/start  → {"ok": true}（服务端直接开麦录音，绕开浏览器权限）
    POST /api/mic/stop   → {"text": "..."}（停止并识别；无语音/未录音 {"text": ""}）
    POST /api/stop       → {"stopped": true}（立即打断服务端音箱当前播报；
                         前端「停止说话」按钮与发送新消息前的自动打断都走这里）
全部 JSON UTF-8（ensure_ascii=False）、CORS 允许 *、处理 OPTIONS 预检；
异常返回 500 + {"error": 中文说明}；未知路径 404。

运行：python server.py [端口]
"""

import asyncio
import atexit
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

# == 把 ../jarvis 加入 sys.path（用 __file__ 定位，与启动目录无关）==
_JARVIS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jarvis")
_JARVIS_DIR = os.path.normpath(_JARVIS_DIR)
if _JARVIS_DIR not in sys.path:
    sys.path.insert(0, _JARVIS_DIR)

import brain       # noqa: E402  大脑：think(user_text, history) -> str
import reminders   # noqa: E402  提醒：check_due / list_pending / add
import memory      # noqa: E402  记忆：recall / load / remember / forget
import file_reader  # noqa: E402  全类型文件阅读引擎：read_upload 类型路由器

try:
    import memory_v2  # noqa: E402  Gap3 结构化长期记忆（画像/萃取）
except ImportError:
    memory_v2 = None
try:
    import proactive  # noqa: E402  Gap4 主动性引擎（三重闸门 + 注入式生成）
except ImportError:
    proactive = None
try:
    import speak_filter as _speak_filter  # noqa: E402  Gap8 说话卫生（剥离代码/JSON）
except ImportError:
    _speak_filter = None
try:
    import progress as _progress  # noqa: E402  通用进度总线（jarvis/progress.py）
except Exception:
    _progress = None

# == 后端代码版本标识 ==
# 用途：曾出现『GUI 失败源于陈旧后端进程（旧代码仍在内存中运行）』的问题，
# 仅靠单实例守卫清理旧进程还不够直观——需要让『当前跑的是不是新代码』一眼可验。
# GET /api/version 返回本常量与当前进程 PID；改代码后务必同步更新本常量。
_VERSION = "2026-08-15-glm53"

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

# == 文件通道（入口：拖拽上传阅读；出口：文件柜列表/下载）==
# 第一性原理：Nolan 的 write_file 工具与网页上传共用同一个『文件柜』——
# jarvis/files/（上传落在其 uploads/ 子目录）。入口把文件抽成文本喂给对话，
# 出口把柜子里的文件列出来供查看/下载；两侧都只认白名单、都过路径穿越防护。
_UPLOADS_DIR = os.path.normpath(os.path.join(_FILES_DIR, "uploads"))
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024       # 上传文件大小上限 50MB
_UPLOAD_MAX_BODY_BYTES = 65 * 1024 * 1024  # base64 请求体粗闸门上限（50MB 膨胀约 1/3 再加余量）
_EXCERPT_CHARS = 8000                      # 响应 excerpt 带上前 8000 字
                                           # （与前端附件拼接上限一致；超出部分
                                           #  由 read_file 工具经 uploads/ 子目录的
                                           #  .extracted.txt 落盘全文回读）

# ---- 静态托管（软件形态）：vite 构建产物 dist/ 由后端直接服务，
# 双击 bat 即可用，不再依赖 vite dev server；SPA 路径回退 index.html
_DIST_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist"))
_STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

# /api/files 下载白名单（图片之外的扩展；下载统一带 Content-Disposition: attachment）
_DOWNLOAD_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    # 文本直读类其余后缀统一下载为纯文本
    ".py": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8",
    ".ts": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
}

# 文件柜列表的内部排除项：TTS 缓存目录、pidfile、背景状态文件、诊断/录音日志
# ——它们是后端运行机制的产物，不属于『Nolan 生成 / 先生上传的文件』
_LIST_EXCLUDE_DIRS = {"tts_cache"}
_LIST_EXCLUDE_FILES = {"server.pid", "web_background.json"}
_LIST_EXCLUDE_EXTS = {".log"}

# 文件柜 kind 分类（按后缀）
_KIND_EXTS = {
    "图片": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"},
    "表格": {".csv", ".xls", ".xlsx"},
    "音频": {".mp3", ".wav", ".ogg", ".m4a"},
    "文档": {".pdf", ".doc", ".docx", ".txt", ".md", ".ppt", ".pptx"},
}


def _classify_kind(ext: str) -> str:
    """按后缀把文件归为 文档/图片/表格/音频/其他。"""
    for kind, exts in _KIND_EXTS.items():
        if ext in exts:
            return kind
    return "其他"


def _sanitize_filename(name: str) -> str:
    """
    文件名净化：先剥掉一切路径成分，再只保留安全字符
    （英数、点、短横、下划线、常用汉字），其余一律替换为 '_'；
    去掉前导点（防隐藏文件与 '..' 残留），空名兜底为 'file'。
    """
    base = os.path.basename((name or "").replace("\\", "/"))
    out = []
    for ch in base:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        elif "一" <= ch <= "鿿":
            out.append(ch)  # 常用汉字放行（先生的文件大多是中文名）
        else:
            out.append("_")
    cleaned = "".join(out).lstrip(".").strip()
    return cleaned or "file"


def _whisper_transcribe_file(path: str):
    """
    音视频转写通道（注入给 file_reader 用）：复用常驻 whisper 单例按路径转写，
    返回 (文本, 时长秒)。模型不可用抛 RuntimeError；PyAV 解码失败的格式
    异常原样上抛——file_reader 统一降级为诚实 note，绝不 500。
    """
    _load_whisper()
    if _whisper_model is None:
        raise RuntimeError(_whisper_error or "语音模型不可用。")
    with _whisper_lock:
        segments, info = _whisper_model.transcribe(
            path,
            language="zh",
            initial_prompt="以下是用户上传的音频/视频文件内容，多为中文。",
            beam_size=1,      # 与 ASR 端点同策略：small 模型贪心解码够用且快
            vad_filter=True,  # 过滤静音段，纯音乐/静音自然得到空结果
        )
        text = "".join(seg.text for seg in segments).strip()
    return text, float(getattr(info, "duration", 0.0) or 0.0)


def _save_upload(name: str, data: bytes,
                 uploads_dir: str = _UPLOADS_DIR, files_dir: str = _FILES_DIR):
    """
    上传落盘 + 全类型抽取（纯逻辑，可单测；不起服务）。
    返回 (存储文件名, 抽取结果 dict)；dict 契约见 file_reader.read_upload
    （kind/text/meta/note，任何单类失败只降级 note，绝不抛异常）。
    只有硬性闸门失败抛 ValueError（中文说明）：空文件、超 50MB、目录越界。

    安全闸门（与 _serve_file 同一套思路）：
      大小上限 50MB；文件名净化只留安全字符；时间戳前缀防覆盖；
      落盘目录必须仍在 files 目录内（commonpath 双重保险）。
    非图片/二进制类的抽取全文落盘 <存储名>.extracted.txt（深度追问回读用：
    read_file 工具特许「uploads/文件名」一层子目录，历史存根里的回读路径
    即指向此文件，见 _slim_for_history）。
    """
    if not data:
        raise ValueError("文件内容为空。")
    if len(data) > _UPLOAD_MAX_BYTES:
        raise ValueError(
            f"文件超过大小上限（50MB），本文件约 {len(data) / 1024 / 1024:.1f}MB。")
    # 双重保险：上传目录必须仍在 files 目录内（配置被改动时宁可拒绝写入）
    try:
        inside = os.path.commonpath(
            [os.path.abspath(uploads_dir), os.path.abspath(files_dir)]
        ) == os.path.abspath(files_dir)
    except ValueError:
        inside = False  # 不同盘符等异常，一律视为不合法
    if not inside:
        raise ValueError("上传目录配置异常（不在文件柜内），已拒绝写入。")
    try:
        os.makedirs(uploads_dir, exist_ok=True)
    except OSError as e:
        raise ValueError(f"上传目录创建失败：{e}")
    safe = _sanitize_filename(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stored = f"{stamp}_{safe}"
    full = os.path.join(uploads_dir, stored)
    n = 2
    while os.path.exists(full):
        # 同一秒同名上传：追加序号防覆盖
        stored = f"{stamp}-{n}_{safe}"
        full = os.path.join(uploads_dir, stored)
        n += 1
    try:
        with open(full, "wb") as f:
            f.write(data)
    except OSError as e:
        raise ValueError(f"文件写入失败：{e}")
    # 全类型路由：任何格式都有通道，任何单类失败只降级 note，文件一律留柜
    result = file_reader.read_upload(full, name,
                                     transcribe_fn=_whisper_transcribe_file)
    # 抽取全文落盘（图片/二进制类没有长文本产物，不落）
    if result.get("text") and result.get("kind") not in ("图片", "二进制"):
        try:
            with open(full + ".extracted.txt", "w", encoding="utf-8") as f:
                f.write(result["text"])
        except OSError as e:
            print(f"[server] 抽取全文落盘失败（不影响上传）：{e}")
    return stored, result


def _resolve_files_path(name: str):
    """
    /api/files 路径解析（纯逻辑，可单测）：合法返回绝对路径，非法/路径穿越返回 None。
    拒绝 '..'、绝对路径、盘符（':'）；解析后的绝对路径必须落在 files 目录内（含子目录）。
    """
    if not name or ".." in name or os.path.isabs(name) or ":" in name:
        return None
    full = os.path.normpath(os.path.join(_FILES_DIR, name))
    try:
        inside = os.path.commonpath(
            [os.path.abspath(full), os.path.abspath(_FILES_DIR)]
        ) == os.path.abspath(_FILES_DIR)
    except ValueError:
        return None  # 不同盘符等异常，一律视为不合法
    return full if inside else None


def _files_list_payload() -> dict:
    """
    文件柜列表：递归扫描 jarvis/files/（含 uploads/ 子目录），按 mtime 倒序。
    排除后端内部文件（TTS 缓存、pidfile、背景状态、日志）——它们不属于
    『Nolan 生成 / 先生上传的文件』。name 为相对 files 目录的正斜杠路径。
    """
    items = []
    try:
        for root, dirs, files in os.walk(_FILES_DIR):
            dirs[:] = [d for d in dirs if d not in _LIST_EXCLUDE_DIRS]
            for fname in files:
                if fname in _LIST_EXCLUDE_FILES:
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in _LIST_EXCLUDE_EXTS:
                    continue
                full = os.path.join(root, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, _FILES_DIR).replace(os.sep, "/")
                items.append({
                    "name": rel,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "kind": _classify_kind(ext),
                })
    except OSError:
        pass  # 目录不可读时返回空列表，不报错
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": items}


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
            payload = resp.content
            # Gap8 说话卫生：GLM-TTS 的 wav 自带 ~1.8 秒「滴答」接通提示音，
            # 落盘前裁掉前奏+淡入（净化异常原样落盘，绝不弄坏音频）
            try:
                import audio_clean
                payload = audio_clean.clean_wav_bytes(payload)
            except Exception:
                pass
            with open(path, "wb") as f:
                f.write(payload)
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
    # 说话卫生（Gap8）：代码/JSON/路径/URL 是思考不是台词——
    # 进发声链前剥离；剥完没剩人话就用通用兜底话术，绝不念代码
    if _speak_filter is not None:
        try:
            text = _speak_filter.speakable(text, max_chars=None) \
                or "先生，详细内容我放在屏幕上了。"
        except Exception:
            pass  # 过滤器异常原样放行，绝不阻断发声
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
            # P5 换代（bench_asr.py 实测数字）：small/beam1 = 1.53x 实时倍率、
            # 准确率 92%，与 medium/beam5（5.49x/92%）持平但提速 3.6 倍，
            # 冷启动加载 ~6s（medium 需 ~58s，此前「识别等数十秒」的主因）
            print("[server] 正在加载 faster-whisper small 模型（CPU+int8，P5 换代：3.6x 提速）……")
            _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
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
                beam_size=1,  # P5：small 模型贪心解码实测准确率不降（92%），更快
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
        _wake_pause_ev.set()  # 指令录音期间耳蜗暂停，防抢麦与锁竞争
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
        _wake_pause_ev.clear()  # 录音结束，耳蜗恢复值守
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
                beam_size=1,  # P5：small 模型贪心解码实测准确率不降（92%），更快
                vad_filter=True,  # 过滤静音段，无语音时自然得到空结果
            )
            text = "".join(seg.text for seg in segments).strip()
        return text
    except Exception as e:
        raise RuntimeError(f"语音识别失败了：{e}")


# == 唤醒词（J5 在场感）：常驻耳蜗，主人说「诺兰 / Nolan」即回应 ==
#
# 第一性原理：唤醒 = 持续听（sounddevice 常驻流）+ 能量门控（只对语音段
# 触发识别，静音零开销）+ 关键词命中（复用 medium whisper 中文识别，
# 与指令录音同模型，唤醒时指令录音暂停耳蜗防抢麦）。
# 自我触发防护：Nolan 自己播报的音频也含「诺兰」——每次回复/问候/闹钟后
# 暂停耳蜗一个与播报时长匹配的窗口，绝不陷入「自己叫醒自己」的循环。
_WAKE_KEYWORDS = ("诺兰", "nolan", "诺蓝", "挪兰", "脑兰", "洛兰", "罗兰")
                                    # 「洛兰/罗兰」：small 模型对唤醒词的同音误识（P5 实测）
_WAKE_STATE_FILE = os.path.join(_JARVIS_DIR, "memory", "wake_state.json")
_WAKE_ACK = "在的，先生，请讲。"
_WAKE_RMS_GATE = 0.012          # 语音能量门（float32 RMS，低于视为静音）
_WAKE_MAX_PAUSE_SEC = 20.0      # 播报后暂停耳蜗的封顶秒数

_wake_enabled = False
_wake_thread = None
_wake_stop_ev = threading.Event()
_wake_pause_ev = threading.Event()   # 指令录音期间置位（防抢麦）
_wake_pause_until = 0.0              # 播报期间暂停到该时间戳（防自我触发）
import collections as _collections   # noqa: E402  模块内专用，避免搅动文件头
_wake_events = _collections.deque(maxlen=10)
_wake_events_lock = threading.Lock()

# == 播报打断（P3 全双工·网页版）：耳蜗在播报窗口内不丢帧，转为打断侦测 ==
# 第一性原理：打断 = 唤醒的免费副产品——常驻麦克风流、能量门控、语音段收集、
# whisper 转写全都现成，唯一新问题是「区分主人声音和 Nolan 自己的播报回声」。
# 无 AEC 条件下的双保险（两道独立防线，各自都能单独拦住回声）：
#   ① 自适应能量门：基线取近期帧 RMS 中位数（含播报声），门 = 基线 × 倍率，
#      主人近场语音远高于音箱到麦克风的回声，回声本身够不到门；
#   ② 回声文本过滤：服务端知道 Nolan 正在说什么（_now_speaking_text），
#      转写文本与播报文本高度相似即判定回声丢弃——即使回声触发能量门也过不了这关。
_BARGEIN_GATE_MULT = 2.0       # 自适应门倍率（基线含播报声，主人语音须明显盖过）
_BARGEIN_BASELINE_N = 40       # 滚动基线窗口（约 2 秒帧能量）
_BARGEIN_ECHO_RATIO = 0.45     # 与播报文本相似度阈值（超过即判回声）
_now_speaking_text = ""        # Nolan 当前正在播报的文本（回声判定依据）
_bargein_silence_until = 0.0   # 打断成立后的硬静默期（前端接管期间丢帧，防重复触发）


def _wake_pause_for(seconds: float) -> None:
    """播报后暂停耳蜗 seconds 秒（封顶 _WAKE_MAX_PAUSE_SEC）。"""
    global _wake_pause_until
    _wake_pause_until = max(
        _wake_pause_until,
        time.time() + min(max(seconds, 0.0), _WAKE_MAX_PAUSE_SEC))


def _wake_load_state() -> bool:
    try:
        with open(_WAKE_STATE_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return False


def _wake_save_state(enabled: bool) -> None:
    try:
        os.makedirs(os.path.dirname(_WAKE_STATE_FILE), exist_ok=True)
        with open(_WAKE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f)
    except Exception as e:
        print("[wake] 状态写入失败（跳过）：%s" % e)


def _note_speaking(text: str) -> None:
    """登记 Nolan 即将播报的文本：播报窗口内回声文本过滤的判定依据。"""
    global _now_speaking_text
    _now_speaking_text = (text or "").strip()


def _transcribe_seg(audio, beam_size: int = 1) -> str:
    """对一段语音做小代价转写（耳蜗/打断侦测共用）；失败或模型不可用返回空串。"""
    _load_whisper()
    if _whisper_model is None:
        return ""
    try:
        with _whisper_lock:
            segments, _ = _whisper_model.transcribe(
                audio, language="zh", beam_size=beam_size, vad_filter=False)
        return "".join(s.text for s in segments).strip()
    except Exception as e:
        print("[wake] 转写失败（跳过）：%s" % e)
        return ""


def _normalize_text(s: str) -> str:
    """相似度比较前的归一化：小写 + 去标点空白，只看字面内容。"""
    return "".join(ch for ch in s.lower() if ch.isalnum() or "一" <= ch <= "鿿")


def _is_echo(heard: str) -> bool:
    """听到的文本是否为 Nolan 自己播报的回声：与当前播报文本高度相似即判回声。"""
    a = _normalize_text(heard)
    b = _normalize_text(_now_speaking_text)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= _BARGEIN_ECHO_RATIO


def _wake_spot(audio) -> None:
    """对一段语音做关键词侦测：命中唤醒词即入队事件并暂停播报窗口。"""
    text = _transcribe_seg(audio).lower()
    if not text:
        return
    print("[wake] 听到：%s" % text[:40])
    if any(k in text for k in _WAKE_KEYWORDS):
        # B4 声纹门禁（默认关闭）：开启且已注册模板时，
        # 非主人声音说唤醒词不唤醒（防家人/电视误触发）
        try:
            import ears
            if not ears.voice_gate_pass(audio):
                print("[wake] 唤醒词命中但声纹不匹配，忽略。")
                return
        except Exception:
            pass  # 门禁异常恒放行，绝不把主人锁在门外
        print("[wake] 唤醒词命中，通知前端。")
        with _wake_events_lock:
            _wake_events.append({"ts": time.time(), "kind": "wake"})
        _wake_pause_for(8)  # 确认音播报窗口，防自我触发


def _bargein_check(audio) -> None:
    """播报窗口内的打断侦测：转写语音段，非回声即判定主人打断，入队 bargein 事件。

    回声双保险：能量门已在采集侧拦住大部分播报声（见 _wake_loop 自适应门），
    此处做文本级终审——听到的内容与 Nolan 正在说的话高度相似，必是自己人。
    """
    text = _transcribe_seg(audio)
    if not text:
        return
    if _is_echo(text):
        print("[bargein] 回声段（自己的播报声），忽略：%s" % text[:30])
        return
    print("[bargein] 主人打断了播报：%s" % text[:40])
    with _wake_events_lock:
        _wake_events.append({"ts": time.time(), "kind": "bargein", "text": text})
    # 打断成立后进入硬静默期：前端轮询到事件需要几秒，期间丢帧不侦测，
    # 防止主人后半句话被二次侦测成重复事件（同一指令发两遍）
    global _wake_pause_until, _bargein_silence_until
    _wake_pause_until = 0.0
    _bargein_silence_until = time.time() + 6


def _wake_loop() -> None:
    """耳蜗主循环：能量门控收集语音段，逐段侦测唤醒词；可随时停止。

    三种状态（第一性原理：一条常驻流，分时复用）：
      硬暂停（指令录音 / 打断后静默期）：丢帧静默，麦克风让位；
      播报窗口（_wake_pause_until 内）：打断侦测——自适应能量门（基线含
        播报声）收集语音段，转写后经回声文本过滤，主人声音入队 bargein 事件；
      正常值守：固定能量门收集语音段，侦测唤醒词。
    """
    import numpy as np
    import sounddevice as sd
    print("[wake] 耳蜗启动：对麦克风说「诺兰」即可唤醒，播报时直接说话即可打断。")
    frames_buf = []
    speech = []
    in_speech = False
    silence_hits = 0
    baseline = _collections.deque(maxlen=_BARGEIN_BASELINE_N)  # 播报窗口滚动能量基线

    def _cb(indata, frames, time_info, status):
        frames_buf.append(indata.copy())

    stream = None
    try:
        stream = sd.InputStream(samplerate=_MIC_SAMPLERATE, channels=1,
                                dtype="float32", callback=_cb)
        stream.start()
        while not _wake_stop_ev.is_set():
            now = time.time()
            # 硬暂停（指令录音 / 打断后静默期）：清空缓冲，静默等待
            if _wake_pause_ev.is_set() or now < _bargein_silence_until:
                frames_buf.clear()
                speech.clear()
                in_speech = False
                baseline.clear()
                time.sleep(0.2)
                continue
            in_playback = now < _wake_pause_until
            time.sleep(0.05)
            if not frames_buf:
                continue
            chunk = np.concatenate(frames_buf, axis=0).flatten()
            frames_buf.clear()
            rms = float(np.sqrt(float(np.mean(chunk ** 2)))) if chunk.size else 0.0

            if in_playback:
                # 打断侦测：自适应门 = 滚动基线（含播报声）× 倍率
                gate = max(
                    float(np.median(baseline)) * _BARGEIN_GATE_MULT if baseline else 0.0,
                    _WAKE_RMS_GATE)
            else:
                baseline.clear()
                gate = _WAKE_RMS_GATE

            if rms > gate:
                speech.append(chunk)
                in_speech = True
                silence_hits = 0
                if not in_playback or len(speech) <= 2:
                    # 基线只收「未判定为语音」的能量；语音头两帧不污染基线
                    pass
            elif in_speech:
                speech.append(chunk)
                silence_hits += 1
                if in_playback:
                    baseline.append(rms)  # 语音间隙的能量回充基线（播报声是连续底噪）
                if silence_hits >= 10:  # 连续约 0.5 秒静音：语音段结束
                    seg = np.concatenate(speech)
                    speech.clear()
                    in_speech = False
                    secs = seg.shape[0] / _MIC_SAMPLERATE
                    if 0.3 <= secs <= 6.0:
                        if in_playback:
                            _bargein_check(seg)
                        else:
                            _wake_spot(seg)
            else:
                # 纯静音帧：正常值守忽略；播报窗口回充基线，跟踪播报音量变化
                if in_playback:
                    baseline.append(rms)
    except Exception as e:
        print("[wake] 耳蜗异常退出：%s" % e)
    finally:
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass
        print("[wake] 耳蜗已停止。")


def _wake_set_enabled(enabled: bool) -> dict:
    """开关耳蜗：开启拉起守护线程，关闭发停止信号；状态落盘。"""
    global _wake_enabled, _wake_thread
    enabled = bool(enabled)
    if enabled and not (_wake_thread and _wake_thread.is_alive()):
        _wake_stop_ev.clear()
        _wake_thread = threading.Thread(target=_wake_loop, daemon=True,
                                        name="wake-ear")
        _wake_thread.start()
    elif not enabled:
        _wake_stop_ev.set()
    _wake_enabled = enabled
    _wake_save_state(enabled)
    return {"enabled": _wake_enabled,
            "listening": bool(_wake_thread and _wake_thread.is_alive())}


# == 全局状态 ==
_brain_lock = threading.Lock()   # brain 调用与 mouth 播报共用同一把锁串行化
_history = []                    # 服务端维护的对话历史，裁剪到 20 轮（40 条）
_HISTORY_MAX_TURNS = 20

# == 历史持久化（重启不失忆）==
# 根因（2026-08-11 真机病例）：主人刚让 Nolan 写的作文还显示在屏幕对话记录里，
# 一句「生动形象一些」，Nolan 却答「我这边没有看到之前的文稿」——
# 对话记录是前端状态，_history 是内存态；server 一旦重启（部署/预览重启/休眠恢复），
# 大脑失忆，屏幕上的对话成了只有主人记得的单向记忆。伙伴的第一条纪律：
# 你记得的事，我必须也记得。
# 对策——写穿式落盘 jarvis/files/web_chat_history.json（原子替换，防爆写），
# 启动时恢复；存的已是 _slim_for_history 后的存根版，附件全文不进盘。
_HISTORY_FILE = os.path.normpath(os.path.join(_FILES_DIR, "web_chat_history.json"))


def _save_history() -> None:
    """把 _history 原子写盘；失败只记日志，绝不影响对话主流程。调用方须持 _brain_lock。"""
    try:
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_history, f, ensure_ascii=False)
        os.replace(tmp, _HISTORY_FILE)
    except Exception as e:  # noqa: BLE001 - 落盘失败下轮再试
        print(f"[server] 对话历史落盘失败（下轮再试）：{e}")


def _load_history() -> None:
    """启动时恢复对话历史；文件缺席/损坏一律从零开始，不阻断服务。"""
    global _history
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        turns = [
            t for t in data
            if isinstance(t, dict)
            and t.get("role") in ("user", "assistant")
            and isinstance(t.get("content"), str)
        ]
        _history = turns[-_HISTORY_MAX_TURNS * 2:]
        if _history:
            print(f"[server] 对话历史已恢复：{len(_history) // 2} 轮（重启不失忆）")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 - 损坏文件不阻断启动
        print(f"[server] 对话历史恢复失败（从零开始）：{e}")


# == 历史瘦身（上下文工程）：附件全文只活在当前轮，历史里只留紧凑存根 ==
# 根因——前端把附件抽取全文（≤8000 字）拼进用户消息（标记块
# [附件《存储名》内容开始]…[附件内容结束，请基于以上内容回答]），若原样写入
# _history，这段全文要在历史里躺 20 轮，LLM 每轮都扛着它：上下文被稀释、
# 注意力被带偏（实测「乱回答」的物理来源之一）。
# 对策——写入历史前把附件块替换为存根（文件名/字数/落盘路径）；全文落盘在
# jarvis/files/uploads/<存储名>.extracted.txt，LLM 需要时用 read_file 工具
# 按「uploads/<存储名>.extracted.txt」回读（hands._sandbox_path 特许
# uploads/ 一层子目录）。发给 brain 的当前轮消息仍是全文，不受影响。
_ATTACH_BLOCK_RE = re.compile(
    r"\[附件《([^》]*)》内容开始\]\n?(.*?)\n?\[附件内容结束，请基于以上内容回答\]",
    re.DOTALL,
)
_ATTACH_START_MARK = "[附件《"


def _slim_for_history(user_text: str, uploads_dir: str = _UPLOADS_DIR) -> str:
    """
    把消息中的附件全文块替换为紧凑存根（纯逻辑，可单测）：
      「附件《安全名》（共N字，全文已存 uploads/<安全名>.extracted.txt，
        可用 read_file 工具回读「uploads/<安全名>.extracted.txt」）」
    - 安全名与 upload 落盘同一规则（_sanitize_filename，对前端已净化的
      存储名幂等）；N 为本消息内嵌附件正文的字数（前端按 8000 字截断，
      不一定是文件全文总字数）；
    - 无附件消息原样返回；多附件逐块替换；附件块前后的问题文本原样保留；
    - 保守闸门：起始标记数与完整块数不一致（标记残缺）时整条原样返回，
      绝不半截替换弄丢内容；
    - 落盘副本不存在时（图片 VLM 文本/文件被清理等）存根降级为
      「无全文落盘副本可回读」，不给 LLM 指一条读不到的路。
    """
    text = user_text or ""
    if _ATTACH_START_MARK not in text:
        return text
    blocks = list(_ATTACH_BLOCK_RE.finditer(text))
    if text.count(_ATTACH_START_MARK) != len(blocks):
        return text  # 有残缺标记（缺结尾/头尾不全），保守原样返回

    def _stub(m):
        safe = _sanitize_filename(m.group(1))
        n = len(m.group(2))
        rel = f"uploads/{safe}.extracted.txt"
        if os.path.isfile(os.path.join(uploads_dir, safe + ".extracted.txt")):
            return (f"「附件《{safe}》（共{n}字，全文已存 {rel}，"
                    f"可用 read_file 工具回读「{rel}」）」")
        return f"「附件《{safe}》（共{n}字，无全文落盘副本可回读）」"

    return _ATTACH_BLOCK_RE.sub(_stub, text)


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


# == 主动晨报（J5）：每天第一次打开网页时，Nolan 主动开口问候 ==
_GREETING_STATE = os.path.join(_JARVIS_DIR, "memory", "greeting_state.json")


def _greeting_payload() -> dict:
    """
    每天首次请求：时段问候 + 日期星期 + 待办提醒数 + 语音；
    当天已问候过返回 {"greeted": true}，前端退回静态欢迎语。
    状态落盘（greeting_state.json），重启服务也不重复问候——
    同伴的分寸感：主动，但不啰嗦。
    """
    lt = time.localtime()
    today = time.strftime("%Y-%m-%d", lt)
    try:
        with open(_GREETING_STATE, encoding="utf-8") as f:
            if json.load(f).get("date") == today:
                return {"greeted": True, "text": None, "audio_url": None}
    except Exception:
        pass
    salut = "早上好" if 5 <= lt.tm_hour < 12 else (
        "下午好" if lt.tm_hour < 18 else "晚上好")
    text = "%s，先生。今天是%d年%d月%d日，星期%s。" % (
        salut, lt.tm_year, lt.tm_mon, lt.tm_mday,
        "一二三四五六日"[lt.tm_wday])
    try:
        rem_file = os.path.join(_JARVIS_DIR, "memory", "reminders.txt")
        with open(rem_file, encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip())
        if n:
            text += "您有 %d 个待办提醒，需要时对我说「我的提醒」。" % n
    except Exception:
        pass
    text += "Nolan 在线，随时吩咐。"
    try:
        os.makedirs(os.path.dirname(_GREETING_STATE), exist_ok=True)
        with open(_GREETING_STATE, "w", encoding="utf-8") as f:
            json.dump({"date": today}, f)
    except Exception:
        pass
    return {"greeted": False, "text": text, "audio_url": synth_for(text)}


# == 显示层终态卫生（与发声层同一套 speak_filter 标准）==
# 第一性原理：工具调用 JSON、代码块是 Nolan 的思考，不是给先生看的内容。
# 发声层（synth_for）早已剥离；显示层此前只在 brain.think 出口闸（_speak_guard）
# 剥过——但流式路径（/api/chat/stream）绕开 brain 自行拼装 LLM 全文，
# 「人话 + 行尾工具 JSON」混排会原样漏进字幕终态与历史记录。
# 此处收口：两条对话路径的最终回复文本在「落历史 + 发前端」之前统一过一道。
# 流式中间态（逐字 delta）允许短暂出现 JSON 字符，终态必须干净。
_DISPLAY_PLACEHOLDER = "（正在处理，请稍候…）"


def _display_clean(text: str) -> str:
    """显示层终态剥离：工具 JSON/代码块/路径/URL 一律剥掉，只留人话。

    剥完为空（纯工具调用、无台词）用轻量占位——不用 TTS 那句兜底话术
    （「详细内容我放在屏幕上了」在屏幕上读出来是循环指涉）。
    过滤器缺失或异常时原样放行：显示层绝不因卫生检查阻断对话。
    """
    t = (text or "").strip()
    if not t or _speak_filter is None:
        return t
    try:
        return _speak_filter.speakable(t, max_chars=None) or _DISPLAY_PLACEHOLDER
    except Exception:
        return t


def _chat(user_text: str) -> dict:
    """
    处理一轮对话：串行调用 brain.think，维护 history（裁剪 20 轮）。
    返回 {"reply": str, "audio_url": str|null}，exit 时附加 "exit": True。
    audio_url 为 TTS 发声链合成的回复语音（缓存复用），合成失败为 None。
    回复语音只由浏览器播放（单通道），服务端音箱仅用于闹钟提醒。
    """
    global _history, _last_user_activity
    _last_user_activity = time.time()  # Gap4：主动性「刚交互过不打扰」闸门依据
    user_text = (user_text or "").strip()
    if not user_text:
        reply = "先生，您似乎还没有说话，请输入内容后再发送。"
        return {"reply": reply, "audio_url": synth_for(reply)}

    # 新消息进场前先打断正在进行的音箱播报（主人开口即优先）
    if mouth is not None:
        try:
            mouth.interrupt()
        except Exception:
            pass

    with _brain_lock:
        reply = brain.think(user_text, list(_history))
        # 历史写入与裁剪也在同一临界区内：与 brain.think 串行化，
        # 杜绝并发请求把对话历史交错、裁剪互相覆盖
        if reply != "__EXIT__":
            # 显示层终态卫生：剥离工具 JSON/代码后再落历史、发前端
            # （__EXIT__ 哨兵原样保留，绝不过滤）
            reply = _display_clean(reply)
            # 历史瘦身：写入历史的是存根版（附件全文不躺历史）；
            # 发给 brain 的当前轮 user_text 仍是全文（LLM 本轮要读附件）
            _history.append({"role": "user", "content": _slim_for_history(user_text)})
            _history.append({"role": "assistant", "content": reply})
            if len(_history) > _HISTORY_MAX_TURNS * 2:
                _history = _history[-_HISTORY_MAX_TURNS * 2:]
            _save_history()  # 写穿落盘：server 重启后大脑仍记得这轮对话

    if reply == "__EXIT__":
        farewell = "好的先生，我先去休息了，随时叫我的名字就能唤醒我。"
        # 只走浏览器 audio_url 单通道发声（见下方说明）
        _note_speaking(farewell)  # 登记播报文本：打断侦测的回声判定依据
        _wake_pause_for(2 + len(farewell) * 0.3)  # 播报窗口=打断侦测窗口
        return {"reply": farewell, "audio_url": synth_for(farewell), "exit": True}

    # 发声单通道化：网页端只由浏览器播放 audio_url，服务端音箱不再同步播报。
    # 此前音箱 + 浏览器双通道同时发声（引擎/延迟不同），听感是两个人重合说话。
    # 音箱通道仍保留给闹钟提醒（/api/due 的 _speak_alarm_async，必被听见场景）
    _note_speaking(reply)  # 登记播报文本：打断侦测的回声判定依据
    _wake_pause_for(2 + len(reply) * 0.3)
    return {"reply": reply, "audio_url": synth_for(reply)}


# == 句级流式对话（/api/chat/stream · Gap1 流式化网页版）==
#
# 第一性原理：出声延迟 = 首个音符出现的时间。整段路径把「等 LLM 想完全文」
# 和「等 TTS 合完全文」两笔税串行叠加（148 字长回复实测 5.8 秒才出声）。
# 句级流水线把三件事重叠：LLM 边产出 token（stream=True），后端边按句尾
# 标点切句，TTS 生产线程合成好一句立刻推给前端播放——长回复的出声延迟
# 与全文长度解耦，只剩「首句 LLM 产出 + 首句合成」（2 秒级）。
#
# 零件复用（不重造轮子）：切句法则与 mouth._split_sentences 一致（句尾标点
# 触发、<8 字碎片并入下句）；单句合成用 mouth._synthesize_sentence_to_file
# （GLM-TTS→edge 两级链）；系统提示/历史/模型配置直接读 brain 的纯函数。
#
# 分流契约：只有「纯闲聊/问答」走流式。规则意图（提醒落库、记忆、条件触发、
# 工具执行、复合任务、退出、待确认）在 brain.think 内有确定性行为，流式直答
# 会绕过它们——预检命中一律回退整段（fallback 事件），行为与 /api/chat 一致。

# 短句碎片阈值：与 mouth 保持一致（模块不可用时取默认 8）
_STREAM_MIN_SENTENCE_CHARS = getattr(mouth, "_MIN_SENTENCE_CHARS", 8) if mouth else 8


class _SentenceStreamer:
    """增量切句器：与 mouth._split_sentences 同一刀法（句尾标点触发，<8字碎片并入下句）。

    feed(piece) 喂入 LLM 增量文本，返回新凑齐的完整句列表（可空）；
    flush() 取流尾残余（短回复在此凑成唯一一句），无残余返回 None。
    第一性原理：句尾标点是「可以开口说」的最小信号——等整段是等全文最后一个
    标点，等句级只等本句最后一个标点。
    """

    _ENDINGS = "。！？；!?;\n"  # 与 mouth._split_sentences 的切分字符一致

    def __init__(self) -> None:
        self._buf = ""    # 未成句的累积文本
        self._carry = ""  # 过短碎片暂存（并入下一句，与 mouth 合并规则一致）

    def feed(self, piece: str) -> list:
        out = []
        self._buf += piece
        while True:
            cut = -1
            for i, ch in enumerate(self._buf):
                if ch in self._ENDINGS:
                    cut = i
                    break
            if cut < 0:
                break
            sentence = self._buf[:cut + 1]
            self._buf = self._buf[cut + 1:]
            merged = (self._carry + sentence).strip()
            if len(merged) < _STREAM_MIN_SENTENCE_CHARS:
                self._carry = merged  # 碎片继续暂存，等下一句并入（防启播断续感）
            else:
                out.append(merged)
                self._carry = ""
        return out

    def flush(self):
        tail = (self._carry + self._buf).strip()
        self._carry = ""
        self._buf = ""
        return tail or None


def _sse_encode(obj: dict) -> bytes:
    """把事件对象编码为一行 SSE 帧（'data: <json>\\n\\n'，UTF-8）。"""
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _stream_hit_rule_intent(text: str) -> bool:
    """流式分流预检（只读 brain 的纯函数/常量，零副作用，不改 brain 一个字节）。

    命中大脑规则层意图（退出/待确认/复合/记忆/提醒/触发/规则工具/搜写通道）
    的输入必须走 brain.think 的确定性路径——流式直答会绕过提醒落库、工具执行
    等行为。预检本身异常时保守返回 True（回退整段，绝不让对话挂掉）。
    """
    try:
        if getattr(brain, "_pending_shell", None) is not None:
            return True  # 有待确认事项：确认/取消必须进 brain 的状态机
        if any(p in text for p in getattr(brain, "_EXIT_PATTERNS", ())):
            return True  # 退出意图：走 _chat 的道别语 + exit 契约
        if brain._is_composite(text):
            return True  # 复合任务：分层规划器/Agent 循环，不可流式直答
        # 记忆意图（与 _handle_memory_intent 同触发词，只判不执行）
        if ("记住" in text
                or any(k in text for k in getattr(brain, "_RECALL_KEYS", ()))
                or any(k in text for k in getattr(brain, "_FORGET_KEYS", ()))):
            return True
        # 提醒意图（提醒我 / 设…提醒 / 闹钟叫醒 / 提醒查询）
        if "提醒我" in text or any(k in text for k in getattr(brain, "_REMIND_LIST_KEYS", ())):
            return True
        if re.search(r"设(?:一个|个|置|一下)?.+?的?提醒", text):
            return True
        if brain._extract_alarm_raw(text) is not None:
            return True
        # 条件触发意图（触发词+动作词双闸门，与 brain 同一判定）
        if any(k in text for k in getattr(brain, "_TRIGGER_LIST_KEYS", ())):
            return True
        if (any(k in text for k in getattr(brain, "_TRIGGER_KEYS", ()))
                and any(k in text for k in getattr(brain, "_TRIGGER_ACTION_KEYS", ()))):
            return True
        # 规则工具意图（时间/媒体/打开/搜索/文件/命令，只解析不执行）
        if brain._parse_intent(text) is not None:
            return True
        # 「搜 X 写到 F」快速通道
        if brain._parse_search_write(text) is not None:
            return True
        # PPT 纠正轮（不对/换个视角/不，是PPT）：brain 内有确定性路由重做
        if brain._ppt_correction_route(text) is not None:
            return True
        return False
    except Exception as e:
        print(f"[server] 流式预检异常（保守回退整段）：{e}")
        return True


def _stream_llm_ready() -> bool:
    """流式所需的大模型配置是否齐备（有 api_key 即可）；异常按不可用处理。"""
    try:
        cfg = brain._load_llm_config()
        return bool(cfg.get("api_key"))
    except Exception:
        return False


def _synth_sentence_url(text: str):
    """单句合成 → 可服务 URL（'/api/tts/<sha1>.wav|.mp3'）；全链失败返回 None。

    三级保障：
      ① sha1 缓存命中零合成（毫秒级）；
      ② mouth._synthesize_sentence_to_file（GLM-TTS→edge 两级链），产物从系统
         临时目录搬进 TTS 缓存目录并按 sha1 命名——与既有缓存契约一致，
         _serve_tts 可直接服务，同文本下次直接命中；
      ③ 两级全失败回退 synth_for（追加 SAPI 离线兜底）——单句失声可能性压到最低。
    """
    text = (text or "").strip()
    if not text:
        return None
    hit = _tts_cached_url(text)
    if hit:
        return hit
    tmp = None
    if mouth is not None and hasattr(mouth, "_synthesize_sentence_to_file"):
        try:
            tmp = mouth._synthesize_sentence_to_file(text)
        except Exception as e:
            print(f"[server] 句级合成异常（回退 synth_for）：{e}")
            tmp = None
    if tmp:
        ext = os.path.splitext(tmp)[1].lower()
        if ext in (".wav", ".mp3"):
            name = hashlib.sha1(text.encode("utf-8")).hexdigest() + ext
            dest = os.path.join(_TTS_CACHE_DIR, name)
            try:
                os.makedirs(_TTS_CACHE_DIR, exist_ok=True)
                if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    os.remove(tmp)  # 并发下已有同文本产物：用现成的，丢弃重复品
                else:
                    shutil.move(tmp, dest)
                return "/api/tts/" + name
            except OSError as e:
                print(f"[server] 句级产物入库失败（回退 synth_for）：{e}")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
    # 兜底：synth_for 自带 GLM→edge→SAPI 三级链（声音必达底线不破）
    return synth_for(text)


def _llm_stream_worker(user_text: str, history: list, splitter: _SentenceStreamer,
                       sentence_q: "queue.Queue", event_q: "queue.Queue",
                       cancel: threading.Event) -> None:
    """LLM 流式消费线程：GLM stream=True 边收 token 边推 delta、边切句边投合成队列。

    回退契约：
      - 回复以 '{' 开头（系统提示约束下的工具 JSON）→ 发 abort，整段回退
        brain.think（此时无任何 delta 出场，回退不会重复发声）；
      - 网络/格式异常：未产出任何内容 → abort 整段回退；已有产出 → 按现有内容
        收尾（done 事件由收尾逻辑发），绝不把对话挂掉；
      - 消息构造与 brain._think_via_llm 同配方（系统提示 + 近 10 轮历史 +
        extra_body 透传），只读 brain 纯函数，不改 brain。
    """
    full = ""
    emitted = False  # 是否已有 delta 出场（决定失败时能否安全整段回退）
    try:
        cfg = brain._load_llm_config()
        base_url = (cfg.get("base_url") or "").rstrip("/")
        model = cfg.get("model") or "glm-5.2"
        messages = [{"role": "system", "content": brain._build_system_prompt()}]
        for turn in (history or [])[-getattr(brain, "_HISTORY_TURNS", 10):]:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        payload = {"model": model, "messages": messages, "temperature": 0.7, "stream": True}
        # extra_body 透传（与 brain 同一扩展点，如关闭思考型推理加速）
        extra_body = cfg.get("extra_body")
        if extra_body:
            try:
                extra = json.loads(extra_body)
                if isinstance(extra, dict):
                    payload.update(extra)
            except ValueError:
                pass  # 配置写错时静默忽略，不阻断对话（与 brain 一致）
        headers = {"Authorization": f"Bearer {cfg['api_key']}",
                   "Content-Type": "application/json"}
        import httpx  # 既有依赖，局部导入与模块风格一致
        with httpx.stream("POST", base_url + "/chat/completions",
                          json=payload, headers=headers,
                          timeout=getattr(brain, "_API_TIMEOUT", 60.0)) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if cancel.is_set():
                    return  # 前端已打断/断开：即刻止损，不再烧 token
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue  # 心跳/畸形分片跳过，不中断整流
                piece = delta.get("content") or ""
                if not piece:
                    continue  # reasoning_content 等思考分片：不上字幕、不出声
                full += piece
                # 工具 JSON 侦测：回复以 '{' 开头必是执行指令，流式无法执行工具——
                # 立即中止，整段回退 brain.think 走 Agent 循环
                if full.lstrip().startswith("{"):
                    print("[server] 流式检出工具 JSON，中止流式并整段回退。")
                    event_q.put(("abort", "tool-json"))
                    return
                emitted = True
                event_q.put(("delta", piece))
                for s in splitter.feed(piece):
                    sentence_q.put(s)
    except Exception as e:
        print(f"[server] LLM 流式异常：{type(e).__name__}: {e}")
        if not emitted:
            event_q.put(("abort", "stream-failed"))
            return
        print("[server] 已有部分内容出场，按现有内容收尾（degraded）。")
    finally:
        # 流尾残余作为最后一句投递（闲聊短回复在此凑成唯一一句，零额外延迟）
        try:
            tail = splitter.flush()
            if tail and not cancel.is_set():
                sentence_q.put(tail)
        except Exception:
            pass
        sentence_q.put(None)  # 生产结束哨兵（任何退出路径都送达，防生产线程干等）
        if full:
            event_q.put(("llm_done", full))
        else:
            # 空回复（流正常结束却无一字）：等同失败，整段回退
            event_q.put(("abort", "empty"))


def _tts_stream_producer(sentence_q: "queue.Queue", event_q: "queue.Queue",
                         cancel: threading.Event) -> None:
    """TTS 生产线程（单线程保序）：逐句合成，合成好一句立刻推 sentence 事件。

    为什么单线程：句子的出声顺序必须等于语序——多生产者并发合成会让
    完成顺序乱掉（短句后来先出），前端队列播放就会倒序。GLM-TTS 实测
    ~26ms/字，单句合成远快于单句播放，单生产者始终跑在播放前面。
    """
    while True:
        sentence = sentence_q.get()
        if sentence is None:
            break  # 哨兵：LLM 侧已投完全部句子
        if cancel.is_set():
            continue  # 已取消：排空队列直至哨兵，不再合成
        url = _synth_sentence_url(sentence)
        # 回声登记：打断侦测以「正在播什么」为判定依据（与 /api/chat 登记语义一致）
        try:
            _note_speaking(sentence)
            _wake_pause_for(2 + len(sentence) * 0.3)
        except Exception:
            pass
        event_q.put(("sentence", (sentence, url)))
    event_q.put(("tts_done", None))


def _fallback_with_progress(handler, user_text: str) -> None:
    """整段回退（fallback 事件）+ 工具执行进度实时推送（progress 事件）。

    为什么线程化：make_ppt 等大项目执行约 2 分钟，若在处理器线程同步执行
    _chat（brain.think → hands.execute 全链），SSE 长时间静默、前端只能看到
    一句话然后空等。改为：
      工作线程   —— 跑 _chat 整段路径；执行期间 ppt_maker 等模块往
                    jarvis/progress 进度总线 emit 埋点事件；
      处理器线程 —— 每 0.3 秒 drain 一次总线，把进度事件实时写进 SSE；
                    工作线程完成后照常下发 fallback 事件（契约不变）。
    单一写者原则不变：仍然只有处理器线程碰 wfile，杜绝交错写帧。
    模块级函数（非 NolanHandler 方法）：只依赖 handler._sse_send 表面，
    与 _llm_stream_worker / _tts_stream_producer 同一可测形态。
    """
    if _progress is None:
        # 进度总线缺席：行为与接线前完全一致（同步整段回退）
        handler._sse_send({"type": "fallback", **_chat(user_text)})
        return
    _progress.begin()   # 订阅开始：ppt_maker 等的 emit 从此进队列
    box = {}
    done = threading.Event()

    def _run():
        try:
            box["result"] = _chat(user_text)
        except Exception as e:
            # 异常装箱上交：由处理器线程走 _handle_chat_stream 既有 error 事件路径
            box["error"] = e
        finally:
            done.set()  # 任何退出路径都置位，防处理器线程干等

    threading.Thread(target=_run, daemon=True,
                     name="chat-fallback-exec").start()
    try:
        while not done.wait(0.3):
            for ev in _progress.drain():
                handler._sse_send({"type": "progress", **ev})
        # 收尾排空：工作线程已完成，最后一批进度必须先于 fallback 出场
        for ev in _progress.drain():
            handler._sse_send({"type": "progress", **ev})
        if "error" in box:
            raise box["error"]
        handler._sse_send({"type": "fallback",
                           **(box.get("result") or {"reply": "", "audio_url": None})})
    finally:
        _progress.end()   # 任何路径（含客户端断开）都退订，不泄漏到下一轮


# == 条件触发后台检查（P4 · 主动性进阶）==
# 为什么独立线程：条件评估要走 LLM 联网搜索（秒级），放进 /api/due 会挂住
# 15 秒轮询、占满浏览器连接池（此前 TTS 同步合成挂死全链路的教训）。
# 后台线程每分钟评估一轮，触发的消息进队列，/api/due 出队即走（毫秒级）；
# 音箱通道同步播报两遍（闹钟语义：必被听见）。
_trigger_fired = _collections.deque(maxlen=20)
_trigger_fired_lock = threading.Lock()

# Gap4 主动性：用户最近活动时刻（epoch 秒），供 should_initiate 的
# 「用户刚交互过不打扰」闸门判定；_chat 与流式入口每轮对话更新
_last_user_activity = 0.0
_last_initiative = 0.0


def _trigger_loop() -> None:
    """条件触发检查守护线程：定期评估 triggers，触发消息入队 + 音箱播报。"""
    global _last_initiative  # 开口记账（行内赋值），缺此声明会让上面的读取变 UnboundLocalError
    import triggers
    print("[triggers] 条件触发检查线程启动（每分钟一轮）。")
    while True:
        try:
            def _exec(cmd: str) -> str:
                # 执行型动作经大脑跑完整工具链（与 /api/chat 同锁串行化）
                with _brain_lock:
                    return brain.think(cmd, [])
            msgs = triggers.check_due(
                executor=_exec, evaluator=brain.eval_condition)
            if msgs:
                with _trigger_fired_lock:
                    for m in msgs:
                        _trigger_fired.append(m)
                # 语音与闹钟提醒同一路径：/api/due 出队时暖 TTS + 音箱播报两遍，
                # 此处不单独播报（否则与 /api/due 的播报叠音）
        except Exception as e:
            print("[triggers] 检查异常（下轮继续）：%s" % e)
        # Gap4 主动性：触发器之外，Nolan 也会基于记忆主动开口。
        # 与触发器共用同一条出队通道（/api/due），不新增投递路径；
        # 三重闸门（速率/安静时段/用户刚交互）在 proactive 内部把关，
        # 这里只负责「该不该开口 → 生成 → 入队」，生成失败沉默不打扰。
        if proactive is not None and memory_v2 is not None:
            try:
                ctx = {
                    "now": time.time(),
                    "last_user_activity": _last_user_activity,
                    "last_initiative": _last_initiative,
                    "profile": memory_v2.profile_summary(),
                    "due_messages": list(_trigger_fired),
                }
                # H2 预判素材：统计模式识别（episodic 时间线 + habit 记忆）
                # → 当日预判 → 高置信标记，注入 context 供生成与闸门使用
                try:
                    _patterns = proactive.detect_patterns()
                    ctx["patterns"] = _patterns
                    _ant = proactive.current_anticipation(ctx, _patterns)
                    if _ant:
                        ctx["anticipation"] = _ant
                        ctx["high_confidence_anticipation"] = any(
                            proactive.high_confidence(p) for p in _patterns)
                except Exception as _e:
                    print("[proactive] 模式识别异常（本轮跳过预判）：%s" % _e)
                if proactive.should_initiate(ctx):
                    msg = proactive.generate_initiative(ctx, brain.glm_one_shot)
                    if msg:
                        with _trigger_fired_lock:
                            _trigger_fired.append(msg)
                        _last_initiative = time.time()
                        print("[proactive] 主动开口：%s" % msg[:40])
            except Exception as e:
                print("[proactive] 主动性评估异常（下轮继续）：%s" % e)
        time.sleep(60)


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

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    download_name: str = None) -> None:
        """发送二进制响应（用于 mp3 音频 / 图片 / 文件下载），带 CORS 头。
        download_name 非空时按附件下载（RFC 5987 编码文件名，兼容中文名）。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        if download_name:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(download_name))
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
        从 jarvis/files/ 目录返回文件：图片（jpg/jpeg/png/webp）内联展示；
        下载白名单（pdf/docx/pptx/txt/md/csv 及文本类后缀）以附件形式下载。
        防路径穿越：拒绝 '..'、绝对路径、盘符（':'）；解析后的绝对路径必须落在
        files 目录内；只允许白名单后缀。不合规一律 404，绝不泄露任意文件。
        """
        name = unquote(raw_name)
        full = _resolve_files_path(name)
        if full is None:
            self._send_error_json(404, f"未知路径：/api/files/{raw_name}")
            return
        ext = os.path.splitext(name)[1].lower()
        inline_mime = _IMAGE_MIME.get(ext)
        download_mime = _DOWNLOAD_MIME.get(ext)
        if inline_mime is None and download_mime is None:
            self._send_error_json(404, f"不支持的文件类型：{name}")
            return
        if not os.path.isfile(full):
            self._send_error_json(404, f"文件不存在：{name}")
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_error_json(500, f"读取文件失败了：{e}")
            return
        if inline_mime is not None:
            self._send_bytes(200, data, inline_mime)
        else:
            # 下载：附件形式下发，文件名带原始 basename（RFC 5987 兼容中文名）
            self._send_bytes(200, data, download_mime,
                             download_name=os.path.basename(name))

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

    # -- 文件入口（拖拽上传阅读）--

    def _handle_upload(self) -> None:
        """POST /api/upload：base64 JSON 上传（契约见文件头 API 契约段）。
        落盘 jarvis/files/uploads/ 并抽取文本返回；安全闸门在 _save_upload。"""
        # 粗闸门：请求体超限直接拒，并断开连接避免残留字节污染 keep-alive
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._send_error_json(400, "请求体为空。")
            return
        if length > _UPLOAD_MAX_BODY_BYTES:
            self.close_connection = True
            self._send_error_json(413, "文件超过大小上限（50MB）。")
            return
        try:
            data = self._read_json_body()
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        name = str(data.get("name", "") or "")
        b64 = str(data.get("data_base64", "") or "")
        if not name or not b64:
            self._send_error_json(400, "请求缺少 name 或 data_base64 字段。")
            return
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            self._send_error_json(400, "data_base64 不是合法的 base64 数据。")
            return
        try:
            stored, result = _save_upload(name, raw)
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        text = result.get("text", "")
        self._send_json(200, {
            "ok": True,
            "name": stored,
            "kind": result.get("kind", "二进制"),
            "chars": len(text),
            "excerpt": text[:_EXCERPT_CHARS],
            # 全量抽取文本：发送时拼进对话 payload 用（前端按 8000 字截断）
            "text": text,
            "meta": result.get("meta", {}),
            "note": result.get("note", ""),
            "truncated": bool(result.get("meta", {}).get("truncated")),
            "file_url": "/api/files/" + quote("uploads/" + stored, safe="/"),
        })

    # -- 静默访问日志（保持控制台干净，可按需打开）--
    def log_message(self, fmt, *args):
        pass

    # -- 句级流式对话（SSE）--

    def _sse_send(self, obj: dict) -> None:
        """写一条 SSE 事件。wfile 无缓冲（wbufsize=0）直达 socket，写完即出场。
        客户端断开时抛 BrokenPipeError/ConnectionResetError，由调用方取消流水线。"""
        self.wfile.write(_sse_encode(obj))

    def _handle_chat_stream(self) -> None:
        """POST /api/chat/stream：句级流式对话（事件契约见文件头 API 契约段）。

        线程分工（单一写者原则：只有本处理器线程碰 wfile，杜绝交错写帧）：
          LLM 线程   —— 边收 token 边把 delta/abort/llm_done 投进 event_q，
                        凑齐的句子投进 sentence_q；
          TTS 线程   —— 单线程保序合成，sentence/tts_done 投进 event_q；
          本线程     —— 从 event_q 取事件逐条写 SSE，两个 done 齐后写 done 收尾。
        任何写失败（客户端断开）→ 置 cancel，两线程各自止损退出（守护线程）。
        """
        global _history, _last_user_activity  # 历史落账在方法尾部；方法前部仅读快照
        _last_user_activity = time.time()  # Gap4：主动性「刚交互过不打扰」闸门依据
        try:
            data = self._read_json_body()
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        user_text = str(data.get("text", "") or "").strip()

        # SSE 响应头：无 Content-Length，Connection: close 让「关闭即 EOF」，
        # 绕开标准库不支持 chunked 的短板；ThreadingHTTPServer 每连接一线程，关闭无碍
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        cancel = threading.Event()
        try:
            # 分流：空文本 / 规则意图 / 无大模型配置 → 与 /api/chat 完全相同的整段路径，
            # 以 fallback 事件下发（前端按普通回复处理，行为一字不差）；
            # 工具执行线程化：执行期间进度事件实时推送（make_ppt 不再静默空等）
            if (not user_text or _stream_hit_rule_intent(user_text)
                    or not _stream_llm_ready()):
                _fallback_with_progress(self, user_text)
                return

            # 新消息进场先打断音箱正在进行的播报（与 _chat 同一语义）
            if mouth is not None:
                try:
                    mouth.interrupt()
                except Exception:
                    pass

            sentence_q: "queue.Queue" = queue.Queue()
            event_q: "queue.Queue" = queue.Queue()
            splitter = _SentenceStreamer()
            threading.Thread(
                target=_llm_stream_worker,
                args=(user_text, list(_history), splitter, sentence_q, event_q, cancel),
                daemon=True, name="chat-stream-llm",
            ).start()
            threading.Thread(
                target=_tts_stream_producer,
                args=(sentence_q, event_q, cancel),
                daemon=True, name="chat-stream-tts",
            ).start()

            reply = ""
            llm_done = False
            tts_done = False
            while not (llm_done and tts_done):
                kind, payload = event_q.get()
                if kind == "delta":
                    self._sse_send({"type": "delta", "text": payload})
                elif kind == "sentence":
                    self._sse_send({"type": "sentence", "text": payload[0],
                                    "audio_url": payload[1]})
                elif kind == "abort":
                    # 流式早期失败/工具 JSON/空回复：整段回退（此刻无任何内容出场，
                    # _chat 重走 brain.think + synth_for，不会重复发声）；
                    # 工具执行线程化：执行期间进度事件实时推送
                    cancel.set()
                    _fallback_with_progress(self, user_text)
                    return
                elif kind == "llm_done":
                    reply = payload
                    llm_done = True
                elif kind == "tts_done":
                    tts_done = True

            # 显示层终态卫生：流式全文绕开 brain 出口闸，「人话+行尾工具 JSON」
            # 混排会原样漏进来——落历史与 done 事件之前统一剥离（中间态 delta 不动）
            reply = _display_clean(reply)
            # 历史落账：与 /api/chat 同一把锁串行化、同一裁剪规则；
            # 同样写入存根版（附件全文不躺历史，当前轮全文已发给 LLM）
            with _brain_lock:
                _history.append({"role": "user", "content": _slim_for_history(user_text)})
                _history.append({"role": "assistant", "content": reply})
                if len(_history) > _HISTORY_MAX_TURNS * 2:
                    _history = _history[-_HISTORY_MAX_TURNS * 2:]
                _save_history()  # 写穿落盘：server 重启后大脑仍记得这轮对话
            self._sse_send({"type": "done", "reply": reply})
        except (BrokenPipeError, ConnectionResetError):
            cancel.set()  # 客户端断开：止损，LLM/TTS 线程各自退出
        except Exception as e:
            cancel.set()
            print(f"[server] 流式对话处理异常：{type(e).__name__}: {e}")
            # 响应头已发，只能尽力补一条 error 事件，前端按失败提示处理
            try:
                self._sse_send({"type": "error",
                                "message": f"流式对话出错了：{e}"})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    # -- 方法分发 --

    def do_OPTIONS(self):
        """CORS 预检。"""
        self._send_json(200, {"ok": True})

    def _serve_static(self, path: str) -> bool:
        """托管 vite 构建产物 dist/（软件形态）。命中返回 True，未命中/越界返回 False。

        - 路径穿越一律不服务（realpath 必须落在 dist 内）；
        - 无扩展名或目录路径按 SPA 回退 index.html（前端路由）；
        - 带 hash 的 assets 长缓存，index.html 不缓存（发版即生效）；
        - dist 不存在（未构建）时返回 False，调用方走 404，绝不影响 /api。
        """
        try:
            rel = path.lstrip("/")
            full = os.path.normpath(os.path.join(_DIST_DIR, rel))
            if os.path.commonpath([os.path.abspath(full), os.path.abspath(_DIST_DIR)]
                                  ) != os.path.abspath(_DIST_DIR):
                return False
            if not os.path.isfile(full):
                if "." in os.path.basename(rel):   # 有扩展名的缺失文件：真 404
                    return False
                full = os.path.join(_DIST_DIR, "index.html")  # SPA 回退
                if not os.path.isfile(full):
                    return False
            ext = os.path.splitext(full)[1].lower()
            mime = _STATIC_MIME.get(ext)
            if mime is None:
                return False
            with open(full, "rb") as f:
                blob = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            if os.path.basename(full) == "index.html" or "/assets/" not in full.replace(os.sep, "/"):
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(blob)
            return True
        except Exception:
            return False

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
                # P4 条件触发：后台线程评估好的触发消息随本通道出队（毫秒级，不阻塞）
                with _trigger_fired_lock:
                    fired = list(_trigger_fired)
                    _trigger_fired.clear()
                messages += fired
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
            elif path == "/api/greeting":
                # 主动晨报：每天首次打开网页时的问候语 + 语音
                payload = _greeting_payload()
                if payload.get("text"):
                    _note_speaking(payload["text"])  # 回声判定依据
                    _wake_pause_for(2 + len(payload["text"]) * 0.3)
                self._send_json(200, payload)
            elif path == "/api/wake/state":
                self._send_json(200, {
                    "enabled": _wake_enabled,
                    "listening": bool(_wake_thread and _wake_thread.is_alive()),
                })
            elif path == "/api/wake/events":
                # 事件出队：前端 2.5 秒轮询。
                # wake 事件 → 确认音「在的，先生，请讲。」；
                # bargein 事件 → 带上听到的指令文本，前端停播报并自动发送该指令
                with _wake_events_lock:
                    evs = list(_wake_events)
                    _wake_events.clear()
                out = []
                for ev in evs:
                    if ev.get("kind") == "bargein" and ev.get("text"):
                        out.append({"kind": "bargein", "text": ev["text"],
                                    "audio_url": None})
                    else:
                        out.append({"kind": "wake", "text": _WAKE_ACK,
                                    "audio_url": _tts_cached_url(_WAKE_ACK)})
                if any(ev.get("kind") != "bargein" for ev in evs):
                    _warm_tts_async(_WAKE_ACK)
                    _note_speaking(_WAKE_ACK)  # 确认音也是播报声，纳入回声判定
                self._send_json(200, {"events": out})
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
            elif path == "/api/files_list":
                # 文件柜：递归列出 jarvis/files/（含 uploads/），mtime 倒序
                self._send_json(200, _files_list_payload())
            elif path.startswith("/api/files/"):
                self._serve_file(path[len("/api/files/"):])
            else:
                # 非 /api 路径：尝试静态托管（dist/ 软件形态），未命中才 404
                if not self._serve_static(path):
                    self._send_error_json(404, f"未知路径：{path}")
        except Exception as e:
            self._send_error_json(500, f"服务器处理请求时出错了：{e}")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/chat/stream":
                # 句级流式对话（SSE）：LLM 边想、TTS 边产、前端边播
                self._handle_chat_stream()
            elif path == "/api/upload":
                # 文件入口：base64 JSON 上传 → 落盘 uploads/ + 抽取文本
                self._handle_upload()
            elif path == "/api/chat":
                try:
                    data = self._read_json_body()
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                # 进度总线卫生：本路径不接进度（行为同现状），但执行期间
                # ppt_maker 等会 emit——begin/end 包住执行段，保证事件
                # 不残留、不泄漏到下一轮流式请求
                if _progress is not None:
                    _progress.begin()
                try:
                    result = _chat(str(data.get("text", "") or ""))
                finally:
                    if _progress is not None:
                        _progress.end()
                self._send_json(200, result)
            elif path == "/api/wake/toggle":
                # 唤醒词开关：{"enabled": true|false}，状态落盘重启保留
                try:
                    data = self._read_json_body()
                except ValueError as e:
                    self._send_error_json(400, str(e))
                    return
                self._send_json(200, _wake_set_enabled(data.get("enabled")))
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
            elif path == "/api/stop":
                # 立即打断服务端音箱当前播报（mouth 为 None 时静默成功）
                self._discard_body()  # 读净请求体，防止残留字节污染 keep-alive 连接
                if mouth is not None:
                    try:
                        mouth.interrupt()
                    except Exception as e:
                        print(f"[server] 打断播报失败（已静默）：{e}")
                self._send_json(200, {"stopped": True})
            else:
                self._send_error_json(404, f"未知路径：{path}")
        except Exception as e:
            self._send_error_json(500, f"服务器处理请求时出错了：{e}")


def main() -> None:
    port = 7901
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"[server] 端口参数无效：{sys.argv[1]}，使用默认 7901")

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
    _load_history()     # 恢复对话历史：server 重启不失忆（伙伴纪律）
    threading.Thread(target=_trigger_loop, daemon=True,
                     name="trigger-checker").start()  # P4 条件触发后台检查
    if _wake_load_state():  # 唤醒词开关状态落盘：上次开启过则自动恢复耳蜗
        _wake_set_enabled(True)
        print("[server] 唤醒词耳蜗已按落盘状态自动开启（说「诺兰」即可唤醒）")
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
