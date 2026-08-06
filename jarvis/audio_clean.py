# -*- coding: utf-8 -*-
"""音频净化模块（audio_clean.py）—— 消灭 TTS wav 开头的「滴答」爆音。

诊断结论（2026-08-06，合成端实测）：
    GLM-TTS API 返回的每一句 wav 开头都带一段固定约 1.8 秒的「接通提示音」：
    550Hz 纯音频发（振幅恒 1794）与静音交替，逐采样完全相同，与文本无关。
    用户听到的「滴答滴答」就是它。它既不是直流偏移（实测 dc≈-1，可忽略），
    也不是采样突跳爆点（前 200ms 无 >8000 的跳变），而是结构化纯音前奏。

净化策略（clean_wav_bytes，纯函数，字节进字节出）：
    1) 解析 wav 头（采样率/声道/位深全从头读，不硬编码；8/16/32bit PCM、
       单/双声道均支持）；
    2) 10ms 帧分类：静音 / 稳定纯音（频谱能量高度集中于单一峰）/ 其他（语音）；
    3) 从头扫描：连续「静音+纯音」前缀（纯音主频须一致，防误裁元音/音乐）
       视为前奏裁掉；孤立的非语音瞬态（<20ms，如爆点）跳过；
       第一个持续 ≥20ms 的「语音」帧作为语音起点，起点前留 5ms 余量；
    4) 去直流偏移（裁后整体减均值）；
    5) 10ms 线性淡入，消灭裁切点的零交叉爆音。

安全边界：
    - 任何解析/处理异常 → 原样返回输入字节，绝不弄坏音频；
    - 裁剪上限 5 秒、裁剪后剩余不得少于 500ms、裁剪比例不得过半，
      触发任一即放弃裁剪（只做无害的淡入）；
    - 无纯音前奏时退化为普通前导静音裁剪（底噪 3 倍门限），
      干净样本几乎原样通过。
"""

from __future__ import annotations

import io
import wave

import numpy as np

# ===== 可调常量 =====
_FRAME_MS = 10          # 分析帧长（毫秒）
_FADE_MS = 10           # 淡入长度（毫秒）
_MARGIN_MS = 5          # 语音起点前保留的余量（毫秒）
_MIN_ONSET_FRAMES = 2   # 语音起点须持续的帧数（2 帧 = 20ms）
_MAX_TRIM_MS = 5000     # 单次最多裁掉的开头长度（毫秒）
_MIN_REMAIN_MS = 500    # 裁剪后最少剩余时长（毫秒，静音裁剪路径）
_MIN_REMAIN_TONE_MS = 200   # 确认纯音前奏时的剩余下限（短句人声本身可不足 500ms）
_TONE_CONCENTRATION = 0.8   # 纯音判定：top-3 频峰能量占比阈值
_TONE_FREQ_TOL = 0.15       # 前奏内纯音主频一致性容差（±15%）
_TONE_FREQ_TOL_HZ = 150.0   # 另加绝对容差：10ms 帧 FFT bin 宽 100Hz，主频可能在相邻 bin 间翻转
_TONE_RMS_TOL = 0.35        # 前奏内纯音振幅一致性容差（±35%，提示音逐采样恒定）
_MIN_PREAMBLE_MS = 80       # 前奏最短长度（短于此不值得裁）
_SIL_FLOOR_RATIO = 3.0      # 静音门限：底噪的倍数
_ABS_SIL_RMS = 30.0         # 静音绝对下限（int16 标尺）


def _decode_pcm(raw: bytes, sampwidth: int) -> np.ndarray | None:
    """把 PCM 字节解码为 float64（[-1,1] 标尺）；不支持的位深返回 None。"""
    if sampwidth == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sampwidth == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif sampwidth == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        return None
    return a


