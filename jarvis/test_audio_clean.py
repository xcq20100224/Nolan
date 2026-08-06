# -*- coding: utf-8 -*-
"""audio_clean 测试：numpy 合成样本 + 真实 GLM-TTS wav 前后对比。

运行：cd jarvis && python test_audio_clean.py
"""
import io
import os
import wave

import numpy as np

from audio_clean import clean_wav_bytes

HERE = os.path.dirname(os.path.abspath(__file__))
DIAG_DIR = os.path.join(HERE, "files", "diag")


# ===== 工具 =====

def make_wav(samples: np.ndarray, sr: int, nch: int = 1) -> bytes:
    """float64 [-1,1] → 16bit PCM wav 字节。samples 形状 (n,) 或 (n, nch)。"""
    a = np.clip(samples, -1.0, 1.0)
    if nch > 1:
        assert a.ndim == 2 and a.shape[1] == nch
        a = a.reshape(-1)
    payload = (a * 32767.0).round().astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(payload)
    return buf.getvalue()


def read_wav(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch)
    return a, sr, nch


def speech_like(n: int, sr: int, phase0: float = 0.0) -> np.ndarray:
    """类语音信号：泛音丰富 + 基频抖动 + 幅度起伏（绝不会被判成纯音）。"""
    t = np.arange(n) / sr
    f0 = 120.0 + 15.0 * np.sin(2 * np.pi * 3.0 * t)      # 基频抖动
    phase = 2 * np.pi * np.cumsum(f0) / sr + phase0
    sig = np.zeros(n)
    for h, amp in [(1, 0.5), (2, 0.3), (3, 0.2), (5, 0.12), (7, 0.06)]:
        sig += amp * np.sin(h * phase + 0.3 * h)
    sig *= 0.6 + 0.4 * np.sin(2 * np.pi * 2.0 * t + 1.0)  # 幅度起伏
    return np.clip(sig, -0.9, 0.9)


def beep_preamble(sr: int) -> np.ndarray:
    """复刻 GLM-TTS 前奏：550Hz 纯音频发（amp≈1794/32768）+ 静音，约 1.8s。"""
    amp = 1794.0 / 32768.0
    t = np.arange(int(sr * 1.85)) / sr
    tone = amp * np.sin(2 * np.pi * 550.0 * t)
    gate = np.zeros_like(tone)
    # 实测节奏：响120ms 静160ms 响360ms 静690ms 响120ms 静160ms 响120ms
    spans = [(0.00, 0.12), (0.28, 0.64), (1.33, 1.45), (1.61, 1.73)]
    for s, e in spans:
        gate[int(s * sr):int(e * sr)] = 1.0
    return tone * gate


def corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a ** 2).sum() * (b ** 2).sum()))
    return float((a * b).sum() / d) if d > 0 else 0.0


def rms_head(data: bytes, ms: int = 100) -> float:
    a, sr, nch = read_wav(data)
    m = a if a.ndim == 1 else a.mean(axis=1)
    h = m[: int(sr * ms / 1000)]
    return float(np.sqrt(np.mean(h ** 2))) * 32768.0  # int16 标尺


results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# ===== 测试 1：50ms 前导静音 + 爆点 → 爆点消失、语音保留 =====
sr = 24000
sil = np.zeros(int(sr * 0.05))
click_at = int(sr * 0.025)
sil[click_at] = 0.9                      # 单个采样爆点
speech = speech_like(int(sr * 1.0), sr)
raw = np.concatenate([sil, speech])
cleaned = clean_wav_bytes(make_wav(raw, sr))
ca, csr, _ = read_wav(cleaned)
dur_cut_ms = (len(raw) - len(ca)) / sr * 1000
# 爆点在 25ms 处：裁掉 ≥30ms 即证明爆点消失；语音保真度须 >0.99
offset = len(raw) - len(ca)
tail_corr = corr(ca[500:], raw[offset + 500:])
check("T1 前导静音+爆点", 25 <= dur_cut_ms <= 80 and tail_corr > 0.99,
      f"裁掉={dur_cut_ms:.0f}ms（爆点在25ms） 语音相关={tail_corr:.4f}")

# ===== 测试 2：直流偏移 → 起始采样≈0 =====
sr = 16000
speech = speech_like(int(sr * 0.8), sr) + 0.08          # 加直流
cleaned = clean_wav_bytes(make_wav(speech, sr))
ca, csr, _ = read_wav(cleaned)
dc = float(ca.mean())
first10_max = float(np.max(np.abs(ca[:10])))
check("T2 直流偏移", abs(dc) < 0.005 and first10_max < 0.04,
      f"净化后均值={dc:.5f} 前10采样峰值={first10_max:.4f}")

