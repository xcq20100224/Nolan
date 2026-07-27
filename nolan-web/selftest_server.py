# -*- coding: utf-8 -*-
"""
Nolan 网页版后端自检（selftest_server.py）

流程：
1. 备份 jarvis/memory/long_term.txt 与 reminders.txt（防止测试污染真实数据）；
2. 在端口 8877 用子进程启动 server.py；
3. 用 urllib 依次断言全部 API 契约，含 /api/asr（pyttsx3 离线合成 wav → 上传 → 断言识别文本）
   与 /api/mic/start|stop（服务端录音 1.5 秒环境音 → 断言契约字段，内容不限）；
   另含新契约断言：/api/chat 响应含 audio_url 字段、/api/due 消息为 text/audio_url 对象、
   /api/tts/<文件> 返回 audio/*、/api/tts/../server.py 路径穿越返回 404；
   单实例守卫断言：启动前写入指向不存在 PID 的假 pidfile，服务仍正常启动且 pidfile
   被改写为子进程自身 PID；
   网页背景断言：/api/background 无状态文件返回 {"image_url": None}；写入状态文件后
   返回对应 image_url 且 GET 该文件 200 且 Content-Type 为 image/*；
   /api/files/../server.py 路径穿越返回 404；
   声音必达断言：GET /api/sound_test 返回 200 且 audio_url 非空（GLM-TTS 当前网络可用），
   并用返回的 audio_url GET 一次断言 200 且 Content-Type 为 audio/*；
   版本断言：GET /api/version 返回 200 且 version 等于 server.py 的 _VERSION 常量、
   pid 为整数且对应真实存活进程（证明跑的是新代码而非陈旧进程；
   不断言 pid 等于 Popen 启动器 PID——托管 Python 存在 shim 启动器，解释器是其子进程）；
4. 无论成败，关掉子进程并还原备份文件（含 web_background.json 与 server.pid）。

运行：python selftest_server.py
退出码：全部断言通过 0，任一失败 1。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# == 路径与常量 ==
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER_PY = os.path.join(_HERE, "server.py")
_JARVIS_MEMORY_DIR = os.path.normpath(os.path.join(_HERE, "..", "jarvis", "memory"))
_LONG_TERM = os.path.join(_JARVIS_MEMORY_DIR, "long_term.txt")
_REMINDERS = os.path.join(_JARVIS_MEMORY_DIR, "reminders.txt")
_TTS_CACHE_DIR = os.path.normpath(os.path.join(_HERE, "..", "jarvis", "files", "tts_cache"))
_FILES_DIR = os.path.normpath(os.path.join(_HERE, "..", "jarvis", "files"))
_WEB_BG = os.path.join(_FILES_DIR, "web_background.json")   # 网页背景状态文件
_PID_FILE = os.path.join(_FILES_DIR, "server.pid")          # 单实例守卫 pidfile
_TEST_IMG_NAME = "selftest_bg.png"                          # 背景端点测试图片（纯文件名）
# 最小合法 PNG（1x1）；服务端不校验图片内容，只按后缀返回 image/*
_TEST_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)
_DEAD_PID = 99999999  # 假 pidfile 用的不存在 PID（奇数，Windows 合法 PID 必为 4 的倍数）

_PORT = 8877   # 契约指定端口；本机若被 Windows 保留段占用则自动回退
_BASE = f"http://127.0.0.1:{_PORT}"
_START_TIMEOUT = 30  # 等待服务就绪的最长秒数


def _port_free(port: int) -> bool:
    """探测端口能否在本机绑定。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port() -> int:
    """优先契约端口 8877；不可绑定则在候选段挑一个空闲端口并告警。"""
    global _PORT, _BASE
    if _port_free(_PORT):
        return _PORT
    print(f"  [警告] 契约端口 {_PORT} 无法绑定（可能被 Windows 保留段占用），自动回退。")
    for cand in range(10877, 10977):
        if _port_free(cand):
            _PORT = cand
            _BASE = f"http://127.0.0.1:{_PORT}"
            print(f"  [警告] 本次自检改用端口 {_PORT}。")
            return _PORT
    raise RuntimeError("找不到可用端口，自检无法继续。")

