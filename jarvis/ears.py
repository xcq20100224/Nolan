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

import os
import time
import numpy as np
import sounddevice as sd

# ========== 可调参数 ==========
采样率 = 16000            # Whisper 要求 16kHz
声道数 = 1                # 单声道
数据类型 = "float32"

帧长毫秒 = 30             # 每帧 30ms，用于 VAD 能量计算
帧样本数 = int(采样率 * 帧长毫秒 / 1000)


def _读环境浮点(名: str, 默认: float) -> float:
    """读浮点环境变量，缺失/非法值回落默认——配置出错绝不拖垮耳朵。"""
    try:
        return float(os.environ.get(名, "") or 默认)
    except ValueError:
        return 默认


def _读环境整数(名: str, 默认: int) -> int:
    try:
        return int(os.environ.get(名, "") or 默认)
    except ValueError:
        return 默认


# ---- VAD 自适应门限（B4：环境噪声底估计的窗口/系数全部可做环境变量微调） ----
# 默认值与旧版一字不差；嘈杂环境可调，例如 NOLAN_VAD_THRESHOLD_RATIO=3.0
环境标定秒 = _读环境浮点("NOLAN_VAD_ENV_SECONDS", 0.5)    # 录音前采集环境噪声的时长
阈值倍率 = _读环境浮点("NOLAN_VAD_THRESHOLD_RATIO", 2.5)  # 语音判定阈值 = 环境 RMS × 倍率
最小阈值 = _读环境浮点("NOLAN_VAD_MIN_THRESHOLD", 0.01)   # 阈值下限，防安静环境阈值过低误判
语音起判帧数 = _读环境整数("NOLAN_VAD_START_FRAMES", 3)   # 连续 3 帧超阈值才认为“说话开始”
静音结束秒 = _读环境浮点("NOLAN_VAD_END_SILENCE", 1.0)    # 语音后连续静音多久判定“说完了”
最长句子秒 = 15.0         # 单句最长录音时长，防止无限录音

# ---- 突发噪声免疫（B4） ----
# 第一性原理：语音是「持续」信号，咳嗽/敲桌是「瞬态」信号——人类最短音节
# 也在 150ms 量级，而敲击类突发通常在 100ms 内。因此「能量突增但有声部分
# 持续不足 150ms」的片段不送进 ASR。设为 0 完全关闭，恢复旧版逐字行为。
突发免疫毫秒 = _读环境整数("NOLAN_BURST_IMMUNE_MS", 150)

模型名称 = "small"         # P5 换代：实测 small/beam1 = 1.53x 实时倍率、准确率 92%，
                           # 与 medium/beam5（5.49x/92%）准确率持平、提速 3.6 倍；
                           # 冷启动加载也从 ~58s 降到 ~6s。medium 的 CPU 延迟是
                           # 语音链路最大瓶颈，benchmark 数字见 bench_asr.py
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


def _片段是否语音(有声帧数: int) -> bool:
    """突发噪声免疫判定：有声部分持续 ≥ 突发免疫毫秒 才算语音。
    突发免疫毫秒 = 0 时恒真（完全关闭，恢复旧版行为）。"""
    return 有声帧数 * 帧长毫秒 >= 突发免疫毫秒


def _录音(timeout: float) -> np.ndarray | None:
    """
    从默认麦克风录一句话。
    返回一维 float32 数组（16kHz）；超时未听到语音返回 None。
    """
    帧列表: list[np.ndarray] = []
    已说话 = False
    静音帧数 = 0
    有声帧数 = 0          # 累计超阈值帧数，用于突发噪声免疫（B4）
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
                        有声帧数 = 连续超阈值  # 起判的连续高帧全部计入有声
                else:
                    帧列表.append(帧)
                    if 能量 < 阈值:
                        静音帧数 += 1
                    else:
                        静音帧数 = 0
                        有声帧数 += 1
                    if 静音帧数 >= 静音结束帧数:
                        if not _片段是否语音(有声帧数):
                            # 突发噪声免疫：咳嗽/敲桌等短促高能片段不进 ASR，
                            # 清空状态回到「等说话」继续值守，而不是结束录音
                            print(f"👂 耳朵：忽略 {有声帧数 * 帧长毫秒}ms 突发噪声 🛡️")
                            已说话 = False
                            帧列表 = []
                            静音帧数 = 0
                            连续超阈值 = 0
                            有声帧数 = 0
                            continue
                        break  # 说完后的尾静音达标，结束
                    if len(帧列表) >= 最长帧数:
                        break  # 达到单句最长时长
    except Exception as 错误:
        print(f"👂 耳朵：录音出错 😢 —— {错误}")
        return None

    if not 已说话 or not 帧列表:
        return None

    # 超时/最长帧数收尾路径也要过一遍突发免疫：短促噪声不配进 ASR
    if not _片段是否语音(有声帧数):
        print(f"👂 耳朵：忽略 {有声帧数 * 帧长毫秒}ms 突发噪声 🛡️")
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
            beam_size=1,  # P5：small 模型贪心解码实测准确率不降（92%），更快
        )
        文本 = "".join(片段.text for 片段 in 片段们).strip()
        return 文本 if 文本 else None
    except Exception as 错误:
        print(f"👂 耳朵：识别出错 😢 —— {错误}")
        return None