# ===== 测试 3：干净样本 → 几乎不变（相似度>99%）=====
sr = 24000
speech = speech_like(int(sr * 1.2), sr, phase0=1.3)  # 开头即有能量，无前导静音
orig_bytes = make_wav(speech, sr)
cleaned = clean_wav_bytes(orig_bytes)
ca, _, _ = read_wav(cleaned)
oa, _, _ = read_wav(orig_bytes)
same_len = len(ca) == len(oa)
c = corr(ca, oa) if same_len else 0.0
check("T3 干净样本几乎不变", same_len and c > 0.99, f"等长={same_len} 相关={c:.4f}")

# ===== 测试 4：畸形字节 → 原样返回不抛 =====
for junk in [b"not a wav at all", b"RIFF\x00\x01", b"", b"\x00" * 100]:
    out = clean_wav_bytes(junk)
    ok = out == junk
    if not ok:
        check("T4 畸形字节原样返回", False, f"len={len(junk)} 被改动")
        break
else:
    check("T4 畸形字节原样返回", True, "4 组垃圾输入全部原样返回")

# ===== 测试 5：48kHz 双声道 模拟 GLM 前奏 → 裁掉 =====
sr = 48000
pre = beep_preamble(sr)
speech = speech_like(int(sr * 1.0), sr)
raw = np.concatenate([pre, speech])
stereo = np.stack([raw, raw * 0.8], axis=1)
cleaned = clean_wav_bytes(make_wav(stereo, sr, nch=2))
ca, csr, cnch = read_wav(cleaned)
dur_left = len(ca) / csr * 1000
mono = ca.mean(axis=1)
# 净化后开头不应再有 550Hz 持续纯音：前 100ms 频谱 top3 占比
seg = mono[: int(csr * 0.1)] * np.hanning(int(csr * 0.1))
spec = np.abs(np.fft.rfft(seg)) ** 2
conc = float(np.sort(spec)[-3:].sum() / spec.sum()) if spec.sum() > 0 else 0.0
check("T5 48k立体声 模拟前奏", cnch == 2 and dur_left < 1300 and conc < 0.8,
      f"剩余={dur_left:.0f}ms 开头100ms频谱集中度={conc:.2f}")

# ===== 测试 6：8kHz 单声道 模拟前奏 → 裁掉 =====
sr = 8000
raw = np.concatenate([beep_preamble(sr), speech_like(int(sr * 1.0), sr)])
cleaned = clean_wav_bytes(make_wav(raw, sr))
ca, csr, _ = read_wav(cleaned)
dur_left = len(ca) / csr * 1000
check("T6 8k单声道 模拟前奏", dur_left < 1300, f"剩余={dur_left:.0f}ms")

# ===== 测试 7：真实 GLM-TTS wav 前后对比 =====
for tag in ("glm_real_short", "glm_real_long_first"):
    path = os.path.join(DIAG_DIR, f"{tag}.wav")
    if not os.path.isfile(path):
        check(f"T7 真实样本 {tag}", False, "样本不存在（先跑 _diag_real_glm.py）")
        continue
    with open(path, "rb") as fh:
        orig = fh.read()
    cleaned = clean_wav_bytes(orig)
    oa, osr, _ = read_wav(orig)
    ca, csr, _ = read_wav(cleaned)
    cut_ms = (len(oa) - len(ca)) / osr * 1000
    r_before = rms_head(orig)
    r_after = rms_head(cleaned)
    # 前奏特征校验：净化后开头 100ms 不应再是 550Hz 稳态纯音（top3 集中度 <0.8）
    m = ca if ca.ndim == 1 else ca.mean(axis=1)
    hn = int(csr * 0.1)
    spec = np.abs(np.fft.rfft(m[:hn] * np.hanning(hn))) ** 2
    conc = float(np.sort(spec)[-3:].sum() / spec.sum()) if spec.sum() > 0 else 0.0
    # 内容保真：净化段（跳过淡入）应与原文件裁点后的采样一致
    offset = len(oa) - len(ca)
    seg = min(20000, len(ca) - 300)
    fidelity = corr(ca[300:300 + seg], oa[offset + 300: offset + 300 + seg])
    ok = cut_ms > 1500 and conc < 0.8 and fidelity > 0.99
    check(f"T7 真实样本 {tag}", ok,
          f"裁掉={cut_ms:.0f}ms 头100msRMS {r_before:.0f}→{r_after:.0f} "
          f"开头频谱集中度={conc:.2f} 保真相关={fidelity:.4f}")

# ===== 汇总 =====
n_fail = sum(1 for _, ok, _ in results if not ok)
print(f"\n==== {len(results) - n_fail}/{len(results)} 通过 ====")
raise SystemExit(1 if n_fail else 0)
