# -*- coding: utf-8 -*-
"""
reminders.py 独立自测脚本（不依赖其他未完成模块）。

覆盖：
  1. parse_time('十分钟后')      ≈ now + 10 分钟（误差容忍 70 秒）
  2. parse_time('明天早上九点')  = 明天 09:00
  3. parse_time('后天晚上七点半') = 后天 19:30
  4. parse_time('随便什么')      is None
  5. add('一分钟后测试喝水提醒')  成功；65 秒后 check_due() 能弹出该内容
  6. list_pending() 含未到期项
  7. 引导语 / 追问 / 顺延等边界

测试前后自动备份并还原 jarvis\\memory\\reminders.txt。
运行：python selftest_reminders.py
"""

import os
import sys
import time
from datetime import datetime, timedelta

import reminders

_REMINDERS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(reminders.__file__)), "memory", "reminders.txt"
)

_passed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if cond:
        _passed += 1
        print(f"  [通过] {name}")
    else:
        print(f"  [失败] {name}  {detail}")
        raise AssertionError(name)


def backup() -> bytes | None:
    """读出 reminders.txt 原始字节；文件不存在返回 None。"""
    if os.path.exists(_REMINDERS_FILE):
        with open(_REMINDERS_FILE, "rb") as f:
            return f.read()
    return None


def restore(data: bytes | None):
    """还原 reminders.txt；原本不存在则删除测试产物。"""
    if data is None:
        if os.path.exists(_REMINDERS_FILE):
            os.remove(_REMINDERS_FILE)
    else:
        os.makedirs(os.path.dirname(_REMINDERS_FILE), exist_ok=True)
        with open(_REMINDERS_FILE, "wb") as f:
            f.write(data)


def main():
    print("== reminders.py 自测 ==")
    original = backup()
    print(f"已备份 reminders.txt（{'原文件存在' if original is not None else '原文件不存在'}）")
    try:
        # ---- parse_time 纯解析 ----
        print("\n[1] parse_time 基础解析")
        now = datetime.now()
        dt = reminders.parse_time("十分钟后")
        check("十分钟后 ≈ now+10min",
              dt is not None and abs((dt - (now + timedelta(minutes=10))).total_seconds()) <= 70,
              f"got {dt}")

        dt = reminders.parse_time("明天早上九点")
        expect = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        check("明天早上九点 = 明天 09:00", dt == expect, f"got {dt}, expect {expect}")

        dt = reminders.parse_time("后天晚上七点半")
        expect = (datetime.now() + timedelta(days=2)).replace(hour=19, minute=30, second=0, microsecond=0)
        check("后天晚上七点半 = 后天 19:30", dt == expect, f"got {dt}, expect {expect}")

        check("随便什么 → None", reminders.parse_time("随便什么") is None)

        # 扩展覆盖：中文数字、中午、小时后、空串
        check("两小时后 解析成功",
              reminders.parse_time("两小时后") is not None)
        check("今天下午三点 = 今天 15:00（若已过点保持今天，由 add 顺延）",
              reminders.parse_time("今天下午三点").hour == 15)
        check("明天中午 = 明天 12:00",
              reminders.parse_time("明天中午").hour == 12)
        check("二十五分钟后 解析成功",
              reminders.parse_time("二十五分钟后") is not None)
        check("空串 → None", reminders.parse_time("") is None)

        # ---- add 边界 ----
        print("\n[2] add 边界行为")
        r = reminders.add("随便什么")
        check("解析不出时间 → 引导语", "没听清提醒时间" in r, r)
        check("引导语不存储", reminders.list_pending() == "先生，目前没有任何待提醒事项。")

        r = reminders.add("明天早上九点")
        check("内容为空 → 追问", "提醒您做什么事呢" in r, r)

        # ---- add + list_pending（未到期项）----
        print("\n[3] add + list_pending")
        r = reminders.add("明天早上九点测试晨会")
        check("添加未来提醒返回确认", "提醒您：测试晨会" in r, r)
        pending = reminders.list_pending()
        check("list_pending 含未到期项", "测试晨会" in pending, pending)
        check("check_due 此时为空", reminders.check_due() == [])

        # 顺延：构造一个今天已过点的时间
        past_hour = (datetime.now() - timedelta(hours=2)).hour
        r = reminders.add(f"今天{past_hour}点测试顺延")
        check("今天已过点自动顺延到明天并说明", "顺延到明天" in r, r)

        # ---- add 一分钟后 + check_due 弹出 ----
        print("\n[4] add('一分钟后测试喝水提醒') + 65 秒后 check_due")
        r = reminders.add("一分钟后测试喝水提醒")
        check("添加成功返回确认", "提醒您：测试喝水提醒" in r, r)
        pending = reminders.list_pending()
        check("list_pending 含一分钟后项", "测试喝水提醒" in pending, pending)

        print("  等待 65 秒让提醒到点……")
        time.sleep(65)

        due = reminders.check_due()
        check("check_due 弹出喝水提醒",
              any("测试喝水提醒" in t for t in due), f"due={due}")
        check("播报文本格式", any(t.startswith("先生，提醒时间到：") for t in due))
        check("弹出后不再重复",
              all("测试喝水提醒" not in t for t in reminders.check_due()))
        check("弹出后 list_pending 不含该项",
              "测试喝水提醒" not in reminders.list_pending())

        # 清理剩余测试项，验证删除路径
        remaining = reminders.check_due()  # 未来项不会被弹出
        check("未来项不会被 check_due 弹出",
              all("测试晨会" not in t for t in remaining))

        print(f"\n全部通过：{_passed} 项断言。")
        return 0
    finally:
        restore(original)
        print("已还原 reminders.txt。")


if __name__ == "__main__":
    sys.exit(main())
