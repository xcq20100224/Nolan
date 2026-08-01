# -*- coding: utf-8 -*-
"""
嘴巴模块（mouth.py）—— 贾维斯的语音输出（声音必达版）。

发声三级链（严格按序，逐级降级，任何一级失败只打印中文日志再降级）：
    1) GLM-TTS 主通道：读 jarvis/llm_config.json 的 api_key/base_url，
       httpx POST {base_url}/audio/speech（voice='male'，response_format='wav'），
       返回的 wav 字节写临时文件后用 pygame.mixer 播放；
    2) edge-tts 备用：现有 zh-CN-YunjianNeural（浑厚男声）逻辑，失败重试一次；
    3) SAPI 兜底：Windows 系统离线语音（无需联网，但音色由系统决定）。

对外接口（跨模块契约，签名不可改）：
    def speak(text: str) -> None
附加接口（可打断）：
    def interrupt() -> None   # 立即打断当前播报；speak 开头自清标志
"""

import asyncio
import json
import os
import tempfile
import threading
import time

import edge_tts
import pygame

# ===== 可配置常量 =====
VOICE = "zh-CN-YunjianNeural"       # edge-tts 备用音色（浑厚男声）
GLM_TTS_VOICE = "male"              # GLM-TTS 主通道男声
GLM_TTS_TIMEOUT = 60                # GLM-TTS 请求超时（秒）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_config.json")

# pygame.mixer 模块级单例初始化标记
_mixer_ready = False

# 可打断标志：外部线程（如网页端 /api/stop）置位，正在播放的语音立即停止。
# 每次 speak() 开头清位，保证打断只作用于「当前这一句」。
_interrupt = threading.Event()


def interrupt() -> None:
    """打断当前正在播放的语音（pygame.mixer 通道）。

    线程安全，可随时调用；无播放时调用无副作用。
    注意：SAPI 离线兜底（pyttsx3 的 runAndWait）是阻塞调用，不可中断——
    该通道仅在主备两条网络链路全部失败时启用，属极端降级场景，接受此边界。
    """
    _interrupt.set()


def _init_mixer() -> None:
    """初始化 pygame.mixer（只初始化一次，重复调用安全）。"""
    global _mixer_ready
    if _mixer_ready:
        return
    pygame.mixer.init()
    _mixer_ready = True


