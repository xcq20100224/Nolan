# -*- coding: utf-8 -*-
"""提示句「恰好一次」播报契约测试（纯逻辑，不起服务、不出声）。

病例（2026-08-17 真实截图）：主动性提示句「先生，上午九点的晨会已过……」
被播报 2 遍。根因：/api/due 只在浏览器轮询时才会被调用（即浏览器必然在场），
却无条件叠加音箱两遍播报——浏览器 1 遍 + 音箱 2 遍，双通道叠音。
且「音箱闹钟」从设计上就覆盖不了离屏场景（没人轮询就不触发）。

契约（恰好一次原则）：
    1. 浏览器在场 + 音频命中 → 音箱 0 遍（浏览器播 1 遍）
    2. 浏览器在场 + 音频冷缓存 → 音箱补 1 遍（提示句绝不无声）
    3. 浏览器离屏 + 提醒/触发 → 音箱 2 遍（闹钟必被听见）
    4. 浏览器离屏 + 主动性提示 → 音箱 1 遍（轻提醒不吵人）
    5. _speak_alarm_async 严格按 times 次数播报（含 0/1/2）

运行：python test_due_voice.py
"""
import importlib.util
import os
import sys
import time

# 动态加载 server.py（不启动 HTTP 服务，只取纯函数，与 test_history_slim 同一模式）
spec = importlib.util.spec_from_file_location(
    "nolan_server", os.path.join(os.path.dirname(__file__), "server.py"))
server = importlib.util.module_from_spec(spec)
sys.modules["nolan_server"] = server
spec.loader.exec_module(server)

_checks = 0


def check(name, cond, detail=""):
    global _checks
    _checks += 1
    assert cond, f"{name}: {detail}"
    print(f"✅ {name}")


class _FakeMouth:
    def __init__(self):
        self.calls = []

    def speak(self, text):
        self.calls.append(text)


def main():
    # == 决策函数：/api/due 在场补位 ==
    check("在场+音频命中→音箱0遍", server._due_gap_cover_times("/api/tts/x.wav") == 0)
    check("在场+音频缺失→音箱补1遍", server._due_gap_cover_times(None) == 0 + 1)

    # == 决策函数：触发循环离屏兜底 ==
    check("在场+提醒→音箱0遍", server._absent_speaker_times("trigger", False) == 0)
    check("在场+主动性→音箱0遍", server._absent_speaker_times("proactive", False) == 0)
    check("离屏+提醒→音箱2遍（必被听见）", server._absent_speaker_times("trigger", True) == 2)
    check("离屏+主动性→音箱1遍（轻提醒）", server._absent_speaker_times("proactive", True) == 1)

    # == _speak_alarm_async 次数契约（mock mouth，线程同步等待）==
    fake = _FakeMouth()
    old_mouth = server.mouth
    server.mouth = fake
    try:
        server._speak_alarm_async("零遍", times=0)
        server._speak_alarm_async("一遍", times=1)
        server._speak_alarm_async("两遍", times=2)
        deadline = time.time() + 10
        while len(fake.calls) < 3 and time.time() < deadline:
            time.sleep(0.05)
        check("times=0/1/2 严格按次数播报（共 3 声）",
              len(fake.calls) == 3 and "零遍" not in fake.calls, str(fake.calls))
        # mouth 为 None 时静默跳过（不抛异常、不新增播报）
        server.mouth = None
        server._speak_alarm_async("无声", times=2)
        time.sleep(0.2)
        check("mouth=None 静默跳过", len(fake.calls) == 3, str(fake.calls))
    finally:
        server.mouth = old_mouth

    print(f"\n全部通过：{_checks} 项")


if __name__ == "__main__":
    main()
