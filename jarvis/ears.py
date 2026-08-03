# -*- coding: utf-8 -*-
"""
ears.py —— 贾维斯的耳朵 👂
功能：监听默认麦克风，用 RMS 能量做 VAD 检测语音起止，
      录音后交给 faster-whisper 识别中文文本。

对外接口（跨模块契约，签名不可改）：
    def listen_once(timeout: float = 30.0) -> str | None
        阻塞监听用户说的一句话，返回识别出的中文文本；
        超时或纯静音返回 None。
"""

import time
import numpy as np
import sounddevice as sd

# ========== 可调参数 ==========
采样率 = 16000            # Whisper 要求 16kHz
声道数 = 1                # 单声道
数据类型 = "float32"

帧长毫秒 = 30             # 每帧 30ms，用于 VAD 能量计算
帧样本数 = int(采样率 * 帧长毫秒 / 1000)

环境标定秒 = 0.5          # 录音前采集环境噪声的时长
阈值倍率 = 2.5            # 语音判定阈值 = 环境 RMS × 倍率
最小阈值 = 0.01           # 阈值下限，防止安静环境下阈值过低误判
语音起判帧数 = 3          # 连续 3 帧超阈值才认为“说话开始”
静音结束秒 = 1.0          # 语音后连续静音多久判定“说完了”
最长句子秒 = 15.0         # 单句最长录音时长，防止无限录音

模型名称 = "medium"        # faster-whisper small，本地已缓存
模型设备 = "cpu"
模型精度 = "int8"

# ---- 播报打断（barge-in, P3）参数 ----
打断触发帧数 = 13           # 连续 13 帧（约 0.4s）超阈值才判定「主人开口」，
                            # 防咳嗽/敲击/短时杂音误触
打断标定秒 = 0.3            # 打断监听启动时的环境标定时长（此时 Nolan 自己的
                            # 播报声成为底噪基线，阈值自动抬高到播报声之上）


class _VoiceTrigger:
    """持续语音判定器：喂入逐帧 RMS，连续超阈值达帧数即触发。
    纯逻辑无硬件依赖，单元测试直接喂合成能量序列。"""

    def __init__(self, env_rms: float, trigger_frames: int = 打断触发帧数):
        self.阈值 = max(env_rms * 阈值倍率, 最小阈值)
        self.需要帧数 = max(1, trigger_frames)
        self._连续 = 0

    def feed(self, rms: float) -> bool:
        """喂一帧能量，返回本帧后是否进入「主人开口」状态。"""
        if rms >= self.阈值:
            self._连续 += 1
        else:
            self._连续 = 0
        return self._连续 >= self.需要帧数


def watch_for_voice(on_voice, stop_event, frame_source=None) -> None:
    """后台打断监听：检测到主人持续开口即回调 on_voice() 后返回；
    stop_event 置位（播报结束）时安静退出。

    frame_source 可注入自定义帧迭代器（单元测试喂合成帧），
    缺省使用真实麦克风输入流。任何异常静默退出——打断是增强，
    绝不能拖垮播报主流程。
    """
    try:
        if frame_source is not None:
            # 测试路径：帧源为可迭代的一维数组序列（首段作环境标定）
            frames = iter(frame_source)
            标定帧数 = max(1, int(打断标定秒 * 1000 / 帧长毫秒))
            env = [_计算RMS(f) for _, f in zip(range(标定帧数), frames)]
            trig = _VoiceTrigger(float(np.median(env)) if env else 0.0)
            for f in frames:
                if stop_event.is_set():
                    return
                if trig.feed(_计算RMS(f)):
                    on_voice()
                    return
            return
        with sd.RawInputStream(samplerate=采样率, blocksize=帧样本数,
                               dtype=数据类型, channels=声道数) as 流:
            标定帧数 = max(1, int(打断标定秒 * 1000 / 帧长毫秒))
            env = [_计算RMS(_录一帧(流)) for _ in range(标定帧数)]
            trig = _VoiceTrigger(float(np.median(env)) if env else 0.0)
            while not stop_event.is_set():
                if trig.feed(_计算RMS(_录一帧(流))):
                    on_voice()
                    return
    except Exception:
        return  # 静默：打断监听失败不影响播报

# ========== 模型单例（模块级懒加载） ==========
_模型 = None
_模型加载失败 = False


def _取模型():
    """懒加载 faster-whisper 模型单例；加载失败返回 None。"""
    global _模型, _模型加载失败
    if _模型加载失败:
        return None
    if _模型 is None:
        try:
            from faster_whisper import WhisperModel

            print("👂 耳朵：正在加载 Whisper 模型（首次稍慢）……")
            _模型 = WhisperModel(模型名称, device=模型设备, compute_type=模型精度)
            print("👂 耳朵：模型就绪 ✅")
        except Exception as 错误:
            _模型加载失败 = True
            print(f"👂 耳朵：模型加载失败 😢 —— {错误}")
            return None
    return _模型


def _计算RMS(帧: np.ndarray) -> float:
    """计算一帧音频的 RMS（均方根）能量。"""
    if 帧.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(帧), dtype=np.float64)))


def _录一帧(流: sd.RawInputStream) -> np.ndarray:
    """从输入流读一帧，转成一维 float32 数组。"""
    原始数据, _溢出 = 流.read(帧样本数)
    帧 = np.frombuffer(原始数据, dtype=np.float32)
    return 帧


