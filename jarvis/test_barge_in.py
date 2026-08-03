# -*- coding: utf-8 -*-
"""P3 全双工打断 · 单元测试（不碰真实麦克风，全部合成帧注入）。

覆盖：
    1. _VoiceTrigger —— 安静环境不触发
    2. _VoiceTrigger —— 持续高能 13 帧触发
    3. _VoiceTrigger —— 短促杂音（<13 帧）不触发，且连续计数重置
    4. watch_for_voice —— 触发后回调恰好一次并返回
    5. watch_for_voice —— stop_event 置位时安静退出、不回调
    6. watch_for_voice —— 标定期含播报声底噪时，轻声人声不触发、正常人声触发
"""
import threading

import numpy as np

import ears


def _帧(rms: float, n: int = ears.帧样本数) -> np.ndarray:
    """造一帧恒定幅值约等于 rms 的合成音频。"""
    return np.full(n, rms, dtype=np.float32)


def test_安静不触发():
    trig = ears._VoiceTrigger(env_rms=0.005)
    for _ in range(200):
        assert not trig.feed(0.006)  # 低于阈值 0.0125
    print("✅ 1/6 安静环境不触发")


def test_持续高能触发():
    trig = ears._VoiceTrigger(env_rms=0.005)
    for i in range(ears.打断触发帧数 - 1):
        assert not trig.feed(0.1)
    assert trig.feed(0.1)  # 第 13 帧触发
    print("✅ 2/6 持续高能 13 帧触发")


def test_短促杂音不触发且重置():
    trig = ears._VoiceTrigger(env_rms=0.005)
    for _ in range(ears.打断触发帧数 - 1):  # 12 帧高能：咳嗽
        assert not trig.feed(0.2)
    assert not trig.feed(0.0)  # 掉一帧 → 计数清零
    for _ in range(ears.打断触发帧数 - 1):  # 再来 12 帧仍不触发
        assert not trig.feed(0.2)
    assert trig.feed(0.2)  # 补满 13 帧才触发
    print("✅ 3/6 短促杂音不触发、连续计数重置")


def test_回调恰好一次():
    fired = []
    stop = threading.Event()
    frames = [_帧(0.005)] * 10 + [_帧(0.1)] * 30  # 标定 10 帧安静 → 持续人声
    ears.watch_for_voice(lambda: fired.append(1), stop, frame_source=frames)
    assert fired == [1], f"回调应为恰好一次，实际 {len(fired)}"
    print("✅ 4/6 触发后回调恰好一次并返回")


def test_stop_event_安静退出():
    fired = []
    stop = threading.Event()
    frames = []

    def 帧源():
        yield from [_帧(0.005)] * 10  # 标定段
        stop.set()
        yield from [_帧(0.2)] * 100  # stop 后即使高能也不应回调

    ears.watch_for_voice(lambda: fired.append(1), stop, frame_source=帧源())
    assert not fired, "stop_event 置位后不应回调"
    print("✅ 5/6 stop_event 置位安静退出、不回调")


def test_播报底噪上轻声不触发正常触发():
    # 模拟：标定期是 Nolan 自己的播报声（RMS≈0.05），阈值≈0.125
    fired = []
    stop = threading.Event()
    frames = [_帧(0.05)] * 10 + [_帧(0.08)] * 30  # 轻声人声低于阈值
    ears.watch_for_voice(lambda: fired.append(1), stop, frame_source=frames)
    assert not fired, "播报声底噪上轻声人声不应误触"

    fired2 = []
    frames2 = [_帧(0.05)] * 10 + [_帧(0.3)] * 30  # 正常音量人声远高于阈值
    ears.watch_for_voice(lambda: fired2.append(1), stop, frame_source=frames2)
    assert fired2 == [1], "正常音量人声应触发打断"
    print("✅ 6/6 播报底噪：轻声不误触、正常音量触发")


if __name__ == "__main__":
    test_安静不触发()
    test_持续高能触发()
    test_短促杂音不触发且重置()
    test_回调恰好一次()
    test_stop_event_安静退出()
    test_播报底噪上轻声不触发正常触发()
    print("\n🎉 P3 打断监听单元测试 6/6 全过")