def _encode_pcm(a: np.ndarray, sampwidth: int) -> bytes:
    """float64（[-1,1]）编码回 PCM 字节。"""
    a = np.clip(a, -1.0, 1.0)
    if sampwidth == 2:
        return (a * 32767.0).round().astype("<i2").tobytes()
    if sampwidth == 1:
        return ((a * 127.0).round() + 128.0).astype(np.uint8).tobytes()
    if sampwidth == 4:
        return (a * 2147483647.0).round().astype("<i4").tobytes()
    raise ValueError(f"unsupported sampwidth={sampwidth}")


def _classify_frames(mono: np.ndarray, sr: int) -> tuple[list[str], list[float], np.ndarray, float]:
    """10ms 帧分类：'sil' / 'tone' / 'other'，返回 (类别, 各帧主频Hz, 各帧RMS, 静音门限)。

    纯音判定：帧频谱 top-3 峰能量占比 > _TONE_CONCENTRATION。
    语音（含元音）有丰富泛音，能量分散，不会误判；爆点/咔哒是宽带冲激，
    能量同样分散，归为 'other'（由持续性规则过滤孤立瞬态）。
    """
    frame = max(1, int(sr * _FRAME_MS / 1000))
    n = len(mono) // frame
    if n == 0:
        return [], [], np.array([]), 0.0
    rms = np.array([float(np.sqrt(np.mean(mono[i * frame:(i + 1) * frame] ** 2)))
                    for i in range(n)])
    # 底噪：全段帧 RMS 的 10 分位（比最小值稳健）
    floor = max(float(np.percentile(rms, 10)) * 1.0, 1e-9)
    sil_gate = max(floor * _SIL_FLOOR_RATIO, _ABS_SIL_RMS / 32768.0)

    kinds: list[str] = []
    freqs: list[float] = []
    win = np.hanning(frame)
    for i in range(n):
        seg = mono[i * frame:(i + 1) * frame]
        if rms[i] < sil_gate:
            kinds.append("sil")
            freqs.append(0.0)
            continue
        spec = np.abs(np.fft.rfft(seg * win)) ** 2
        total = float(spec.sum())
        if total <= 0:
            kinds.append("sil")
            freqs.append(0.0)
            continue
        top3 = float(np.sort(spec)[-3:].sum()) if spec.size >= 3 else total
        dom = int(np.argmax(spec))
        freqs.append(float(np.fft.rfftfreq(frame, 1.0 / sr)[dom]))
        if top3 / total >= _TONE_CONCENTRATION:
            kinds.append("tone")
        else:
            kinds.append("other")
    return kinds, freqs, rms, sil_gate


def _find_speech_onset(kinds: list[str], freqs: list[float],
                       rms: np.ndarray, sil_gate: float) -> tuple[int, bool] | None:
    """从头扫描，返回 (语音起点帧号, 是否确认纯音前奏)；无可裁前奏返回 None。

    前奏帧 = 静音帧，或与既有前奏纯音「主频+振幅」双重一致的纯音帧
    （GLM-TTS 提示音逐采样恒定；男声元音虽也是纯音帧，但主频 100-200Hz、
    振幅持续变化，双重一致判据可可靠区分）。
    前奏帧之外即内容：孤立内容瞬态（后 2 帧皆静音，例如爆点）跳过；
    持续内容（3 帧窗内 ≥2 帧非静音）即语音起点。找到后再按迟滞门限
    （sil_gate 的 0.3 倍）向前回退，把语音的弱起音也保住。
    """
    tone_f: list[float] = []   # 前奏纯音主频（滚动中位数做基准）
    tone_r: list[float] = []   # 前奏纯音 RMS
    saw_tone = False
    n = len(kinds)
    i = 0
    while i < n:
        k = kinds[i]
        is_content = (k == "other")
        if k == "tone":
            if tone_f:
                mf = float(np.median(tone_f))
                mr = float(np.median(tone_r))
                same_f = abs(freqs[i] - mf) <= max(_TONE_FREQ_TOL * mf,
                                                   _TONE_FREQ_TOL_HZ)
                same_r = abs(rms[i] - mr) <= _TONE_RMS_TOL * mr
                if same_f and same_r:
                    tone_f.append(freqs[i])
                    tone_r.append(float(rms[i]))
                    i += 1
                    continue
                is_content = True      # 频率/振幅漂移的纯音 = 语音起音
            else:
                tone_f.append(freqs[i])
                tone_r.append(float(rms[i]))
                saw_tone = True
                i += 1
                continue
        elif k == "sil":
            i += 1
            continue
        if is_content:
            # 持续性：3 帧窗内 ≥2 帧非静音才算语音（孤立爆点跳过）
            nonsil = sum(1 for j in range(i, min(i + 3, n)) if kinds[j] != "sil")
            if nonsil >= _MIN_ONSET_FRAMES and i + 1 < n:
                if i == 0:
                    return None        # 开头即是语音：无前奏
                if saw_tone and i * _FRAME_MS < _MIN_PREAMBLE_MS:
                    return None        # 前奏太短，不值得裁
                # 迟滞回退：保住紧跟语音前的弱起音帧
                while i > 0 and kinds[i - 1] == "sil" and rms[i - 1] > sil_gate * 0.3:
                    i -= 1
                return i, saw_tone
            i += 1                     # 孤立瞬态：跳过继续找
    return None                        # 整段都是静音/纯音：不裁