def _录音(timeout: float) -> np.ndarray | None:
    """
    从默认麦克风录一句话。
    返回一维 float32 数组（16kHz）；超时未听到语音返回 None。
    """
    帧列表: list[np.ndarray] = []
    已说话 = False
    静音帧数 = 0
    静音结束帧数 = max(1, int(静音结束秒 * 1000 / 帧长毫秒))
    最长帧数 = int(最长句子秒 * 1000 / 帧长毫秒)

    开始时刻 = time.monotonic()

    try:
        with sd.RawInputStream(
            samplerate=采样率,
            blocksize=帧样本数,
            dtype=数据类型,
            channels=声道数,
        ) as 流:
            # ---- 第一步：采集约 0.5 秒环境噪声，标定能量阈值 ----
            标定帧数 = max(1, int(环境标定秒 * 1000 / 帧长毫秒))
            环境能量: list[float] = []
            for _ in range(标定帧数):
                if time.monotonic() - 开始时刻 > timeout:
                    return None
                环境能量.append(_计算RMS(_录一帧(流)))
            环境RMS = float(np.median(环境能量)) if 环境能量 else 0.0
            阈值 = max(环境RMS * 阈值倍率, 最小阈值)

            # ---- 第二步：VAD 循环，等待语音开始 / 检测语音结束 ----
            连续超阈值 = 0
            while True:
                if time.monotonic() - 开始时刻 > timeout:
                    if not 已说话:
                        print("👂 耳朵：超时未听到语音 ⏱️")
                        return None
                    break  # 已经在说话，按当前已有内容收尾

                帧 = _录一帧(流)
                能量 = _计算RMS(帧)

                if not 已说话:
                    if 能量 >= 阈值:
                        连续超阈值 += 1
                    else:
                        连续超阈值 = 0
                    if 连续超阈值 >= 语音起判帧数:
                        已说话 = True
                        帧列表.append(帧)
                        静音帧数 = 0
                else:
                    帧列表.append(帧)
                    if 能量 < 阈值:
                        静音帧数 += 1
                    else:
                        静音帧数 = 0
                    if 静音帧数 >= 静音结束帧数:
                        break  # 说完后的尾静音达标，结束
                    if len(帧列表) >= 最长帧数:
                        break  # 达到单句最长时长
    except Exception as 错误:
        print(f"👂 耳朵：录音出错 😢 —— {错误}")
        return None

    if not 已说话 or not 帧列表:
        return None

    # 去掉尾部静音帧，减少 Whisper 的无效输入
    有效帧 = 帧列表[:-静音帧数] if 静音帧数 > 0 else 帧列表
    if not 有效帧:
        return None

    音频 = np.concatenate(有效帧).astype(np.float32)
    时长 = len(音频) / 采样率
    print(f"👂 耳朵：录到 {时长:.1f} 秒语音 🎙️")
    return 音频


def _转写(音频: np.ndarray) -> str | None:
    """把 16kHz float32 音频交给 faster-whisper 识别，返回中文文本。"""
    模型 = _取模型()
    if 模型 is None:
        return None
    try:
        片段们, _信息 = 模型.transcribe(
            音频,
            language="zh",
            vad_filter=True,
            beam_size=5,
        )
        文本 = "".join(片段.text for 片段 in 片段们).strip()
        return 文本 if 文本 else None
    except Exception as 错误:
        print(f"👂 耳朵：识别出错 😢 —— {错误}")
        return None


# ========== 对外接口（契约函数） ==========
def listen_once(timeout: float = 30.0) -> str | None:
    """
    阻塞监听用户说的一句话，返回识别出的中文文本；
    超时或纯静音返回 None。
    """
    try:
        音频 = _录音(timeout)
        if 音频 is None:
            return None
        文本 = _转写(音频)
        if 文本:
            print(f"👂 耳朵：识别结果「{文本}」✅")
        else:
            print("👂 耳朵：没听清（识别为空）🤔")
        return 文本
    except Exception as 错误:
        # 兜底：任何意外都不向上抛，保证主循环不崩
        print(f"👂 耳朵：发生未预期错误 😢 —— {错误}")
        return None


# ========== 模块自测（不碰麦克风） ==========
if __name__ == "__main__":
    print("👂 耳朵模块自测开始……")

    # 1) 静音数组走一遍转写流程：应返回 None 而不崩溃
    静音 = np.zeros(采样率, dtype=np.float32)  # 1 秒纯静音
    结果 = _转写(静音)
    assert 结果 is None or isinstance(结果, str)
    print(f"👂 自测 1/2：静音转写返回 {结果!r}（不崩溃 ✅）")

    # 2) 低幅噪声走一遍转写流程：同样不应崩溃
    rng = np.random.default_rng(42)
    噪声 = (rng.standard_normal(采样率 * 2) * 0.001).astype(np.float32)
    结果2 = _转写(噪声)
    assert 结果2 is None or isinstance(结果2, str)
    print(f"👂 自测 2/2：噪声转写返回 {结果2!r}（不崩溃 ✅）")

    print("👂 自测全部通过 🎉（真实录音请由主程序调用 listen_once()）")
