# -*- coding: utf-8 -*-
"""
voiceprint.py —— Nolan 的声纹门禁（轻量级）🪪

第一性原理动机
--------------
唤醒词解决「说了什么」，声纹解决「谁在说」。唤醒词工作在文本层（ASR 之后），
任何人说出「Nolan / 诺兰」都会命中；要只在主人的声音下开门，必须在声学层区分。
人声的个体差异主要来自两处：
  1) 声道谱包络（共振峰分布）—— MFCC 正是它的紧凑表示。本模块刻意丢弃 c0
     能量系数：响度随说话距离/音量变化，不是身份特征，留下反而添乱；
  2) 声带基频 F0（音高）—— MFCC 在设计上抹掉了基频，需用自相关法单独补一维。
模板 = 注册语音全部有声帧特征的「均值 + 协方差」摘要；
打分 = MFCC 余弦相似度 × 马氏距离衰减。两个视角互补：
余弦看「谱形状像不像」，马氏距离看「落在注册分布里的典型程度」。

诚实边界
--------
- 这是轻量质心模板，不是安防级说话人确认：
  * 能防：家人/同事/电视里随口一句「Nolan」造成的非刻意误唤醒；
  * 防不了：刻意模仿、录音回放、嗓音天生相近者的针对性尝试。
- 未注册模板时 verify() 恒返回 (True, 1.0) —— 没录入就不过滤，
  绝不把主人锁在门外（可用性优先于安全性；门禁默认关闭，见 ears.py）。
- 依赖仅 numpy（本机已确认 2.4.4 可用），MFCC 手写实现，零第三方包。
"""

import json
import os
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

# ========== 存储与判定阈值 ==========
# 测试通过 monkeypatch 本变量指向临时目录，不碰真实模板
_存储路径 = Path(__file__).resolve().parent / "data" / "voiceprint.json"

# 判定阈值：分数 = 谱余弦 × 马氏衰减，同人显著高于异人时阈值取两者之间。
# 校准依据见 test_voiceprint.py 打印的分数分布（合成说话人：同人 ≈0.9+，异人 ≈0.5-）。
_阈值 = 0.75

_特征维数 = 13          # MFCC c1..c12（12 维，弃 c0）+ log2 基频（1 维）
_MFCC系数数 = 13        # DCT 输出总数（含 c0，随后弃用 c0）
_梅尔滤波器数 = 26
_FFT点数 = 512
_最少有声帧 = 25        # ≈0.25s 有效语音；不足则拒注册/拒打分
_协方差地板 = 0.02      # 每维方差下限：防合成/平稳语音协方差退化导致马氏距离爆炸


# ========== MFCC 手写实现（numpy，~60 行） ==========
def _分帧(信号: np.ndarray, 帧长: int, 帧移: int) -> np.ndarray:
    """把一维信号切成 (帧数, 帧长) 的重叠帧矩阵；不足一帧则补零成一帧。"""
    if len(信号) <= 帧长:
        return np.pad(信号, (0, 帧长 - len(信号))).reshape(1, 帧长)
    总数 = 1 + (len(信号) - 帧长) // 帧移
    索引 = np.arange(帧长)[None, :] + 帧移 * np.arange(总数)[:, None]
    return 信号[索引]