def clean_wav_bytes(data: bytes) -> bytes:
    """净化 wav 字节：裁前奏/前导静音/爆点 + 去直流 + 10ms 淡入。

    任何异常或无法解析 → 原样返回 data（绝不弄坏音频）。
    """
    try:
        return _clean_wav_bytes_inner(data)
    except Exception:
        return data


def _clean_wav_bytes_inner(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        nfr = w.getnframes()
        comptype = w.getcomptype()
        raw = w.readframes(nfr)
    if comptype != "NONE" or nch < 1 or sr <= 0:
        return data
    a = _decode_pcm(raw, sw)
    if a is None or a.size == 0:
        return data
    if a.size % nch != 0:
        return data
    frames = a.reshape(-1, nch)
    mono = frames.mean(axis=1)

    total_ms = len(mono) / sr * 1000.0
    kinds, freqs, rms, sil_gate = _classify_frames(mono, sr)
    found = _find_speech_onset(kinds, freqs, rms, sil_gate)

    cut = 0
    if found is not None:
        onset_frame, saw_tone = found
        cut = onset_frame * int(sr * _FRAME_MS / 1000) - int(sr * _MARGIN_MS / 1000)
        cut = max(0, cut)
        cut_ms = cut / sr * 1000.0
        remain_ms = total_ms - cut_ms
        if saw_tone:
            # 已确认结构化纯音前奏：短句的前奏占比可超 9 成（实测「你好。」
            # 前奏 1.86s / 全长 2.25s），放宽比例闸，只保裁剪量与剩余下限
            if cut_ms > _MAX_TRIM_MS or remain_ms < _MIN_REMAIN_TONE_MS:
                cut = 0
        else:
            # 仅前导静音/瞬态：保守三重闸（裁剪量、剩余量、比例）
            if (cut_ms > _MAX_TRIM_MS
                    or remain_ms < _MIN_REMAIN_MS
                    or cut > len(mono) // 2):
                cut = 0

    out = frames[cut:] if cut > 0 else frames.copy()
    if out.shape[0] == 0:
        return data

    # 去直流偏移（整体减均值，逐声道）
    out = out - out.mean(axis=0, keepdims=True)

    # 线性淡入（消灭裁切点/起始零交叉爆音）
    fade_n = min(out.shape[0], int(sr * _FADE_MS / 1000))
    if fade_n > 1:
        ramp = np.linspace(0.0, 1.0, fade_n, endpoint=True).reshape(-1, 1)
        out[:fade_n] *= ramp

    payload = _encode_pcm(out.reshape(-1), sw)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        w.writeframes(payload)
    return buf.getvalue()


# 兼容旧式导入
__all__ = ["clean_wav_bytes"]
