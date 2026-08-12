# -*- coding: utf-8 -*-
"""
journeys.py 单元测试（unittest，零外部依赖）。

隔离方式：每个用例把 journeys._STORE_FILE 替换到临时目录下，
测试全程不触碰真实的 jarvis\\memory\\journeys.jsonl。

运行：cd jarvis && python test_journeys.py
"""

import json
import os
import shutil
import tempfile
import time
import unittest

import journeys


class JourneysTestBase(unittest.TestCase):
    """公共脚手架：临时目录接管存储路径，用例间互不影响。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="journeys_test_")
        self._store = os.path.join(self._tmpdir, "memory", "journeys.jsonl")
        self._orig_store = journeys._STORE_FILE
        journeys._STORE_FILE = self._store

    def tearDown(self):
        journeys._STORE_FILE = self._orig_store
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---- 辅助 ----

    def _read_lines(self):
        """读出全部合法经历行（跳过坏行），供断言。"""
        if not os.path.exists(self._store):
            return []
        out = []
        with open(self._store, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def _write_raw(self, events):
        """直接向存储文件写入若干条原始事件 dict（可指定历史 ts）。"""
        os.makedirs(os.path.dirname(self._store), exist_ok=True)
        with open(self._store, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


class TestRecord(JourneysTestBase):
    """record + 读取回环。"""

    def test_record_roundtrip(self):
        journeys.record("ppt_made", "做了PPT《开学季》，共8页", "开学季_xxx.pptx")
        events = self._read_lines()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["kind"], "ppt_made")
        self.assertEqual(e["summary"], "做了PPT《开学季》，共8页")
        self.assertEqual(e["artifact"], "开学季_xxx.pptx")
        self.assertIsInstance(e["ts"], float)
        self.assertLessEqual(abs(e["ts"] - time.time()), 5)

    def test_record_auto_creates_dir(self):
        # 目录尚未创建时 record 应自动创建
        self.assertFalse(os.path.exists(self._store))
        journeys.record("file_written", "写了文件「a.txt」", "a.txt")
        self.assertTrue(os.path.exists(self._store))

    def test_record_appends_not_overwrites(self):
        journeys.record("ppt_made", "做了PPT《一》", "a.pptx")
        journeys.record("ppt_made", "做了PPT《二》", "b.pptx")
        events = self._read_lines()
        self.assertEqual(len(events), 2)

    def test_record_empty_summary_skipped(self):
        journeys.record("ppt_made", "", "a.pptx")
        self.assertEqual(self._read_lines(), [])

    def test_record_never_raises(self):
        journeys._STORE_FILE = os.path.join(self._tmpdir, "\0非法路径", "x.jsonl")
        journeys.record("ppt_made", "做了PPT《崩》", "a.pptx")  # 不应抛异常


class TestRecordForToolMakePpt(JourneysTestBase):

    _OK = ("好的先生，PPT 已经做好并放进文件柜了：《开学季》，"
           "文件名 开学季_20260104.pptx，共 8 页，"
           "每一页的备注栏里都写好了演讲稿，您照着讲就行。")

    def test_make_ppt_success(self):
        journeys.record_for_tool("make_ppt", {"topic": "开学季"}, self._OK)
        events = self._read_lines()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["kind"], "ppt_made")
        self.assertIn("《开学季》", e["summary"])
        self.assertIn("共8页", e["summary"])
        self.assertEqual(e["artifact"], "开学季_20260104.pptx")

    def test_make_ppt_title_fallback_to_topic(self):
        # 话术里提取不到《标题》时回退 args.topic
        result = "好的先生，PPT 已经做好并放进文件柜了，共 5 页。"
        journeys.record_for_tool("make_ppt", {"topic": "年度总结"}, result)
        e = self._read_lines()[0]
        self.assertIn("《年度总结》", e["summary"])
        self.assertIn("共5页", e["summary"])
        self.assertEqual(e["artifact"], "")

    def test_make_ppt_failure_skipped(self):
        journeys.record_for_tool("make_ppt", {"topic": "开学季"},
                                 "抱歉先生，PPT 没做成：模型超时。")
        self.assertEqual(self._read_lines(), [])

    def test_make_ppt_non_str_result_skipped(self):
        journeys.record_for_tool("make_ppt", {"topic": "开学季"},
                                 {"ok": True, "file_name": "a.pptx"})
        self.assertEqual(self._read_lines(), [])
        journeys.record_for_tool("make_ppt", {"topic": "开学季"}, None)
        self.assertEqual(self._read_lines(), [])


class TestRecordForToolEditPpt(JourneysTestBase):

    def test_edit_ppt_success(self):
        journeys.record_for_tool(
            "edit_ppt",
            {"file_name": "开学季_20260104.pptx", "instruction": "第3页换成校园照片"},
            "好的先生，PPT 已经改好了。")
        e = self._read_lines()[0]
        self.assertEqual(e["kind"], "ppt_edited")
        self.assertIn("第3页换成校园照片", e["summary"])
        self.assertEqual(e["artifact"], "开学季_20260104.pptx")

    def test_edit_ppt_instruction_truncated_to_30(self):
        long_instr = "把每一页的标题全部改成加粗黑体并且配色换成暖色调三十字以上肯定超过了"
        journeys.record_for_tool(
            "edit_ppt",
            {"file_name": "a.pptx", "instruction": long_instr},
            "好的先生，改好了。")
        e = self._read_lines()[0]
        # 摘要里的指令部分不超过 30 字
        self.assertIn(long_instr[:30], e["summary"])
        self.assertNotIn(long_instr[:31], e["summary"])

    def test_edit_ppt_failure_skipped(self):
        journeys.record_for_tool(
            "edit_ppt",
            {"file_name": "a.pptx", "instruction": "改标题"},
            "抱歉先生，修改 PPT 时出了点意外。")
        self.assertEqual(self._read_lines(), [])


class TestRecordForToolWriteFile(JourneysTestBase):

    def test_write_file_success(self):
        journeys.record_for_tool(
            "write_file",
            {"name": "日记.txt", "content": "今天很开心"},
            "好的先生，内容已经写进文件「日记.txt」了。")
        e = self._read_lines()[0]
        self.assertEqual(e["kind"], "file_written")
        self.assertIn("日记.txt", e["summary"])
        self.assertEqual(e["artifact"], "日记.txt")

    def test_write_file_name_fallback_from_result(self):
        # args 里没有 name 时从话术里提取
        journeys.record_for_tool(
            "write_file", {},
            "好的先生，内容已经写进文件「周报.md」了。")
        e = self._read_lines()[0]
        self.assertEqual(e["artifact"], "周报.md")

    def test_write_file_failure_skipped(self):
        journeys.record_for_tool(
            "write_file",
            {"name": "日记.txt", "content": "x"},
            "抱歉先生，写文件时出了问题：磁盘已满")
        self.assertEqual(self._read_lines(), [])


class TestRecordForToolOther(JourneysTestBase):

    def test_other_tools_not_recorded(self):
        journeys.record_for_tool("web_search", {"query": "新闻"},
                                 "好的先生，已经在浏览器里搜好了。")
        journeys.record_for_tool("open_app", {"app": "记事本"},
                                 "好的先生，记事本已经打开了。")
        self.assertEqual(self._read_lines(), [])

    def test_never_raises_on_garbage_args(self):
        journeys.record_for_tool("make_ppt", None, "好的，做好了")  # args 非 dict
        journeys.record_for_tool(None, {}, "好的，做好了")          # tool 为 None


class TestBriefForPrompt(JourneysTestBase):

    def _seed(self, n, start_days_ago=0, step_hours=1):
        """造 n 条经历，最新一条距今 start_days_ago 天，每条间隔 step_hours 小时。"""
        now = time.time()
        events = []
        for i in range(n):
            ts = now - start_days_ago * 86400 - (n - 1 - i) * step_hours * 3600
            events.append({"ts": ts, "kind": "ppt_made",
                           "summary": f"做了PPT《第{i+1}号》",
                           "artifact": f"第{i+1}号.pptx"})
        self._write_raw(events)
        return events

    def test_render_format(self):
        journeys.record("ppt_made", "做了PPT《开学季》，共8页", "开学季_xxx.pptx")
        brief = journeys.brief_for_prompt()
        self.assertTrue(brief.startswith(
            "以下是你和主人一起做过的事（共同经历，可在合适时机自然提起）："))
        self.assertIn("做了PPT《开学季》，共8页", brief)
        self.assertIn("（文件柜：开学季_xxx.pptx）", brief)
        dt = time.localtime()
        self.assertIn(f"- {dt.tm_mon}月{dt.tm_mday}日 ", brief)

    def test_newest_first_and_max_items(self):
        events = self._seed(12)
        brief = journeys.brief_for_prompt(max_items=8)
        lines = [l for l in brief.splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 8)
        # 新→旧：最新的第12号在最前
        self.assertIn("第12号", lines[0])
        self.assertIn("第5号", lines[-1])
        self.assertNotIn("第4号", brief)

    def test_days_window_filters_old(self):
        now = time.time()
        self._write_raw([
            {"ts": now - 40 * 86400, "kind": "ppt_made",
             "summary": "做了PPT《远古》", "artifact": "远古.pptx"},
            {"ts": now - 10 * 86400, "kind": "ppt_made",
             "summary": "做了PPT《近期》", "artifact": "近期.pptx"},
        ])
        brief = journeys.brief_for_prompt(days=30)
        self.assertIn("《近期》", brief)
        self.assertNotIn("《远古》", brief)

    def test_cross_year_label_has_year(self):
        now = time.time()
        # 构造一条去年同日期的经历（1 年前减 1 天，仍在 370 天窗口内）
        self._write_raw([
            {"ts": now - 364 * 86400, "kind": "ppt_made",
             "summary": "做了PPT《去年》", "artifact": "去年.pptx"},
        ])
        brief = journeys.brief_for_prompt(days=370)
        dt = time.localtime(now - 364 * 86400)
        if dt.tm_year != time.localtime(now).tm_year:
            self.assertIn(f"{dt.tm_year}年{dt.tm_mon}月{dt.tm_mday}日", brief)
        else:
            self.assertIn(f"{dt.tm_mon}月{dt.tm_mday}日", brief)

    def test_corrupt_lines_skipped(self):
        os.makedirs(os.path.dirname(self._store), exist_ok=True)
        good = json.dumps({"ts": time.time(), "kind": "ppt_made",
                           "summary": "做了PPT《幸存》", "artifact": "a.pptx"},
                          ensure_ascii=False)
        with open(self._store, "w", encoding="utf-8") as f:
            f.write(good + "\n")
            f.write("{这是半截写入的坏行\n")
            f.write('"不是对象的 JSON"\n')
            f.write('{"no_ts": true}\n')
        brief = journeys.brief_for_prompt()
        self.assertIn("《幸存》", brief)
        self.assertEqual(len([l for l in brief.splitlines()
                              if l.startswith("- ")]), 1)

    def test_empty_and_missing_file_returns_empty(self):
        # 文件不存在
        self.assertEqual(journeys.brief_for_prompt(), "")
        # 文件存在但为空
        os.makedirs(os.path.dirname(self._store), exist_ok=True)
        open(self._store, "w", encoding="utf-8").close()
        self.assertEqual(journeys.brief_for_prompt(), "")

    def test_never_raises(self):
        journeys._STORE_FILE = os.path.join(self._tmpdir, "\0非法", "x.jsonl")
        self.assertEqual(journeys.brief_for_prompt(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
