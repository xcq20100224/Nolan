# -*- coding: utf-8 -*-
"""
test_win_context.py —— 窗口上下文记忆模块自测（纯逻辑，不碰屏幕）

存储隔离到临时目录（monkeypatch win_context._PATH），绝不动真实的
jarvis/data/win_context.json。覆盖：
  1. 登记/读取闭环：控件、成功、失败、任务都能存取
  2. 增量计数：重复登记 seen_count 累加、last_seen 刷新
  3. 容量淘汰：控件超 50 淘汰最久未见；成败各滚动保留 10 条
  4. brief 长度硬约束 + 关键信息齐全
  5. 原子写入崩溃安全：写一半的坏 JSON 自动备份 .corrupt 并重开
  6. prune 过期清理
  7. 异常输入不抛：None/怪类型/空 key 一律安全返回
  8. 整文件超 200KB 按 last_ts 淘汰最旧窗口
  9. make_key 正则化：数字占位化、大小写归一
 10. error_class 白名单：非七类一律归 unknown
"""

import glob
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win_context


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        win_context._PATH = os.path.join(td, "sub", "win_context.json")
        KEY = win_context.make_key("Notepad.exe", "无标题 - 记事本")
        assert KEY == "notepad.exe|无标题 - 记事本", KEY

        # 1. 登记/读取闭环（目录不存在自动创建）
        assert win_context.record_controls(KEY, [
            {"name": "文本编辑器", "type": "Edit"},
            {"name": "保存", "type": "Button"},
            "格式",  # 字符串形式：无名类型
        ])
        assert win_context.record_success(KEY, "left_click 保存",
                                          "保存", "UIA按名吸附")
        assert win_context.record_failure(KEY, "left_click 保存",
                                          "coord_drift", "建议按名定位")
        assert win_context.record_task(KEY, "在记事本中输入文字：你好")
        ctx = win_context.get_context(KEY)
        assert ctx is not None, "登记后应能取回"
        assert len(ctx["known_controls"]) == 3
        assert len(ctx["successes"]) == 1 and len(ctx["failures"]) == 1
        assert ctx["last_task"] == "在记事本中输入文字：你好"
        print("[PASS] 1. 登记/读取闭环 + 目录自动创建")

        # 2. 增量计数：重复登记 seen_count 累加、type 更新
        time.sleep(0.01)
        assert win_context.record_controls(KEY, [
            {"name": "文本编辑器", "type": "Edit"}])
        c = next(c for c in win_context.get_context(KEY)["known_controls"]
                 if c["name"] == "文本编辑器")
        assert c["seen_count"] == 2, "重复登记应累加：%s" % c
        assert win_context.get_context("不存在的key") is None
        print("[PASS] 2. 增量计数 seen_count 累加")

        # 3a. 控件容量淘汰：旧批 60 个 + 新批 50 个（制造时间差），共 113 个
        #     只留 50 —— 最久未见的旧批应整体出局，新批完整存活
        assert win_context.record_controls(KEY, [
            {"name": "旧控件%02d" % i, "type": "Button"} for i in range(60)])
        time.sleep(0.01)
        assert win_context.record_controls(KEY, [
            {"name": "新控件%02d" % i, "type": "Button"} for i in range(50)])
        ctrls = win_context.get_context(KEY)["known_controls"]
        assert len(ctrls) == 50, "应淘汰到 50：%d" % len(ctrls)
        names = {c["name"] for c in ctrls}
        assert "新控件49" in names and "旧控件00" not in names, \
            "应淘汰最久未见的：%s" % sorted(names)[:5]
        print("[PASS] 3a. 控件容量淘汰（50 上限，LRU）")

        # 3b. 成败滚动淘汰：各登记 13 条只留 10 条
        for i in range(13):
            assert win_context.record_success(KEY, "act%02d" % i,
                                              "t%d" % i, "坐标点击")
            assert win_context.record_failure(KEY, "act%02d" % i,
                                              "timeout", "教训%d" % i)
        ctx = win_context.get_context(KEY)
        assert len(ctx["successes"]) == 10 and len(ctx["failures"]) == 10
        assert ctx["successes"][-1]["action"] == "act12"
        assert ctx["successes"][0]["action"] == "act03", "最旧的应被挤掉"
        print("[PASS] 3b. 成败滚动淘汰（各 10 条 FIFO）")

        # 4. brief：关键信息齐全 + 长度硬约束
        text = win_context.brief(KEY)
        assert "已知控件" in text and "上次成功" in text and "教训" in text
        assert "坐标点击" in text and "曾超时" in text, text
        assert len(text) <= 300, "默认上限 300：%d" % len(text)
        short = win_context.brief(KEY, max_chars=60)
        assert 0 < len(short) <= 60, "硬约束 60：%d" % len(short)
        assert win_context.brief("不存在的key") == "", "无记录应空串"
        print("[PASS] 4. brief 信息齐全 + 长度硬约束（%d/%d 字符）"
              % (len(text), len(short)))

        # 5. 崩溃安全：模拟写一半的坏 JSON —— 自动备份 .corrupt 并重开
        with open(win_context._PATH, "w", encoding="utf-8") as f:
            f.write('{"%s": {"known_controls": [{"name": "x' % KEY)  # 写一半
        assert win_context.get_context(KEY) is None, "坏文件应重开空库"
        backups = glob.glob(win_context._PATH + ".corrupt.*")
        assert backups, "损坏文件应留 .corrupt 备份"
        # 重开后正常登记不受影响；遗留 .tmp 垃圾也不影响读取
        assert win_context.record_success(KEY, "act", "t", "UIA按名吸附")
        with open(win_context._PATH + ".tmp", "w", encoding="utf-8") as f:
            f.write("垃圾残留")
        assert win_context.get_context(KEY) is not None
        print("[PASS] 5. 原子写入崩溃安全（坏 JSON 备份重开 + .tmp 残留免疫）")

        # 6. prune 过期清理：篡改 last_ts 为 40 天前
        old_key = win_context.make_key("old.exe", "旧窗口")
        assert win_context.record_success(old_key, "act", "t", "s")
        path = win_context._PATH
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data[old_key]["last_ts"] = time.time() - 40 * 86400
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        assert win_context.prune(max_age_days=30) == 1
        assert win_context.get_context(old_key) is None
        assert win_context.get_context(KEY) is not None, "活跃窗口不应被清"
        print("[PASS] 6. prune 过期清理（40 天旧记录被清，活跃保留）")

        # 7. 异常输入不抛：None/怪类型/空 key
        assert win_context.record_controls(None, None) is False
        assert win_context.record_controls("", [{"name": "x"}]) is False
        assert win_context.record_controls(KEY, "不是列表") is False
        assert win_context.record_controls(KEY, [None, 123, {"name": ""}]) is True
        assert win_context.record_success(None, None, None, None) is False
        assert win_context.record_failure(KEY, None, None, None) is False
        assert win_context.get_context(None) is None
        assert win_context.brief(None) == ""
        assert win_context.prune(max_age_days=-1) == 0
        print("[PASS] 7. 异常输入不抛（全安全返回）")

        # 8. 整文件超 200KB：按 last_ts 淘汰最旧窗口
        #    40 窗口 × 50 个长名控件 ≈ 260KB，必然触发预算淘汰
        keys = []
        for i in range(40):
            k = win_context.make_key("app%02d.exe" % i, "窗口%d" % i)
            keys.append(k)
            assert win_context.record_controls(k, [
                {"name": ("控件%02d-" % j) + "x" * 60, "type": "T" * 30}
                for j in range(50)])
        size = os.path.getsize(win_context._PATH)
        assert size <= win_context._MAX_FILE_BYTES, \
            "文件应压到 200KB 内：%d" % size
        with open(win_context._PATH, "r", encoding="utf-8") as f:
            survivors = json.load(f)
        assert keys[-1] in survivors, "最新窗口必须存活"
        assert keys[0] not in survivors, "最旧窗口应被淘汰"
        print("[PASS] 8. 整文件 200KB 预算淘汰最旧窗口（幸存 %d 个，%dKB）"
              % (len(survivors), size // 1024))

        # 9. make_key 正则化：数字占位化、大小写归一、空白压缩
        k1 = win_context.make_key("NOTEPAD.EXE", "文档 第 12 页 - 记事本")
        k2 = win_context.make_key("notepad.exe", "文档 第 34 页 - 记事本")
        assert k1 == k2, "数字与大小写差异应归一：%s vs %s" % (k1, k2)
        assert win_context.make_key("", "") == ""
        assert win_context.make_key(None, None) == ""
        print("[PASS] 9. make_key 正则化（数字占位/大小写归一）")

        # 10. error_class 白名单：七类直存，其余归 unknown
        for cls in ("focus_lost", "app_not_responding", "target_missing",
                    "coord_drift", "text_mismatch", "timeout", "unknown"):
            assert win_context.record_failure(KEY, "act", cls, "L")
        assert win_context.record_failure(KEY, "act", "瞎编的类别", "L")
        fails = win_context.get_context(KEY)["failures"]
        classes = {f["error_class"] for f in fails}
        assert classes <= win_context._VALID_ERROR_CLASSES, classes
        assert fails[-1]["error_class"] == "unknown", "非法类别应归 unknown"
        print("[PASS] 10. error_class 白名单（非法归 unknown）")

    print("\n全部通过：win_context 窗口上下文记忆 10 组用例 OK")


if __name__ == "__main__":
    main()
