# -*- coding: utf-8 -*-
"""
test_skills_growth.py —— H4 技能库「自己长大」纯单测（不碰屏幕、不动真实库）

覆盖：
  1. 旧格式兼容：无统计字段的旧模板/旧字面技能照常 load、find、record_outcome
  2. 自动参数化：文件名 / 引号内容 / 数字 -> {槽N}，锚定正则命中并回填
  3. 数字槽位边界守卫：值 "5" 不得污染文本 "15"
  4. 相似收敛：同族 record 不新增（模板并吞 + 字面 ≥0.9 合并）
  5. 使用统计：find 命中累计 uses；record_outcome 累计 ok/fail；stats() 概况
  6. 保守淘汰：stats 只标记；prune(dry_run) 预演；prune() 显式删除
  7. 契约零回退：record/find 签名、阈值 0.6、相似命中 + 不误命中（第 57 题同型）
运行：python jarvis/test_skills_growth.py
"""

import inspect
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skills


def _steps(text=""):
    return [{"action": "left_click", "text": text, "keys": ""},
            {"action": "wait"}]  # wait 应被过滤


class SkillsGrowthTest(unittest.TestCase):
    def setUp(self):
        self._old_path = skills._PATH
        self._td = tempfile.TemporaryDirectory()
        skills._PATH = os.path.join(self._td.name, "skills.jsonl")

    def tearDown(self):
        skills._PATH = self._old_path
        self._td.cleanup()

    def _write_raw(self, rows):
        """直接手写 JSONL（构造旧格式/回拨时间戳等场景）。"""
        os.makedirs(os.path.dirname(skills._PATH), exist_ok=True)
        with open(skills._PATH, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- 1. 旧格式兼容 ----------
    def test_old_format_compat(self):
        old_literal = {"task": "在某某软件中播放我喜欢的第一首歌",
                       "steps": [{"action": "left_click",
                                  "text": "我喜欢的音乐", "keys": ""}],
                       "ts": 1700000000}
        old_tmpl = {"task": "在记事本中输入文字：{内容}",
                    "pattern": r"^(?:在)?记事本(?:中|里)?(?:输入|写上|写入)(?:文字)?[:：]?(?P<内容>.+)$",
                    "steps": [{"action": "type", "text": "{内容}", "keys": ""}],
                    "ts": 1700000000}
        self._write_raw([old_literal, old_tmpl])
        loaded = skills._load()
        self.assertEqual(len(loaded), 2, "旧格式两行都应读入")
        # 旧模板仍按正则命中并回填
        hit = skills.find("在记事本中输入文字：你好世界")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1][0]["text"], "你好世界")
        # 旧字面技能无统计字段，find/record_outcome/stats 不 KeyError
        hit2 = skills.find("在某某软件中播放我喜欢的第一首歌")
        self.assertIsNotNone(hit2)
        self.assertTrue(skills.record_outcome("在某某软件中播放我喜欢的第一首歌", True))
        st = skills.stats()
        self.assertEqual(st["total"], 2)
        lit = next(i for i in st["skills"] if i["kind"] == "literal")
        self.assertEqual(lit["uses"], 1)
        self.assertEqual(lit["success_rate"], 1.0)
        print("[PASS] 1. 旧格式兼容（无统计字段 load/find/outcome/stats 正常）")

    # ---------- 2. 自动参数化 ----------
    def test_auto_param_filename(self):
        self.assertTrue(skills.record("打开文件 report.txt 并保存",
                                      _steps("report.txt")))
        lib = skills._load()
        self.assertEqual(len(lib), 1)
        self.assertIn("{槽", lib[0]["task"])
        self.assertTrue(lib[0].get("pattern"), "应生成锚定正则")
        hit = skills.find("打开文件 data.csv 并保存")
        self.assertIsNotNone(hit, "同族新实例应命中模板")
        self.assertEqual(hit[1][0]["text"], "data.csv", "参数应回填到动作文本")
        # 字面骨架不同则不命中（不误命中）
        self.assertIsNone(skills.find("删除所有系统文件"))
        print("[PASS] 2a. 文件名槽位自动参数化 + 回填 + 不误命中")

    def test_auto_param_quoted(self):
        skills.record("把「会议纪要」发给张三", _steps("会议纪要"))
        hit = skills.find("把「周报」发给张三")
        self.assertIsNotNone(hit, "引号内容应参数化命中")
        self.assertEqual(hit[1][0]["text"], "周报")
        self.assertIsNone(skills.find("把「周报」发给李四"),
                          "字面锚（张三）不符不应命中")
        print("[PASS] 2b. 引号槽位自动参数化（引号本身仍为字面锚）")

    def test_auto_param_number(self):
        skills.record("等待 5 秒后点击确定", _steps("5"))
        hit = skills.find("等待 12 秒后点击确定")
        self.assertIsNotNone(hit, "数字槽位应参数化命中")
        self.assertEqual(hit[1][0]["text"], "12")
        print("[PASS] 2c. 数字槽位自动参数化")

    def test_number_boundary_guard(self):
        # 数字值 "5" 不得把动作文本 "15" 污染成 "1{槽1}"
        skills.record("等待 5 秒后点击确定",
                      [{"action": "type", "text": "15", "keys": ""}])
        lib = skills._load()
        self.assertEqual(lib[0]["steps"][0]["text"], "15",
                         "数字边界守卫失效：15 被污染")
        print("[PASS] 3. 数字槽位边界守卫（15 不被 5 污染）")

    # ---------- 3. 相似收敛 ----------
    def test_merge_same_family(self):
        skills.record("打开文件 report.txt 并保存", _steps("report.txt"))
        skills.record("打开文件 data.csv 并保存", _steps("data.csv"))
        skills.record("打开文件 summary.docx 并保存", _steps("summary.docx"))
        lib = skills._load()
        self.assertEqual(len(lib), 1,
                         "同族实例应收敛为一条模板而不是无限新增")
        print("[PASS] 4a. 同族模板收敛（3 次 record 只留 1 条）")

    def test_merge_absorbs_old_literal(self):
        # 旧格式字面实例应被新泛化模板并吞，统计累计
        self._write_raw([{"task": "打开文件 old.txt 并保存",
                          "steps": _steps("old.txt")[:1],
                          "ts": 1700000000, "uses": 3, "ok": 2, "fail": 1}])
        skills.record("打开文件 new.txt 并保存", _steps("new.txt"))
        lib = skills._load()
        self.assertEqual(len(lib), 1, "存量字面实例应并入新模板")
        self.assertTrue(lib[0].get("pattern"))
        self.assertEqual(lib[0]["uses"], 3, "并吞应继承 uses")
        self.assertEqual((lib[0]["ok"], lib[0]["fail"]), (2, 1))
        print("[PASS] 4b. 旧字面实例并入模板且统计继承")

    def test_merge_literal_similar(self):
        t1, t2 = "点击确定按钮完成提交", "点击确定按钮完成提交。"
        self.assertGreaterEqual(skills._similarity(t1, t2), 0.9,
                                "测试前提：两者相似度应 ≥ 0.9")
        skills.record(t1, _steps("确定"))
        skills.record(t2, _steps("确定"))
        lib = skills._load()
        self.assertEqual(len(lib), 1, "≥0.9 的字面相似应合并不新增")
        print("[PASS] 4c. 字面相似 ≥0.9 收敛合并")

    def test_no_merge_below_threshold(self):
        skills.record("在某某软件中播放我喜欢的第一首歌", _steps("我喜欢的音乐"))
        skills.record("在记事本中涂鸦一幅山水画试试", _steps("画布"))
        self.assertEqual(len(skills._load()), 2, "不相似任务不应被误合并")
        print("[PASS] 4d. 低相似度不误合并")

    # ---------- 4. 使用统计 ----------
    def test_find_bumps_uses(self):
        skills.record("在某某软件中播放我喜欢的第一首歌", _steps("我喜欢的音乐"))
        skills.find("在某某软件中播放我喜欢的第一首歌")
        skills.find("在某某软件中播放我喜欢的第一首歌")
        lib = skills._load()
        self.assertEqual(lib[0]["uses"], 2, "find 命中应累计 uses")
        self.assertGreater(lib[0]["last_used"], 0)
        print("[PASS] 5a. find 命中累计使用次数")

    def test_record_outcome_and_stats(self):
        skills.record("在某某软件中播放我喜欢的第一首歌", _steps("我喜欢的音乐"))
        self.assertTrue(skills.record_outcome("在某某软件中播放我喜欢的第一首歌", True))
        self.assertTrue(skills.record_outcome("在某某软件里播放我喜欢的第一首歌", True))
        self.assertTrue(skills.record_outcome("在某某软件中播放我喜欢的第一首歌", False))
        self.assertFalse(skills.record_outcome("完全不存在的任务甲天下", True),
                         "找不到技能不应造数据")
        st = skills.stats()
        self.assertEqual(st["total"], 1)
        item = st["skills"][0]
        self.assertAlmostEqual(item["success_rate"], 2 / 3)
        for k in ("total", "templates", "literals", "total_uses", "stale", "skills"):
            self.assertIn(k, st)
        print("[PASS] 5b. record_outcome 反馈 + stats() 概况")

    # ---------- 5. 保守淘汰 ----------
    def test_prune(self):
        old_ts = int(time.time()) - 40 * 86400
        self._write_raw([
            {"task": "陈年冷宫技能甲乙丙", "steps": _steps("x")[:1],
             "ts": old_ts, "uses": 0},
            {"task": "在某某软件中播放我喜欢的第一首歌",
             "steps": _steps("我喜欢的音乐")[:1], "ts": int(time.time()),
             "uses": 5},
        ])
        st = skills.stats()
        self.assertEqual(st["stale"], 1, "stats 应标记但不删除")
        self.assertEqual(len(skills._load()), 2)
        preview = skills.prune(dry_run=True)
        self.assertEqual(preview, ["陈年冷宫技能甲乙丙"])
        self.assertEqual(len(skills._load()), 2, "dry_run 不应动库")
        removed = skills.prune()
        self.assertEqual(removed, ["陈年冷宫技能甲乙丙"])
        lib = skills._load()
        self.assertEqual(len(lib), 1)
        self.assertEqual(lib[0]["task"], "在某某软件中播放我喜欢的第一首歌",
                         "活跃技能必须存活")
        print("[PASS] 6. 保守淘汰（标记 -> 预演 -> 显式删除，活跃存活）")

    # ---------- 6. 契约零回退（第 57 题同型） ----------
    def test_contract(self):
        self.assertEqual(skills._MATCH_THRESHOLD, 0.6, "find 阈值契约不得变")
        sig = inspect.signature(skills.record)
        self.assertEqual(list(sig.parameters), ["task", "steps"])
        sig = inspect.signature(skills.find)
        self.assertEqual(list(sig.parameters), ["task"])
        # 第 57 题同型：record 无槽位任务 -> 一字之差模糊命中 + 不相似不误命中
        ok1 = skills.record("在某某软件中播放我喜欢的第一首歌",
                            [{"action": "left_click", "text": "我喜欢的音乐",
                              "keys": ""}, {"action": "wait"}])
        hit = skills.find("在某某软件里播放我喜欢的第一首歌")
        ok2 = hit is not None and len(hit[1]) == 1
        ok3 = skills.find("在记事本中输入一段文字") is None
        self.assertTrue(ok1 and ok2 and ok3,
                        "第 57 题同型契约被破坏: record=%s hit=%s 误命中=%s"
                        % (ok1, bool(hit), not ok3))
        # 原子写入：不应残留 .tmp
        self.assertFalse(os.path.exists(skills._PATH + ".tmp"),
                         "原子写入不应残留临时文件")
        print("[PASS] 7. 契约零回退（签名/阈值 0.6/相似命中+不误命中/无 tmp 残留）")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    print("== skills 生长（H4）单测全部通过 ==")
