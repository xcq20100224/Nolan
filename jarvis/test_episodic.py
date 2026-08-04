# -*- coding: utf-8 -*-
"""episodic.py 纯单元测试：存储隔离到临时目录，不碰真实 data/episodic.json。

运行：python jarvis/test_episodic.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episodic


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class EpisodicTestBase(unittest.TestCase):
    """每个用例独立临时存储目录，模块缓存隔离。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_file = episodic._STORE_FILE
        episodic._STORE_FILE = os.path.join(self._tmpdir.name, "episodic.json")
        episodic._cache = None

    def tearDown(self):
        episodic._STORE_FILE = self._orig_file
        episodic._cache = None
        self._tmpdir.cleanup()

    def _inject(self, events):
        """直接写入缓存并落盘，绕过 log_event 以伪造任意时间戳。"""
        episodic._cache = events
        episodic._save()

    def _event(self, kind, summary, ts, salience=0.5, refs=None):
        return {"id": "t_" + summary[:8], "ts": _iso(ts), "kind": kind,
                "summary": summary, "refs": refs or [], "salience": salience}


class TestLogEvent(EpisodicTestBase):

    def test_write_and_persist(self):
        ev = episodic.log_event("task", "先生让我搜新闻写日报", refs=["news.md"])
        self.assertEqual(ev["kind"], "task")
        self.assertEqual(ev["salience"], 0.5)          # task 默认显著度
        self.assertTrue(os.path.exists(episodic._STORE_FILE))
        with open(episodic._STORE_FILE, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0]["summary"], "先生让我搜新闻写日报")

    def test_default_salience_by_kind(self):
        self.assertEqual(episodic.log_event("error", "x")["salience"], 0.8)
        self.assertEqual(episodic.log_event("milestone", "x")["salience"], 0.8)
        self.assertEqual(episodic.log_event("task", "x")["salience"], 0.5)
        self.assertEqual(episodic.log_event("outcome", "x")["salience"], 0.5)
        self.assertEqual(episodic.log_event("conversation", "x")["salience"], 0.2)
        # 显式显著度优先，且钳制到 [0,1]
        self.assertEqual(episodic.log_event("task", "x", salience=0.95)["salience"], 0.95)
        self.assertEqual(episodic.log_event("task", "x", salience=5)["salience"], 1.0)

    def test_summary_truncated_to_120(self):
        ev = episodic.log_event("task", "长" * 300)
        self.assertEqual(len(ev["summary"]), 120)

    def test_bad_input_never_raises(self):
        ev = episodic.log_event(None, None, refs="notalist")   # 异常输入
        self.assertEqual(ev["kind"], "conversation")           # 非法 kind 降级
        self.assertEqual(ev["refs"], ["notalist"])             # 非标量 refs 包容
        self.assertIsInstance(episodic.log_event("weird_kind", "s"), dict)


class TestTimeline(EpisodicTestBase):

    def test_filter_by_days_and_order(self):
        now = datetime.now()
        self._inject([
            self._event("task", "十天前的事", now - timedelta(days=10)),
            self._event("task", "昨天的事", now - timedelta(days=1)),
            self._event("task", "今天的事", now),
        ])
        tl = episodic.timeline(days=7)
        self.assertEqual([e["summary"] for e in tl], ["今天的事", "昨天的事"])

    def test_kinds_filter_and_limit(self):
        now = datetime.now()
        self._inject([
            self._event("conversation", "闲聊1", now),
            self._event("error", "报错1", now - timedelta(hours=1), salience=0.8),
            self._event("error", "报错2", now - timedelta(hours=2), salience=0.8),
        ])
        errs = episodic.timeline(days=7, kinds=["error"], limit=1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["summary"], "报错1")


class TestSearch(EpisodicTestBase):

    def test_ranking_recency_and_salience(self):
        now = datetime.now()
        self._inject([
            # 同关键词：显著度高且时间近者必须排前
            self._event("conversation", "记事本 打开了", now - timedelta(days=20),
                        salience=0.2),
            self._event("error", "记事本 任务失败", now - timedelta(hours=3),
                        salience=0.8),
        ])
        hits = episodic.search("记事本", days=30)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["kind"], "error")

    def test_search_refs_and_empty_query(self):
        now = datetime.now()
        self._inject([self._event("task", "写了日报", now, refs=["daily_report.md"])])
        self.assertEqual(len(episodic.search("daily_report", days=30)), 1)
        self.assertEqual(episodic.search("", days=30), [])
        self.assertEqual(episodic.search("不存在的关键词", days=30), [])

    def test_days_window(self):
        now = datetime.now()
        self._inject([self._event("task", "旧任务 周报", now - timedelta(days=60))])
        self.assertEqual(episodic.search("周报", days=30), [])
        self.assertEqual(len(episodic.search("周报", days=90)), 1)


