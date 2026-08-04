# -*- coding: utf-8 -*-
"""
test_voiceprint.py —— 声纹门禁 + 耳朵 VAD 调优的纯合成信号单元测试 🧪

第一性原理：声纹方案的合法性建立在「同一个人多次说话的特征距离，系统性小于
不同人之间的距离」上。本测试用参数化合成语音（可控基频 + 可控共振峰包络的
谐波叠加信号）构造「主人 / 主人变体 / 异人」三类信号，验证排序与阈值判定。
全部信号内存合成，不碰真实麦克风，不写真实模板文件（monkeypatch 存储路径）。
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 保证能 import 同目录模块

import voiceprint  # noqa: E402

采样率 = 16000


def _合成说话人(基频: float, 共振峰: list[float], 秒: float = 2.0,
                噪声: float = 0.004, 种子: int = 0, 幅度: float = 0.3) -> np.ndarray:
    """谐波叠加 + 共振峰包络合成「像人话」的信号。

    每个谐波 h*f0 的幅度由共振峰包络 A(f)=Σ exp(-((f-Fk)/带宽)²) 决定，
    因此基频控制音高维、共振峰控制 MFCC 频谱维——正好对应声纹的两个身份来源。
    """
    rng = np.random.default_rng(种子)
    t = np.arange(int(采样率 * 秒)) / 采样率
    sig = np.zeros_like(t)
    h = 1
    while 基频 * h < 采样率 / 2 - 100:
        f = 基频 * h
        包络 = sum(np.exp(-((f - F) / 260.0) ** 2) for F in 共振峰) + 0.05
        sig += (包络 / h) * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
        h += 1
    sig *= 幅度 / (np.abs(sig).max() + 1e-9)
    sig += rng.standard_normal(sig.size) * 噪声
    return sig.astype(np.float32)


# ---- 三位「说话人」参数 ----
主人 = dict(基频=120.0, 共振峰=[500, 1500, 2500])
异人高音 = dict(基频=210.0, 共振峰=[650, 1900, 2900])   # 音高+频谱都不同
异人同音高 = dict(基频=123.0, 共振峰=[380, 1100, 2100])  # 音高几乎相同，只有频谱不同


def _主人样本(种子: int, 微调: float = 1.0) -> np.ndarray:
    """主人的一次「临场发挥」：基频/共振峰带 ±2% 自然波动。"""
    return _合成说话人(主人["基频"] * 微调,
                       [F * 微调 for F in 主人["共振峰"]], 种子=种子)


def test_注册与排序(tmp路径: Path) -> list[tuple[str, float]]:
    """核心断言：同人高分通过、异人低分拒绝，且严格排序。"""
    voiceprint._存储路径 = tmp路径 / "voiceprint.json"

    ok = voiceprint.enroll([_主人样本(11, 1.00), _主人样本(12, 1.02), _主人样本(13, 0.98)], 采样率)
    assert ok, "主人 3 段注册应成功"
    assert voiceprint.is_enrolled(), "注册后 is_enrolled 应为 True"
    assert voiceprint._存储路径.exists(), "模板文件应已落盘"

    分数表 = []
    案例 = [
        ("主人变体A", _主人样本(21, 1.03), True),
        ("主人变体B(小声)", _主人样本(22, 0.97) * 0.4, True),   # 音量缩放不应影响判定
        ("异人_高音", _合成说话人(**异人高音, 种子=31), False),
        ("异人_同音高", _合成说话人(**异人同音高, 种子=32), False),
    ]
    for 名, 信号, 期望通过 in 案例:
        通过, 分数 = voiceprint.verify(信号, 采样率)
        分数表.append((名, 分数))
        assert 通过 == 期望通过, f"{名}：期望通过={期望通过}，实际={通过}（分数 {分数:.3f}）"

    同人最低 = min(s for n, s in 分数表 if n.startswith("主人"))
    异人最高 = max(s for n, s in 分数表 if n.startswith("异人"))
    assert 同人最低 > 异人最高, f"排序错误：同人最低 {同人最低:.3f} ≤ 异人最高 {异人最高:.3f}"
    print(f"✅ 注册/排序：同人最低 {同人最低:.3f} > 异人最高 {异人最高:.3f}（阈值 {voiceprint._阈值}）")
    return 分数表


def test_未注册恒放行(tmp路径: Path) -> None:
    """没录入模板就不过滤——绝不把主人锁在门外。"""
    voiceprint._存储路径 = tmp路径 / "不存在的目录" / "voiceprint.json"
    assert not voiceprint.is_enrolled()
    通过, 分数 = voiceprint.verify(_合成说话人(**异人高音, 种子=41), 采样率)
    assert (通过, 分数) == (True, 1.0), "未注册时任何人（含异人）都应放行"
    print("✅ 未注册恒放行：verify → (True, 1.0)")


def test_注销(tmp路径: Path) -> None:
    """unenroll 后回到恒放行状态。"""
    voiceprint._存储路径 = tmp路径 / "voiceprint.json"
    assert voiceprint.enroll([_主人样本(51), _主人样本(52, 1.01), _主人样本(53, 0.99)], 采样率)
    assert voiceprint.unenroll()
    assert not voiceprint.is_enrolled()
    通过, 分数 = voiceprint.verify(_主人样本(54), 采样率)
    assert (通过, 分数) == (True, 1.0)
    print("✅ 注销后恢复恒放行")


def test_拒绝退化输入(tmp路径: Path) -> None:
    """纯静音/纯噪声/过短片段：注册拒绝、验证拒绝，且不崩溃。"""
    voiceprint._存储路径 = tmp路径 / "voiceprint.json"
    静音 = np.zeros(采样率, dtype=np.float32)
    rng = np.random.default_rng(7)
    纯噪声 = (rng.standard_normal(采样率 * 2) * 0.01).astype(np.float32)
    过短 = _主人样本(61)[: int(采样率 * 0.1)]

    assert not voiceprint.enroll([静音, 纯噪声], 采样率), "静音+噪声不应能注册"
    assert not voiceprint._存储路径.exists(), "注册失败不应写盘"

    # 先注册主人，再验证退化输入应被拒绝（无法证明是主人）
    assert voiceprint.enroll([_主人样本(62), _主人样本(63, 1.01), _主人样本(64, 0.99)], 采样率)
    for 名, 信号 in [("静音", 静音), ("纯噪声", 纯噪声), ("过短", 过短)]:
        通过, 分数 = voiceprint.verify(信号, 采样率)
        assert not 通过 and 分数 == 0.0, f"{名} 应被拒绝，实际 {通过}/{分数}"
    print("✅ 退化输入（静音/噪声/过短）注册与验证均拒绝且不崩溃")


def test_模板损坏不锁门(tmp路径: Path) -> None:
    """模板 JSON 损坏 = 视为未注册，恒放行（可用性优先）。"""
    voiceprint._存储路径 = tmp路径 / "voiceprint.json"
    tmp路径.mkdir(parents=True, exist_ok=True)
    voiceprint._存储路径.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert not voiceprint.is_enrolled()
    通过, 分数 = voiceprint.verify(_主人样本(71), 采样率)
    assert (通过, 分数) == (True, 1.0)
    print("✅ 模板损坏不锁门：verify → (True, 1.0)")


def test_耳朵vad默认兼容与突发免疫() -> None:
    """ears.py 侧：默认参数与旧版一致；突发免疫判定逻辑正确（纯函数）。"""
    import ears

    # 默认值回归：环境变量未设置时必须与旧版一字不差
    assert ears.环境标定秒 == 0.5 and ears.阈值倍率 == 2.5 and ears.最小阈值 == 0.01
    assert ears.语音起判帧数 == 3 and ears.静音结束秒 == 1.0
    assert ears._VOICE_GATE is False, "声纹门禁默认必须关闭"

    # 突发免疫判定：150ms 门槛 = 5 帧（30ms/帧）；0 = 关闭恒真
    ears.突发免疫毫秒 = 150
    assert not ears._片段是否语音(4), "120ms 突发（咳嗽）应被免疫"
    assert ears._片段是否语音(5), "150ms 及以上应视为语音"
    ears.突发免疫毫秒 = 0
    assert ears._片段是否语音(1), "免疫关闭时任何片段都应放行（旧行为）"
    ears.突发免疫毫秒 = 150  # 恢复默认，别污染后续测试
    print("✅ ears VAD：默认参数与旧版一致，突发免疫边界（4帧拒/5帧放/0关闭）正确")


class _假流:
    """冒充 sd.RawInputStream：按序吐出预制帧，不碰真实麦克风。"""

    def __init__(self, 帧序列):
        self._迭代器 = iter(帧序列)

    def __enter__(self):
        return self

    def __exit__(self, *参数):
        return False

    def read(self, _n):
        return next(self._迭代器).astype(np.float32).tobytes(), False


def _帧序列(描述: list[tuple[float, int]]):
    """把 [(rms, 帧数), ...] 展开成恒定幅值帧流，末尾无限补静音防迭代耗尽。"""
    import ears
    for rms, 帧数 in 描述:
        for _ in range(帧数):
            yield np.full(ears.帧样本数, rms, dtype=np.float32)
    while True:
        yield np.zeros(ears.帧样本数, dtype=np.float32)


def test_突发免疫_录音路径() -> None:
    """_录音 全流程（假流注入）：咳嗽式突发被忽略并继续值守，随后的真语音正常录到。"""
    import ears
    原流 = ears.sd.RawInputStream
    try:
        # 场景：0.5s 安静标定 → 4 帧(120ms)咳嗽 → 1.1s 静音 → 0.6s 真语音 → 1.1s 尾静音
        ears.sd.RawInputStream = lambda **kw: _假流(_帧序列([
            (0.005, 20),   # 环境标定（阈值 = max(0.005*2.5, 0.01) = 0.0125）
            (0.10, 4),     # 突发噪声：超起判 3 帧但总共只有 120ms < 150ms
            (0.005, 37),   # 尾静音 1.1s → 触发突发免疫，重置回值守
            (0.10, 20),    # 真语音 0.6s
            (0.005, 37),   # 尾静音 → 正常结束
        ]))
        音频 = ears._录音(timeout=30.0)
        assert 音频 is not None, "突发被免疫后应继续值守并录到随后的真语音"
        时长ms = len(音频) / ears.采样率 * 1000
        assert 500 <= 时长ms <= 700, f"录到的应是那段 0.6s 真语音，实际 {时长ms:.0f}ms"

        # 场景：只有咳嗽没有语音 → 超时返回 None（旧版会把咳嗽送进 ASR）
        ears.sd.RawInputStream = lambda **kw: _假流(_帧序列([
            (0.005, 20), (0.10, 4), (0.005, 10**9),
        ]))
        assert ears._录音(timeout=0.3) is None, "纯突发噪声应超时返回 None，不进 ASR"

        # 兼容开关：免疫关闭（=0）时恢复旧行为——咳嗽也会被当作语音录下来
        ears.突发免疫毫秒 = 0
        ears.sd.RawInputStream = lambda **kw: _假流(_帧序列([
            (0.005, 20), (0.10, 4), (0.005, 37),
        ]))
        咳嗽 = ears._录音(timeout=30.0)
        assert 咳嗽 is not None, "NOLAN_BURST_IMMUNE_MS=0 时必须恢复旧版行为"
    finally:
        ears.sd.RawInputStream = 原流
        ears.突发免疫毫秒 = 150
    print("✅ 突发免疫录音路径：咳嗽被忽略并继续值守 / 纯咳嗽超时 None / 开关=0 恢复旧行为")


def main() -> None:
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2, \
         tempfile.TemporaryDirectory() as d3, tempfile.TemporaryDirectory() as d4, \
         tempfile.TemporaryDirectory() as d5:
        test_未注册恒放行(Path(d2))
        分数表 = test_注册与排序(Path(d1))
        test_注销(Path(d3))
        test_拒绝退化输入(Path(d4))
        test_模板损坏不锁门(Path(d5))
    test_耳朵vad默认兼容与突发免疫()
    test_突发免疫_录音路径()
    print("\n—— 分数分布（校准阈值用）——")
    for 名, 分数 in 分数表:
        print(f"  {名:<14} {分数:.3f}")
    print("\n🎉 全部声纹/VAD 单元测试通过（纯合成信号，未碰麦克风）")


if __name__ == "__main__":
    main()
