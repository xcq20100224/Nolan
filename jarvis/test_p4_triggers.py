# -*- coding: utf-8 -*-
"""P4 条件触发 · brain 意图路由单测（隔离存储，不碰真实提醒库、不碰 LLM）。

覆盖：
    1. 周期型路由：「每隔30分钟提醒我喝水」→ triggers（不被 reminders 劫持）
    2. 单次条件型：「如果明天下雨，就提醒我带伞」→ 落库 + 确认语
    3. 循环条件型：「每当有重大人工智能新闻，就告诉我」→ 落库 + 冷却说明
    4. 防误劫：「如果明天下雨怎么办」（无动作词）→ 不落库，放行
    5. 触发列表查询：「我的触发」→ 列出口语化清单
    6. 高考保护：「提醒我 2 分钟后吃药」仍走 reminders 一次性提醒
"""
import os
import tempfile

import reminders
import triggers

_tmp = tempfile.mkdtemp()
triggers._STORE = os.path.join(_tmp, "triggers.json")
reminders._REMINDERS_FILE = os.path.join(_tmp, "reminders.txt")

import brain


def test_周期型路由():
    reply = brain.think("每隔30分钟提醒我喝水", [])
    assert "每隔" in reply and "触发" in reply, reply
    entries = [e for e in triggers._read() if e["kind"] == "interval"]
    assert entries and abs(entries[0]["interval_min"] - 30) < 0.01, entries
    # 关键：没有被 reminders 劫持成一次性提醒
    assert "喝水" not in reminders.list_pending(), reminders.list_pending()
    print("✅ 1/6 周期型正确路由到 triggers，未被 reminders 劫持")


def test_单次条件型():
    reply = brain.think("如果明天下雨，就提醒我带伞", [])
    assert "只提醒一次" in reply, reply
    cond = [e for e in triggers._read() if e["kind"] == "condition" and not e["recurring"]]
    assert cond and "下雨" in cond[0]["condition"], cond
    print("✅ 2/6 单次条件型落库 + 确认语")


def test_循环条件型():
    reply = brain.think("每当有重大人工智能新闻，就告诉我", [])
    assert "冷却" in reply, reply
    rec = [e for e in triggers._read() if e["kind"] == "condition" and e["recurring"]]
    assert rec, triggers._read()
    print("✅ 3/6 循环条件型落库 + 冷却说明")


def test_提问不误劫():
    before = len(triggers._read())
    reply = brain.think("如果明天下雨怎么办", [])
    # 无动作词 → 不落库（放行给后续层，回复内容不强制——LLM 可能不在线）
    assert len(triggers._read()) == before, triggers._read()
    assert isinstance(reply, str) and reply, reply
    print("✅ 4/6 纯提问不被误劫（未落库，正常放行）")


def test_触发列表():
    reply = brain.think("我的触发", [])
    assert "条件触发任务" in reply, reply
    print("✅ 5/6 触发列表查询")


def test_高考保护_一次性提醒不受影响():
    reply = brain.think("提醒我 2 分钟后吃药", [])
    assert "触发" not in reply, reply
    assert "吃药" in reminders.list_pending(), reminders.list_pending()
    # 且没有被 triggers 抢去
    assert not any("吃药" in e.get("action", "") for e in triggers._read())
    print("✅ 6/6 一次性提醒仍走 reminders（高考第47题路径不变）")


if __name__ == "__main__":
    test_周期型路由()
    test_单次条件型()
    test_循环条件型()
    test_提问不误劫()
    test_触发列表()
    test_高考保护_一次性提醒不受影响()
    print("\n🎉 P4 路由单测 6/6 全过")