class TestBrief(EpisodicTestBase):

    def test_brief_content_and_length_cap(self):
        now = datetime.now()
        self._inject([
            self._event("task", "搜新闻写日报", now - timedelta(hours=5), salience=0.5),
            self._event("error", "记事本任务出错", now - timedelta(hours=1), salience=0.8),
            self._event("conversation", "闲聊天气", now - timedelta(hours=1), salience=0.2),
        ])
        brief = episodic.brief_for_prompt(max_chars=300)
        self.assertTrue(brief.startswith("近期经历："))
        self.assertIn("搜新闻写日报", brief)
        self.assertIn("记事本任务出错", brief)
        self.assertNotIn("闲聊天气", brief)          # 低显著度不进简报
        self.assertLessEqual(len(brief), 300)

    def test_brief_hard_cap_under_pressure(self):
        now = datetime.now()
        self._inject([self._event("error", "第%d次报错事件摘要比较长" % i,
                                  now - timedelta(hours=i % 40), salience=0.8)
                      for i in range(40)])
        brief = episodic.brief_for_prompt(max_chars=120)
        self.assertLessEqual(len(brief), 120)        # 长度硬约束

    def test_brief_empty_when_nothing_recent(self):
        now = datetime.now()
        self._inject([self._event("error", "三天前的错", now - timedelta(days=3),
                                  salience=0.8)])
        self.assertEqual(episodic.brief_for_prompt(), "")


class TestPrune(EpisodicTestBase):

    def test_prune_by_age(self):
        now = datetime.now()
        self._inject([
            self._event("task", "百天前的旧事", now - timedelta(days=100)),
            self._event("task", "昨天的新事", now - timedelta(days=1)),
        ])
        removed = episodic.prune(max_age_days=90)
        self.assertEqual(removed, 1)
        self.assertEqual(len(episodic.timeline(days=365)), 1)

    def test_prune_by_count_keeps_high_salience(self):
        now = datetime.now()
        events = [self._event("conversation", "闲聊%d" % i,
                              now - timedelta(days=i), salience=0.2)
                  for i in range(10)]
        events.append(self._event("milestone", "重要里程碑",
                                  now - timedelta(days=9), salience=0.9))
        self._inject(events)
        removed = episodic.prune(max_age_days=365, max_events=3)
        self.assertEqual(removed, 8)
        survivors = episodic.timeline(days=365, limit=50)
        summaries = [e["summary"] for e in survivors]
        self.assertIn("重要里程碑", summaries)       # 高显著度存活


class TestCorruptionRecovery(EpisodicTestBase):

    def test_corrupt_file_backed_up_and_reopened(self):
        os.makedirs(os.path.dirname(episodic._STORE_FILE), exist_ok=True)
        with open(episodic._STORE_FILE, "w", encoding="utf-8") as f:
            f.write("{broken json,,")                 # 人为制造损坏
        episodic._cache = None
        ev = episodic.log_event("task", "损坏后第一条")  # 不抛异常
        self.assertEqual(ev["summary"], "损坏后第一条")
        self.assertTrue(os.path.exists(episodic._STORE_FILE + ".corrupt"))
        self.assertEqual(len(episodic.timeline(days=1)), 1)


class TestCapacityDiscipline(EpisodicTestBase):

    def test_oversized_file_triggers_prune_on_save(self):
        now = datetime.now()
        # 造一个超 500KB 的存储：低显著度旧事件填充 + 一条高显著度新事件
        filler = [self._event("conversation", "填充闲聊%d" % i + "水" * 100,
                              now - timedelta(days=80), salience=0.2)
                  for i in range(3000)]
        self._inject(filler)
        self.assertGreater(os.path.getsize(episodic._STORE_FILE), 500 * 1024)
        episodic.log_event("milestone", "容量淘汰后必须存活", salience=0.9)
        self.assertLess(os.path.getsize(episodic._STORE_FILE), 500 * 1024)
        survivors = episodic.timeline(days=365, limit=5000)
        self.assertIn("容量淘汰后必须存活", [e["summary"] for e in survivors])
        self.assertLessEqual(len(survivors), 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