# ========== 声纹门禁（B4，默认关闭） ==========
# 第一性原理：唤醒词工作在文本层，任何人喊「Nolan」都会命中；门禁在声学层
# 补一道「谁在说」的过滤。默认关闭的两个理由：
#   1) 没录入声纹的家庭/办公场景，门禁开了等于没开（verify 恒放行），
#      平白多一层复杂度；
#   2) 声纹是轻量质心模板而非安防级，误拒代价（主人被锁门外）远高于
#      误放代价（陌生人误唤醒一次）——默认行为必须与旧版一字不差。
_VOICE_GATE = os.environ.get("NOLAN_VOICE_GATE", "").strip().lower() in ("1", "true", "yes", "on")


def _取声纹模块():
    """懒加载同目录 voiceprint 模块；脚本/包两种导入方式都兜底，缺模块返回 None。"""
    try:
        import voiceprint  # type: ignore
        return voiceprint
    except Exception:
        try:
            from . import voiceprint  # type: ignore
            return voiceprint
        except Exception:
            return None


def voice_gate_pass(音频: np.ndarray, 采样率_: int = 采样率) -> bool:
    """声纹门禁：判断这段音频是否主人在说。

    放行条件（任一即过）：门禁未开启 / 未注册模板 / 打分通过 / 门禁自身故障。
    拒绝只在一种情况发生：门禁已开、模板已录、打分低于阈值——此时打印一行日志。
    「宁可放行不可误拒」是刻意选择，见上方 _VOICE_GATE 注释。
    """
    if not _VOICE_GATE:
        return True
    vp = _取声纹模块()
    if vp is None or not vp.is_enrolled():
        return True  # 没录入就不过滤，绝不把主人锁在门外
    try:
        通过, 分数 = vp.verify(音频, 采样率_)
    except Exception as 错误:
        print(f"👂 耳朵：声纹打分异常（{错误}），本次放行 ⚠️")
        return True
    if not 通过:
        print(f"👂 耳朵：声纹不匹配（{分数:.2f}），忽略本次唤醒 🚫")
    return 通过


def enroll_voice(段数: int = 3, 每段超时: float = 25.0) -> bool:
    """引导式声纹注册：复用 _录音 路径采集 N 段主人语音，交给 voiceprint 建档。
    每段最多两次尝试；任一段失败即中止（不覆盖旧模板）。返回是否注册成功。
    注册完成后设 NOLAN_VOICE_GATE=1 才会真正启用门禁。
    """
    vp = _取声纹模块()
    if vp is None:
        print("👂 耳朵：voiceprint 模块不可用，无法注册声纹 😢")
        return False
    样本: list[np.ndarray] = []
    for 第几段 in range(1, 段数 + 1):
        print(f"👂 声纹注册 {第几段}/{段数}：请自然说一句话（例如「诺兰，今天天气怎么样」）🎙️")
        音频 = None
        for _ in range(2):
            音频 = _录音(每段超时)
            if 音频 is not None and len(音频) >= int(采样率 * 0.8):
                break
            print("👂 没录到有效语音，请再试一次……")
        if 音频 is None or len(音频) < int(采样率 * 0.8):
            print("👂 声纹注册中止：有效语音不足 😢")
            return False
        样本.append(音频)
    成功 = bool(vp.enroll(样本, 采样率))
    print("👂 声纹注册完成 ✅（设 NOLAN_VOICE_GATE=1 后门禁生效）" if 成功
          else "👂 声纹注册失败 😢")
    return 成功


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
        if not voice_gate_pass(音频):
            return None  # 门禁开启且声纹不匹配：这次「听到」作废
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