def _load_llm_config() -> dict:
    """读取 jarvis/llm_config.json，返回配置字典。

    文件缺失或内容非法时返回空字典（由调用方决定降级），本函数绝不抛异常。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict):
            return cfg
    except Exception as exc:
        print(f"⚠️ 嘴巴：读取 llm_config.json 失败：{exc}")
    return {}


def _synthesize_glm_tts(text: str) -> bytes:
    """第一级：GLM-TTS 主通道合成，返回 wav 字节；失败抛异常交由调用方降级。"""
    import httpx  # 延迟导入：主通道失败时其他链路不依赖 httpx

    cfg = _load_llm_config()
    api_key = cfg.get("api_key")
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("llm_config.json 缺少 api_key/base_url，GLM-TTS 不可用")

    url = f"{base_url}/audio/speech"
    payload = {
        "model": "glm-tts",
        "input": text,
        "voice": GLM_TTS_VOICE,
        "response_format": "wav",
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=GLM_TTS_TIMEOUT)
    resp.raise_for_status()
    audio = resp.content
    if not audio:
        raise RuntimeError("GLM-TTS 返回空音频")
    return audio


def _synthesize_to_file(text: str, out_path: str) -> None:
    """第二级：edge-tts 合成，失败间隔 1 秒重试一次，仍失败抛异常交由调用方降级。"""
    async def _run() -> None:
        communicate = edge_tts.Communicate(text, VOICE)
        await communicate.save(out_path)

    try:
        asyncio.run(_run())
    except Exception as first_exc:
        # 瞬态网络抖动常见：重试一次（仍失败则抛给外层统一走 SAPI 兜底）
        print(f"⚠️ 嘴巴：edge-tts 首次合成失败（{first_exc}），1 秒后重试一次...")
        time.sleep(1)
        asyncio.run(_run())


def _speak_offline(text: str) -> None:
    """第三级：离线兜底嗓子。Windows SAPI（pyttsx3）直接播报，无需联网。

    音色由系统语音包决定（中文系统通常自带微软晓晓/慧慧）。
    任何失败只打印警告，绝不抛异常。
    """
    try:
        import pyttsx3  # 延迟导入：仅在兜底时加载

        engine = pyttsx3.init()
        # 优先选系统里的中文语音
        for voice in engine.getProperty("voices"):
            meta = f"{voice.id} {voice.name}".lower()
            if "zh" in meta or "chinese" in meta or "huihui" in meta or "xiaoxiao" in meta:
                engine.setProperty("voice", voice.id)
                break
        print("🔊 嘴巴：使用离线系统语音播报。")
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as exc:
        print(f"⚠️ 嘴巴：离线语音也失败了（仅文字输出）：{exc}")


def _write_temp_file(data: bytes, suffix: str) -> str:
    """把音频字节写入系统临时文件，返回路径。"""
    fd, path = tempfile.mkstemp(prefix="jarvis_tts_", suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def _play_file(path: str) -> None:
    """用 pygame.mixer 播放音频文件，播完或被 interrupt() 打断才返回。

    被打断时先 stop() 再照常 unload()——不能只 return，否则 Windows 上
    临时文件仍被 mixer 占用，外层 finally 删不掉。
    """
    _init_mixer()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    # 轮询播放状态（每 50ms），同时检查打断标志
    while pygame.mixer.music.get_busy():
        if _interrupt.is_set():
            pygame.mixer.music.stop()
            print("⏹️ 嘴巴：播报被主人打断。")
            break
        time.sleep(0.05)
    pygame.mixer.music.unload()
    print("🔊 嘴巴：播报完毕。")


def _stop_playback() -> None:
    """降级前停止并卸载当前播放，避免占用临时文件。"""
    try:
        if _mixer_ready:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
    except Exception:
        pass


def speak(text: str) -> None:
    """把 text 合成语音并播放，播完才返回。

    三级链：GLM-TTS → edge-tts（重试一次）→ SAPI 离线兜底。
    任何失败只打印中文警告并安全降级/返回，绝不抛异常崩溃。
    """
    if not text or not text.strip():
        print("🔇 嘴巴：收到空文本，跳过播报。")
        return

    # 新一轮播报开始，清除上一轮可能残留的打断标志
    _interrupt.clear()

    tmp_path = None
    try:
        # 1. 初始化播放器（单例）
        _init_mixer()

        # 2. 第一级：GLM-TTS 主通道（wav）
        try:
            print("🗣️ 嘴巴：正在用 GLM-TTS 主通道合成语音（男声）...")
            audio = _synthesize_glm_tts(text)
            tmp_path = _write_temp_file(audio, ".wav")
            print(f"✅ 嘴巴：GLM-TTS 合成成功，文件大小 {os.path.getsize(tmp_path)} 字节。")
            _play_file(tmp_path)
            return
        except Exception as glm_exc:
            print(f"⚠️ 嘴巴：GLM-TTS 主通道失败：{glm_exc}，降级 edge-tts 备用通道。")

        # 3. 第二级：edge-tts 备用通道（mp3，失败重试一次）
        try:
            _stop_playback()
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            print(f"🗣️ 嘴巴：正在用 edge-tts 备用通道合成语音（音色 {VOICE}）...")
            fd, tmp_path = tempfile.mkstemp(prefix="jarvis_tts_", suffix=".mp3")
            os.close(fd)  # 先关掉句柄，避免 Windows 下文件被占用
            _synthesize_to_file(text, tmp_path)
            size = os.path.getsize(tmp_path)
            if size <= 0:
                raise RuntimeError("edge-tts 合成结果为空文件")
            print(f"✅ 嘴巴：edge-tts 合成成功，文件大小 {size} 字节。")
            _play_file(tmp_path)
            return
        except Exception as edge_exc:
            print(f"⚠️ 嘴巴：edge-tts 备用通道失败：{edge_exc}，降级 SAPI 离线兜底。")

        # 4. 第三级：SAPI 离线兜底
        _stop_playback()
        _speak_offline(text)

    except Exception as exc:  # 播放器初始化等意外失败也绝不崩溃
        print(f"⚠️ 嘴巴：发声链路异常：{exc}，尝试离线兜底。")
        _speak_offline(text)
    finally:
        # 5. 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as exc:
                print(f"⚠️ 嘴巴：临时文件删除失败（不影响使用）：{exc}")


if __name__ == "__main__":
    # 模块自测：真实合成并播放一句话
    speak("你好，我是贾维斯。")
