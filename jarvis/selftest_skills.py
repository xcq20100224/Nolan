# -*- coding: utf-8 -*-
"""
selftest_skills.py —— 技能固化模块自测（纯逻辑，不碰屏幕）

覆盖：
  1. record/find 闭环：固化后能按原文找回
  2. 相似任务模糊命中（相似度 ≥ 0.6）
  3. 不相似任务不命中（宁缺毋滥）
  4. 同任务重复固化只留最新一条
  5. 二元组相似度基本性质
测试使用临时文件，绝不动真实的 skills.jsonl。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skills

_STEPS = [
    {"action": "left_click", "text": "我喜欢的音乐", "keys": ""},
    {"action": "left_click", "text": "play", "keys": ""},
    {"action": "wait"},  # 不可固化动作，应被过滤
]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        skills._PATH = os.path.join(td, "skills.jsonl")

        # 1. record/find 闭环
        assert skills.record("在网易云音乐中播放我喜欢的第一首歌", _STEPS)
        hit = skills.find("在网易云音乐中播放我喜欢的第一首歌")
        assert hit, "固化后应能找回"
        task, steps = hit
        assert len(steps) == 2, "wait 不应被固化：%s" % steps
        print("[PASS] 1. record/find 闭环 + wait 过滤")

        # 2. 相似任务模糊命中
        hit2 = skills.find("在网易云音乐里播放我喜欢的第一首歌")
        assert hit2, "相似任务应模糊命中"
        print("[PASS] 2. 相似任务模糊命中（相似度达标）")

        # 3. 不相似不命中
        assert skills.find("在记事本中输入文字") is None, "不相似任务不应命中"
        print("[PASS] 3. 不相似任务不命中（宁缺毋滥）")

        # 4. 重复固化去重
        skills.record("在网易云音乐中播放我喜欢的第一首歌", _STEPS)
        assert len(skills._load()) == 1, "同任务应只留最新一条"
        print("[PASS] 4. 同任务重复固化去重")

        # 5. 相似度性质
        assert skills._similarity("abcde", "abcde") == 1.0
        assert skills._similarity("abcde", "vwxyz") == 0.0
        assert 0 < skills._similarity("播放我喜欢的歌", "播放我喜欢的音乐") < 1
        print("[PASS] 5. 相似度基本性质")

    print("== skills 自测全部通过 ==")


if __name__ == "__main__":
    main()
