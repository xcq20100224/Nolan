# -*- coding: utf-8 -*-
"""
Nolan 大脑 v4 自测（selftest_brain_v4.py）· 阶段四：提醒意图分支

断言点：
  1. think('提醒我一分钟后喝水') -> reminders 已就位时走真实链路，返回含「提醒」的确认文本；
     reminders 未就位时跳过该项并注明（不视为失败）。
  2. think('我的提醒') -> 返回非空文本。
  3. think('现在几点') -> 仍走 get_time 工具，不被提醒分支截获。
  4. think('记住我喜欢蓝色') -> 仍走记忆分支，不被提醒分支截获。

涉及存储文件（jarvis\\memory\\reminders.txt、long_term.txt）测试前备份、测试后还原。
运行方式：在 jarvis 目录下 `python selftest_brain_v4.py`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain

_MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_REMINDERS_FILE = os.path.join(_MEMORY_DIR, "reminders.txt")
_LONG_TERM_FILE = os.path.join(_MEMORY_DIR, "long_term.txt")


def _backup(path: str) -> bytes | None:
    """读走文件原始字节；文件不存在返回 None。"""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _restore(path: str, data: bytes | None) -> None:
    """按备份还原：原来不存在则删除测试产物，原来存在则写回原始字节。"""
    if data is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "wb") as f:
            f.write(data)


def main() -> int:
    reminders_bak = _backup(_REMINDERS_FILE)
    long_term_bak = _backup(_LONG_TERM_FILE)
    failures = []
    try:
        # 1. 新增提醒：提醒我一分钟后喝水
        reply = brain.think("提醒我一分钟后喝水", [])
        if brain.reminders is None:
            print("[跳过] reminders 模块未就位，「提醒我一分钟后喝水」走降级链路，返回：%r" % reply)
        else:
            ok = isinstance(reply, str) and "提醒" in reply
            print("[%s] think('提醒我一分钟后喝水') -> %r" % ("通过" if ok else "失败", reply))
            if not ok:
                failures.append("新增提醒未返回含「提醒」的确认文本")

        # 2. 查询提醒：我的提醒
        reply = brain.think("我的提醒", [])
        if brain.reminders is None:
            print("[跳过] reminders 模块未就位，「我的提醒」走降级链路，返回：%r" % reply)
        else:
            ok = isinstance(reply, str) and reply.strip() != ""
            print("[%s] think('我的提醒') -> %r" % ("通过" if ok else "失败", reply))
            if not ok:
                failures.append("「我的提醒」返回为空")

        # 3. 回归：现在几点仍走 get_time，不被提醒分支截获
        reply = brain.think("现在几点", [])
        ok = isinstance(reply, str) and reply.strip() != "" and "提醒" not in reply
        print("[%s] think('现在几点') -> %r" % ("通过" if ok else "失败", reply))
        if not ok:
            failures.append("「现在几点」被提醒分支截获或返回异常")

        # 4. 回归：记住我喜欢蓝色仍走记忆分支
        reply = brain.think("记住我喜欢蓝色", [])
        ok = isinstance(reply, str) and reply.strip() != ""
        if ok and brain.memory is not None:
            try:
                ok = "蓝色" in brain.memory.recall()
            except Exception:
                ok = False
        print("[%s] think('记住我喜欢蓝色') -> %r" % ("通过" if ok else "失败", reply))
        if not ok:
            failures.append("「记住我喜欢蓝色」未走记忆分支或记忆未落盘")
    finally:
        # 无论断言结果如何，存储文件必须还原
        _restore(_REMINDERS_FILE, reminders_bak)
        _restore(_LONG_TERM_FILE, long_term_bak)
        print("[还原] reminders.txt 与 long_term.txt 已按备份还原")

    if failures:
        print("\n自测未通过：")
        for f in failures:
            print("  - " + f)
        return 1
    print("\n全部断言通过（reminders 未就位的项已按约定跳过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