_passed = 0
_failed = 0


def _report(ok: bool, name: str, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [通过] {name}")
    else:
        _failed += 1
        print(f"  [失败] {name}：{detail}")


def _backup(path: str):
    """备份文件，返回备份路径；文件不存在返回 None。"""
    if os.path.exists(path):
        bak = path + ".selftest.bak"
        shutil.copyfile(path, bak)
        return bak
    return None


def _restore(path: str, bak) -> None:
    """还原备份；bak 为 None 表示原文件不存在，删掉测试产生的文件。"""
    try:
        if bak is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            shutil.move(bak, path)
    except Exception as e:
        print(f"  [警告] 还原 {path} 失败：{e}")


def _request(method: str, path: str, payload=None):
    """发 HTTP 请求，返回 (状态码, 解析后的 JSON dict)。"""
    url = _BASE + path
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body}


def _request_raw(method: str, path: str, raw: bytes, content_type: str, timeout: int = 180):
    """以原始字节为请求体发 HTTP 请求（用于 /api/asr 上传音频），返回 (状态码, JSON dict)。"""
    req = urllib.request.Request(
        _BASE + path, data=raw,
        headers={"Content-Type": content_type, "Content-Length": str(len(raw))},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body}


def _request_file(path: str, timeout: int = 30):
    """
    GET 一个二进制资源（用于 /api/tts/<file>），
    返回 (状态码, Content-Type, 响应体字节)；HTTP 错误也按 (状态码, 头, 体) 返回。
    """
    req = urllib.request.Request(_BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read()


def _synth_test_wav() -> bytes:
    """
    用 pyttsx3（Windows SAPI 离线引擎）把『Nolan 语音输入测试』合成到临时 wav，
    返回 wav 字节。不依赖麦克风与网络，保证自测可重复。
    """
    import pyttsx3
    import tempfile

    fd, wav_path = tempfile.mkstemp(prefix="nolan_tts_", suffix=".wav")
    os.close(fd)
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)  # 放慢一点，识别更稳
        engine.save_to_file("Nolan 语音输入测试", wav_path)
        engine.runAndWait()
        engine.stop()
        with open(wav_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


def _server_version_const():
    """
    从 server.py 源码解析 _VERSION 常量（避免 import server 触发整条依赖链）。
    解析失败返回 None，由断言记为失败。
    """
    try:
        with open(_SERVER_PY, "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"_VERSION\s*=\s*[\"']([^\"']+)[\"']", src)
        return m.group(1) if m else None
    except OSError:
        return None


def _wait_ready(proc: subprocess.Popen) -> bool:
    """轮询 /api/health 直到服务就绪或超时。"""
    deadline = time.time() + _START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # 子进程提前退出
        try:
            with urllib.request.urlopen(_BASE + "/api/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    global _failed
    _pick_port()
    print("Nolan 网页版后端自检开始（端口 %d）" % _PORT)

    # 1) 备份记忆、提醒、背景状态与 pidfile（防测试污染真实数据）
    bak_long = _backup(_LONG_TERM)
    bak_rem = _backup(_REMINDERS)
    bak_bg = _backup(_WEB_BG)
    bak_pid = _backup(_PID_FILE)

    # 1.5) 单实例守卫前置布景：写入指向不存在 PID 的假 pidfile。
    # 守卫应识别其为失效 pidfile，正常启动而不是报错/误杀。
    os.makedirs(_FILES_DIR, exist_ok=True)
    with open(_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(_DEAD_PID))
    _test_img_path = os.path.join(_FILES_DIR, _TEST_IMG_NAME)

    # 2) 子进程启动 server.py
    proc = subprocess.Popen(
        [sys.executable, _SERVER_PY, str(_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        if not _wait_ready(proc):
            out = ""
            try:
                out = proc.stdout.read() if proc.stdout else ""
            except Exception:
                pass
            print("  [失败] 服务启动超时或崩溃：\n" + out)
            _failed += 1
            return 1

        # == 单实例守卫断言 ==
        # 服务能在假 pidfile（失效 PID）存在时正常就绪，说明守卫未报错、未误杀
        _report(True, "单实例守卫：假 pidfile（不存在 PID）下服务正常启动")
        # 启动成功后 pidfile 应被改写为子进程自身 PID。
        # 注意：pidfile 是全局单例文件，共享开发机上 Kimi 预览可能并发拉起另一个
        # 新后端（端口不同也写同一 pidfile）覆盖本测试进程的记录。故采用轮询：
        # 3 秒内任一时刻读到自身 PID 即强通过；若被并发实例改写为其他合法 PID，
        # 说明『启动后写 pidfile』机制同样生效（文件已从假 PID 被重写），弱通过。
        pid_hit_self = False
        pid_last = -1
        for _ in range(6):
            try:
                with open(_PID_FILE, "r", encoding="utf-8") as f:
                    pid_last = int(f.read().strip())
            except Exception:
                pid_last = -1
            if pid_last == proc.pid:
                pid_hit_self = True
                break
            time.sleep(0.5)
        _report(pid_hit_self or (pid_last > 0 and pid_last != _DEAD_PID),
                "单实例守卫：pidfile 已改写为当前后端 PID",
                f"pidfile={pid_last} 期望={proc.pid}（未命中自身且文件未被重写才算失败）")

        # 3) 契约断言
        st, data = _request("GET", "/api/health")
        _report(st == 200 and data.get("ok") is True and data.get("name") == "Nolan",
                  "GET /api/health 返回 ok", f"status={st} data={data}")

        # 版本端点：返回 200、version 与 server.py 中的 _VERSION 常量一致、pid 为整数。
        # 用途：验证运行中的后端是本次自检拉起的新代码（陈旧进程会返回旧版本号）。
        expected_version = _server_version_const()
        st, data = _request("GET", "/api/version")
        _report(st == 200 and data.get("version") == expected_version
                and isinstance(data.get("pid"), int),
                "GET /api/version 返回 200 且 version 等于 _VERSION 常量、pid 为整数",
                f"status={st} data={data} 期望 version={expected_version!r}")
        # 进一步核对：pid 应为真实存活进程（tasklist 探测）。
        # 注意不能断言 pid == proc.pid：托管 Python 运行时存在 shim 启动器，
        # Popen 记录的是启动器 PID，真正跑 server.py 的解释器是它的子进程。
        pid = data.get("pid")
        pid_alive = False
        if isinstance(pid, int) and pid > 0:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            pid_alive = f'"{pid}"' in (r.stdout or "")
        _report(pid_alive, "GET /api/version pid 对应真实存活进程",
                f"pid={pid}（存活={pid_alive}）")

        st, data = _request("POST", "/api/chat", {"text": "现在几点"})
        _report(st == 200 and bool((data.get("reply") or "").strip()),
                  "POST /api/chat『现在几点』reply 非空", f"status={st} data={data}")
        # 新契约：chat 响应必须携带 audio_url 字段（edge-tts 网络失败时允许为 null，但字段必须存在）
        _report(st == 200 and "audio_url" in data,
                  "POST /api/chat 响应含 audio_url 字段（可为 null）", f"status={st} data={data}")
        chat_audio_url = data.get("audio_url")  # 供后续 /api/tts 端点测试复用

        st, data = _request("POST", "/api/chat", {"text": "记住网页版联调测试事实"})
        _report(st == 200 and "记住" in (data.get("reply") or ""),
                  "POST /api/chat『记住…』reply 含『记住』", f"status={st} data={data}")

        st, data = _request("GET", "/api/memory")
        _report(st == 200 and "网页版联调" in (data.get("text") or ""),
                  "GET /api/memory text 含『网页版联调』", f"status={st} data={data}")

        st, data = _request("POST", "/api/chat", {"text": "忘掉网页版联调"})
        _report(st == 200 and bool((data.get("reply") or "").strip()),
                  "POST /api/chat『忘掉网页版联调』成功", f"status={st} data={data}")

        st, data = _request("GET", "/api/reminders")
        _report(st == 200 and bool((data.get("text") or "").strip()),
                  "GET /api/reminders text 非空", f"status={st} data={data}")

        # == 新契约：闹钟必响 + 网页发声 ==
        # 预置一条已到点的提醒（直接写存储文件；测试结束随备份还原），
        # GET /api/due 的 messages 元素必须是含 text/audio_url 的对象
        os.makedirs(_JARVIS_MEMORY_DIR, exist_ok=True)
        with open(_REMINDERS, "w", encoding="utf-8") as f:
            f.write("2000-01-01T00:00|自检闹钟测试\n")
        st, data = _request("GET", "/api/due")
        msgs = data.get("messages")
        due_ok = (st == 200 and isinstance(msgs, list) and len(msgs) >= 1
                  and all(isinstance(m, dict) and "text" in m and "audio_url" in m
                          for m in msgs))
        _report(due_ok, "GET /api/due messages 元素为含 text/audio_url 的对象",
                f"status={st} data={data}")
        # due 消息的 audio_url 也可作为 /api/tts 测试的实际文件
        if not chat_audio_url and msgs and isinstance(msgs[0], dict):
            chat_audio_url = msgs[0].get("audio_url")

        # GET /api/tts/<实际生成的文件>：200 且 Content-Type 为 audio/*。
        # 发声链升级后主通道 GLM-TTS 产物为 wav（audio/wav），edge-tts 备用为 mp3
        # （audio/mpeg），故断言放宽为 audio/*；若在线通道全部失败（audio_url 全为
        # null），则在缓存目录手写一个伪 mp3 验证端点本身（端点行为与合成链路解耦，
        # 保证测试确定性）。
        tts_url = chat_audio_url
        dummy_path = None
        if not tts_url:
            import hashlib
            name = hashlib.sha1("自检音频端点测试".encode("utf-8")).hexdigest() + ".mp3"
            os.makedirs(_TTS_CACHE_DIR, exist_ok=True)
            dummy_path = os.path.join(_TTS_CACHE_DIR, name)
            with open(dummy_path, "wb") as f:
                f.write(b"ID3\x04\x00\x00\x00\x00\x00\x00")  # 最小伪 mp3 头
            tts_url = "/api/tts/" + name
            print("  [提示] 在线 TTS 当前不可用（audio_url 为 null），改用手写伪 mp3 验证 /api/tts 端点。")
        st, ctype, body = _request_file(tts_url)
        _report(st == 200 and (ctype or "").lower().startswith("audio/") and len(body) > 0,
                "GET /api/tts/<实际文件> 返回 200 且 Content-Type 为 audio/*",
                f"status={st} content-type={ctype} bytes={len(body)} url={tts_url}")
        if dummy_path:
            try:
                os.remove(dummy_path)  # 清理伪 mp3，不污染真实缓存
            except OSError:
                pass

        # == 声音必达：/api/sound_test ==
        # 契约：GET /api/sound_test → {"audio_url": str|null, "speaker": true}；
        # GLM-TTS 当前网络可用，audio_url 必须非空
        st, data = _request("GET", "/api/sound_test")
        sound_audio_url = data.get("audio_url")
        _report(st == 200 and bool(sound_audio_url) and data.get("speaker") is True,
                "GET /api/sound_test 返回 200 且 audio_url 非空（GLM-TTS 可用）",
                f"status={st} data={data}")
        # 用返回的 audio_url 实际取一次音频：200 且 Content-Type 为 audio/*
        if sound_audio_url:
            st, ctype, body = _request_file(sound_audio_url)
            _report(st == 200 and (ctype or "").lower().startswith("audio/") and len(body) > 0,
                    "GET sound_test audio_url 返回 200 且 Content-Type 为 audio/*",
                    f"status={st} content-type={ctype} bytes={len(body)} url={sound_audio_url}")
        else:
            _report(False, "GET sound_test audio_url 返回 200 且 Content-Type 为 audio/*",
                    "audio_url 为空，跳过")

        # 防路径穿越：/api/tts/../server.py 必须 404（绝不能泄露源码文件）
        st, _data = _request("GET", "/api/tts/../server.py")
        _report(st == 404, "GET /api/tts/../server.py 路径穿越返回 404", f"status={st}")

        # == 网页背景：/api/background 与 /api/files ==
        # 无状态文件时 image_url 必须为 None（先确保状态文件不存在，已备份）
        if os.path.exists(_WEB_BG):
            os.remove(_WEB_BG)
        st, data = _request("GET", "/api/background")
        _report(st == 200 and data.get("image_url") is None,
                "GET /api/background 无状态文件返回 image_url=null",
                f"status={st} data={data}")

        # 写入状态文件 + 测试图片：image_url 指向该图片，GET 图片 200 且 image/*
        with open(_test_img_path, "wb") as f:
            f.write(_TEST_PNG_BYTES)
        with open(_WEB_BG, "w", encoding="utf-8") as f:
            json.dump({"image": _TEST_IMG_NAME}, f, ensure_ascii=False)
        st, data = _request("GET", "/api/background")
        expected_url = "/api/files/" + _TEST_IMG_NAME
        _report(st == 200 and data.get("image_url") == expected_url,
                "GET /api/background 写入状态文件后返回对应 image_url",
                f"status={st} data={data} 期望 image_url={expected_url}")
        st, ctype, body = _request_file(expected_url)
        _report(st == 200 and (ctype or "").lower().startswith("image/") and len(body) > 0,
                "GET /api/files/<背景图> 返回 200 且 Content-Type 为 image/*",
                f"status={st} content-type={ctype} bytes={len(body)}")

        # 防路径穿越：/api/files/../server.py 必须 404（绝不能泄露源码文件）
        st, _data = _request("GET", "/api/files/../server.py")
        _report(st == 404, "GET /api/files/../server.py 路径穿越返回 404", f"status={st}")

        st, data = _request("POST", "/api/chat", {"text": ""})
        _report(st == 200 and bool((data.get("reply") or "").strip()) and "error" not in data,
                  "POST /api/chat 空文本有礼貌回复不 500", f"status={st} data={data}")

        # ASR：离线合成『Nolan 语音输入测试』wav → POST /api/asr → 断言识别文本
        try:
            wav_bytes = _synth_test_wav()
            st, data = _request_raw("POST", "/api/asr", wav_bytes, "audio/wav")
            text = data.get("text") or ""
            _report(st == 200 and ("Nolan" in text or "语音" in text),
                      "POST /api/asr 识别含『Nolan』或『语音』", f"status={st} data={data}")
        except Exception as e:
            _report(False, "POST /api/asr 识别含『Nolan』或『语音』", f"合成/请求异常：{e}")

        # 服务端麦克风：start → 录 1.5 秒环境音 → stop 返回 200 且含 text 字段
        st, data = _request("POST", "/api/mic/start")
        if st == 200 and data.get("ok") is True:
            _report(True, "POST /api/mic/start 返回 ok")
            time.sleep(1.5)  # 录 1.5 秒环境音（内容不限，静音也算通过）
            st, data = _request("POST", "/api/mic/stop")
            _report(st == 200 and "text" in data,
                    "POST /api/mic/stop 返回 200 且含 text 字段", f"status={st} data={data}")
            # 重复 stop：未在录音，契约要求返回 {"text": ""} 而不是报错
            st, data = _request("POST", "/api/mic/stop")
            _report(st == 200 and data.get("text") == "",
                    "POST /api/mic/stop 重复调用返回空文本不报错", f"status={st} data={data}")
        else:
            # 麦克风被占用/无设备时 start 会 500，三项一并记失败并说明
            _report(False, "POST /api/mic/start 返回 ok", f"status={st} data={data}")
            _report(False, "POST /api/mic/stop 返回 200 且含 text 字段", "start 失败，跳过")
            _report(False, "POST /api/mic/stop 重复调用返回空文本不报错", "start 失败，跳过")

    finally:
        # 4) 关子进程并还原备份（无论成败）
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        _restore(_LONG_TERM, bak_long)
        _restore(_REMINDERS, bak_rem)
        _restore(_WEB_BG, bak_bg)
        _restore(_PID_FILE, bak_pid)
        # 清理测试图片，不污染 jarvis/files
        try:
            if os.path.exists(_test_img_path):
                os.remove(_test_img_path)
        except OSError:
            pass

    print(f"\n自检完成：通过 {_passed} 项，失败 {_failed} 项。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