@lru_cache(maxsize=4)
def _梅尔滤波器组(个数: int, fft点数: int, 采样率: int) -> np.ndarray:
    """三角形梅尔滤波器组 (个数, fft点数//2+1)。按 (个数,点数,采样率) 缓存。"""
    最高梅尔 = 2595.0 * np.log10(1.0 + (采样率 / 2.0) / 700.0)
    赫兹点 = 700.0 * (10.0 ** (np.linspace(0.0, 最高梅尔, 个数 + 2) / 2595.0) - 1.0)
    槽 = np.minimum(fft点数 // 2, np.floor((fft点数 + 1) * 赫兹点 / 采样率).astype(int))
    组 = np.zeros((个数, fft点数 // 2 + 1))
    for i in range(个数):
        左, 中, 右 = int(槽[i]), int(槽[i + 1]), int(槽[i + 2])
        if 中 <= 左:
            中 = 左 + 1
        if 右 <= 中:
            右 = 中 + 1
        右 = min(右, fft点数 // 2 + 1)
        if 中 >= 右:
            continue  # 高频端频槽并合，跳过退化三角形
        组[i, 左:中] = (np.arange(左, 中) - 左) / float(中 - 左)
        组[i, 中:右] = (右 - np.arange(中, 右)) / float(右 - 中)
    return 组


def _逐帧基频(帧: np.ndarray, 采样率: int) -> np.ndarray:
    """自相关法估每帧基频（搜索 50~400Hz 滞后区间）；非周期/过轻帧记 0。"""
    最小滞 = max(1, int(采样率 / 400))
    最大滞 = min(帧.shape[1] - 1, int(采样率 / 50))
    结果 = np.zeros(len(帧))
    for i, f in enumerate(帧):
        f = f - f.mean()
        ac = np.correlate(f, f, mode="full")[len(f) - 1:]
        if ac[0] <= 1e-10:
            continue
        段 = ac[最小滞:最大滞 + 1]
        if 段.size == 0:
            continue
        滞 = 最小滞 + int(np.argmax(段))
        if ac[滞] < 0.3 * ac[0]:
            continue  # 峰不够突出 = 非周期帧（气息/摩擦），不参与基频统计
        结果[i] = 采样率 / 滞
    return 结果


def _提取特征(信号: np.ndarray, 采样率: int) -> np.ndarray | None:
    """提取有声帧特征矩阵 (N, 13)：MFCC c1..c12 + log2(f0/100)。

    只用「有声且周期」的帧进统计：
    - 静音不含身份信息，只会稀释模板（RMS ≥ 峰值 20% 过滤）；
    - 纯噪声/敲击没有周期性，不是「语音」——声纹管的是人声，
      周期帧不足 35% 的信号直接判为退化输入返回 None。
    有效语音不足返回 None（调用方据此拒注册/拒打分）。
    """
    信号 = np.asarray(信号, dtype=np.float64).ravel()
    if 信号.size < int(0.3 * 采样率):
        return None
    加重 = np.concatenate(([信号[0]], 信号[1:] - 0.97 * 信号[:-1]))  # 预加重补偿高频衰减
    帧长, 帧移 = int(0.025 * 采样率), int(0.010 * 采样率)
    帧 = _分帧(加重, 帧长, 帧移)
    rms = np.sqrt(np.mean(帧 ** 2, axis=1))
    if rms.max() <= 1e-6:
        return None
    帧 = 帧[rms >= 0.2 * rms.max()]
    if len(帧) < _最少有声帧:
        return None
    功率 = np.abs(np.fft.rfft(帧 * np.hamming(帧长), n=_FFT点数, axis=1)) ** 2
    梅尔能量 = 功率 @ _梅尔滤波器组(_梅尔滤波器数, _FFT点数, 采样率).T
    对数能量 = np.log(np.maximum(梅尔能量, 1e-10))
    n = np.arange(_梅尔滤波器数)
    dct基 = np.cos(np.pi * np.arange(_MFCC系数数)[:, None] * (n[None, :] + 0.5) / _梅尔滤波器数)
    mfcc = 对数能量 @ dct基.T                       # (N, 13) 含 c0
    f0 = _逐帧基频(帧, 采样率)
    周期 = f0 > 0
    if int(周期.sum()) < max(_最少有声帧, int(0.35 * len(帧))):
        return None                                  # 非人声（噪声/耳语/敲击）：不建档不打分
    mfcc, f0有效 = mfcc[周期], f0[周期]
    logf0 = np.log2(f0有效 / 100.0)
    return np.column_stack([mfcc[:, 1:], logf0])    # 弃 c0：响度≠身份


# ========== 模板读写（原子写入） ==========
def _载入模板() -> tuple[np.ndarray, np.ndarray] | None:
    """读取模板为 (均值, 协方差逆)；文件缺失/损坏/形状不符一律返回 None。"""
    try:
        数据 = json.loads(_存储路径.read_text(encoding="utf-8"))
        均值 = np.asarray(数据["mean"], dtype=np.float64)
        逆 = np.asarray(数据["inv_cov"], dtype=np.float64)
        if 均值.shape != (_特征维数,) or 逆.shape != (_特征维数, _特征维数):
            return None
        return 均值, 逆
    except Exception:
        return None


def is_enrolled() -> bool:
    """是否已有可用声纹模板。"""
    return _载入模板() is not None


def enroll(samples: list[np.ndarray], sample_rate: int) -> bool:
    """用若干段主人录音建声纹模板（MFCC 均值 + 协方差摘要），原子写盘。

    所有样本的有效帧堆叠成一个点云估计分布——样本间自然的音高/语速波动
    正是协方差的来源，它决定了 verify 对主人临场状态波动的容忍度。
    有效语音不足返回 False（不写盘、不覆盖旧模板）。
    """
    帧们 = []
    for s in samples:
        f = _提取特征(s, sample_rate)
        if f is not None:
            帧们.append(f)
    if not 帧们:
        print("🪪 声纹：注册失败——样本里没有足够的有效语音 😢")
        return False
    全部 = np.vstack(帧们)
    if len(全部) < _最少有声帧 * 2:
        print(f"🪪 声纹：注册失败——有效帧不足（{len(全部)}）😢")
        return False
    均值 = 全部.mean(axis=0)
    协方差 = np.cov(全部, rowvar=False)
    if 协方差.shape != (_特征维数, _特征维数):
        return False
    # 正则化：15% 自身方差 + 绝对地板，防退化/奇异，也给基频维留 ±10% 容忍带
    协方差 += np.diag(np.diag(协方差) * 0.15 + _协方差地板)
    模板 = {
        "version": 1,
        "sample_rate": int(sample_rate),
        "mean": 均值.tolist(),
        "inv_cov": np.linalg.inv(协方差).tolist(),
        "frames": int(len(全部)),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _存储路径.parent.mkdir(parents=True, exist_ok=True)
    临时 = _存储路径.with_name(_存储路径.name + ".tmp")
    临时.write_text(json.dumps(模板, ensure_ascii=False), encoding="utf-8")
    os.replace(临时, _存储路径)  # 同目录原子替换，杜绝半截 JSON
    print(f"🪪 声纹：注册完成 ✅（{len(全部)} 帧 → {_存储路径.name}）")
    return True


def unenroll() -> bool:
    """删除声纹模板；删完即回到「恒放行」状态。"""
    try:
        _存储路径.unlink(missing_ok=True)
        print("🪪 声纹：模板已删除 🗑️")
        return True
    except Exception as 错误:
        print(f"🪪 声纹：删除失败 —— {错误}")
        return False


def verify(sample: np.ndarray, sample_rate: int) -> tuple[bool, float]:
    """给一段语音打分并判定是否主人。返回 (是否通过, 分数)。

    分数 = MFCC 余弦相似度 × exp(-马氏距离²/50)：
    余弦管「谱形状方向像不像」，马氏距离管「在注册分布里典不典型」。
    未注册模板时恒返回 (True, 1.0)——没录入就不过滤，绝不把主人锁在门外。
    提取不到有效语音（纯噪声/过短）返回 (False, 0.0)：无法证明是主人。
    """
    模板 = _载入模板()
    if 模板 is None:
        return True, 1.0
    特征 = _提取特征(sample, sample_rate)
    if 特征 is None:
        return False, 0.0
    均值, 逆协方差 = 模板
    x = 特征.mean(axis=0)
    谱余弦 = float(
        np.dot(x[:12], 均值[:12])
        / (np.linalg.norm(x[:12]) * np.linalg.norm(均值[:12]) + 1e-12)
    )
    差 = x - 均值
    马氏 = float(np.sqrt(max(0.0, 差 @ 逆协方差 @ 差)))
    分数 = 谱余弦 * float(np.exp(-(马氏 ** 2) / 50.0))
    return 分数 >= _阈值, 分数
