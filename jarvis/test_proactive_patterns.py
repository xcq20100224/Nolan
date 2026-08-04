# -*- coding: utf-8 -*-
"""
test_proactive_patterns.py —— H2 理解驱动主动性：模式识别与预判 纯单元测试
无网络、无 GUI、无外部服务：GLM 调用全部 mock，episodic/memory_v2 数据源
全部经 timeline_fn / habits_fn / events_fn / today_events 注入替身，
时间全部经 now 注入——可复现，不碰真实存储文件。

覆盖：
    detect_patterns          ≥3 次聚类命中 / 2 次不命中 / 时段散乱不命中 /
                               部分入窗拉低置信度 / 置信度公式 / habit 佐证加成 /
                               同一天三连发不算惯例
    high_confidence          置信度 ≥0.8 且次数 ≥5 双门槛
    current_anticipation     命中惯常时段且今日未发生 → 素材；
                               今日已发生 → 沉默；不在时段 → 沉默
    generate_initiative      素材优先级：到期消息 > 当日预判 > 画像寒暄；
                               预判 prompt 指示「提起惯例并主动提出帮忙」；
                               模型异常回退预判模板；patterns 现场判定；
                               无素材沉默且不调模型
    should_initiate          高置信预判放宽「刚交互」闸门到 2 分钟；
                               安静时段 / 速率限制对预判一律不放松
    memory_v2.habits         只读查询 habit 类，不污染 recall 记账

运行：python -m unittest jarvis.test_proactive_patterns -v
     或在 jarvis 目录内 python test_proactive_patterns.py
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_v2   # noqa: E402
import proactive   # noqa: E402

BASE = datetime(2026, 1, 10, 10, 0, 0)  # 周六上午十点，非安静时段


def _event(days_ago, hour, summary, kind="task", minute=5):
    """构造一条 fake episodic 事件（ISO 时间戳，绕开真实存储）。"""
    ts = (BASE - timedelta(days=days_ago)).replace(hour=hour, minute=minute)
    return {"ts": ts.isoformat(), "kind": kind, "summary": summary,
            "refs": [], "salience": 0.5}


def _five_days_report_events():
    """连续 5 天 9 点写日报——教科书级每日惯例。"""
    return [_event(d, 9, "先生写日报") for d in range(1, 6)]


class TestDetectPatterns(unittest.TestCase):
    """detect_patterns：时间聚类 + 关键词粗聚，纯统计不调 LLM。"""

    def test_cluster_hit_three_plus(self):
        # ≥3 次（此处 5 次）同关键词同时段 → 命中模式
        pats = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: _five_days_report_events())
        self.assertEqual(len(pats), 1)
        p = pats[0]
        self.assertEqual(p["usual_hour"], 9)
        self.assertEqual(p["occurrences"], 5)
        self.assertEqual(p["days_span"], 5)
        self.assertIn("日报", p["keyword"])  # 人类可读关键词而非裸 bigram
        self.assertGreaterEqual(p["confidence"], 0.8)
        self.assertTrue(proactive.high_confidence(p))

    def test_two_occurrences_no_pattern(self):
        # 2 次 → 样本不足，沉默是对的
        events = _five_days_report_events()[:2]
        self.assertEqual(proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events), [])

    def test_scattered_hours_no_pattern(self):
        # 同关键词但时段散乱（8/12/15/19/22 点）→ 无 ±1h 窗口凑够 3 次
        events = [_event(d, h, "先生写日报")
                  for d, h in zip(range(1, 6), (8, 12, 15, 19, 22))]
        self.assertEqual(proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events), [])

    def test_partial_window_lowers_confidence(self):
        # 5 次里 3 次在 9 点、2 次在 15 点 → 窗口内 3 次成模式，
        # 但时间一致性 3/5 拉低置信度，不配高置信
        events = [_event(1, 9, "先生写日报"), _event(2, 9, "先生写日报"),
                  _event(3, 10, "先生写日报"),  # 10 点仍在 9±1 窗口内
                  _event(4, 15, "先生写日报"), _event(5, 15, "先生写日报")]
        pats = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events)
        self.assertEqual(len(pats), 1)
        p = pats[0]
        self.assertEqual(p["occurrences"], 3)
        self.assertAlmostEqual(p["confidence"], round(0.68 * 0.6, 2))
        self.assertFalse(proactive.high_confidence(p))

    def test_confidence_formula_and_habit_boost(self):
        # 置信度 = min(1, 0.5+0.06×次数) × 一致性；5 次全入窗 = 0.80
        events = _five_days_report_events()
        p = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events,
            habits_fn=lambda: [])[0]
        self.assertEqual(p["confidence"], 0.8)
        self.assertFalse(p["habit_backed"])
        # habit 类记忆佐证同一关键词 → +0.1
        p2 = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events,
            habits_fn=lambda: [{"content": "先生习惯早上写日报"}])[0]
        self.assertEqual(p2["confidence"], 0.9)
        self.assertTrue(p2["habit_backed"])

    def test_same_day_burst_not_a_habit(self):
        # 同一天 9/9/10 点三连发 → 冲动不是惯例，跨天数门槛挡掉
        events = [_event(1, 9, "先生写日报", minute=5),
                  _event(1, 9, "先生写日报", minute=40),
                  _event(1, 10, "先生写日报")]
        self.assertEqual(proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: events), [])

    def test_empty_and_garbage_events(self):
        self.assertEqual(proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: []), [])
        garbage = [{"ts": "不是时间", "summary": "先生写日报"},
                   {"no_ts": True}, "不是字典", None]
        self.assertEqual(proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: garbage), [])

    def test_high_confidence_thresholds(self):
        self.assertTrue(proactive.high_confidence(
            {"confidence": 0.8, "occurrences": 5}))
        self.assertFalse(proactive.high_confidence(
            {"confidence": 0.79, "occurrences": 5}))   # 置信度差一点都不行
        self.assertFalse(proactive.high_confidence(
            {"confidence": 0.9, "occurrences": 4}))    # 次数少一次也不行
        self.assertFalse(proactive.high_confidence({}))
        self.assertFalse(proactive.high_confidence(None))


class TestCurrentAnticipation(unittest.TestCase):
    """current_anticipation：此刻命中惯常时段 且 今天尚未发生 → 预判素材。"""

    def setUp(self):
        self.pats = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: _five_days_report_events())
        self.assertEqual(len(self.pats), 1)

    def test_hit_returns_material(self):
        ant = proactive.current_anticipation(
            {"now": BASE.replace(hour=9, minute=30), "today_events": []}, self.pats)
        self.assertIsNotNone(ant)
        self.assertIn("9", ant)
        self.assertIn("日报", ant)
        self.assertIn("今天", ant)

    def test_window_edge_included(self):
        # usual_hour ±1h 边界也算命中（8:59 与 10:00）
        for h, m in ((8, 59), (10, 0)):
            ant = proactive.current_anticipation(
                {"now": BASE.replace(hour=h, minute=m), "today_events": []},
                self.pats)
            self.assertIsNotNone(ant, (h, m))

    def test_outside_window_silence(self):
        self.assertIsNone(proactive.current_anticipation(
            {"now": BASE.replace(hour=15, minute=0), "today_events": []}, self.pats))

    def test_today_already_done_silence(self):
        # 今天已经写过日报（摘要含 match_token「日报」）→ 不重复预判
        done = [{"ts": BASE.replace(hour=9, minute=10).isoformat(),
                 "kind": "task", "summary": "先生写了日报"}]
        self.assertIsNone(proactive.current_anticipation(
            {"now": BASE.replace(hour=9, minute=30), "today_events": done},
            self.pats))

    def test_yesterday_done_does_not_count(self):
        # 「已发生」只看今天：昨天写的不影响今天的预判
        yesterday = [{"ts": (BASE - timedelta(days=1)).replace(hour=9).isoformat(),
                      "kind": "task", "summary": "先生写了日报"}]
        ant = proactive.current_anticipation(
            {"now": BASE.replace(hour=9, minute=30), "today_events": yesterday},
            self.pats)
        self.assertIsNotNone(ant)

    def test_no_patterns_silence(self):
        self.assertIsNone(proactive.current_anticipation(
            {"now": BASE.replace(hour=9), "today_events": []}, []))
        self.assertIsNone(proactive.current_anticipation(
            {"now": BASE.replace(hour=9), "today_events": []}, None))

    def test_events_fn_injection(self):
        # context 不带 today_events 时走 events_fn（今日过滤在函数内完成）
        done_today = [{"ts": BASE.replace(hour=9, minute=10).isoformat(),
                       "kind": "task", "summary": "先生写了日报"}]
        self.assertIsNone(proactive.current_anticipation(
            {"now": BASE.replace(hour=9, minute=30)}, self.pats,
            events_fn=lambda: done_today))


class TestGenerateInitiativePriority(unittest.TestCase):
    """generate_initiative：素材优先级 到期消息 > 当日预判 > 画像寒暄。"""

    def setUp(self):
        proactive._last_initiative_at = 0.0  # 清内部记账，防用例间串扰
        self.captured = []

    def _caller(self, reply="先生，好的。"):
        def call(prompt):
            self.captured.append(prompt)
            return reply
        return call

    def test_due_beats_anticipation_and_profile(self):
        msg = proactive.generate_initiative(
            {"now": BASE,
             "due_messages": ["条件触发：该喝水了"],
             "anticipation": "主人通常 9 点左右处理「写日报」相关的事，今天还没有相关记录",
             "profile": "偏好：黑咖啡"},
            llm_caller=self._caller())
        self.assertEqual(msg, "先生，好的。")
        prompt = self.captured[0]
        self.assertIn("刚到期的事项", prompt)
        self.assertNotIn("惯例预判", prompt)      # 到期事件在场，预判让位
        self.assertNotIn("你记得的先生", prompt)  # 画像寒暄同样让位

    def test_anticipation_beats_profile_and_instructs_butler_tone(self):
        msg = proactive.generate_initiative(
            {"now": BASE,
             "anticipation": "主人通常 9 点左右处理「写日报」相关的事，今天还没有相关记录",
             "profile": "偏好：黑咖啡"},
            llm_caller=self._caller("先生，到写日报的时间了，需要我整理素材吗？"))
        self.assertIn("日报", msg)
        prompt = self.captured[0]
        self.assertIn("惯例预判", prompt)
        self.assertIn("主动提出帮忙", prompt)  # prompt 明确指示提起惯例并主动帮忙
        self.assertNotIn("你记得的先生", prompt)

    def test_profile_only_is_last_resort(self):
        proactive.generate_initiative(
            {"now": BASE, "profile": "偏好：黑咖啡"},
            llm_caller=self._caller())
        prompt = self.captured[0]
        self.assertIn("你记得的先生", prompt)
        self.assertNotIn("惯例预判", prompt)
        self.assertNotIn("主动提出帮忙", prompt)

    def test_llm_failure_falls_back_to_anticipation_template(self):
        def _boom(p):
            raise RuntimeError("模型不在线")
        msg = proactive.generate_initiative(
            {"now": BASE,
             "anticipation": "主人通常 9 点左右处理「写日报」相关的事，今天还没有相关记录"},
            _boom)
        self.assertIsNotNone(msg)
        self.assertIn("日报", msg)
        self.assertIn("需要我", msg)

    def test_patterns_auto_anticipation(self):
        # 主控只给 patterns：generate 现场判定预判素材并驱动 prompt
        pats = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: _five_days_report_events())
        msg = proactive.generate_initiative(
            {"now": BASE.replace(hour=9, minute=30),
             "patterns": pats, "today_events": []},
            llm_caller=self._caller("先生，日报时间到了。"))
        self.assertEqual(msg, "先生，日报时间到了。")
        self.assertIn("惯例预判", self.captured[0])

    def test_patterns_today_done_no_material_silence_and_no_llm_call(self):
        # 今天已发生 → 无素材 → 沉默，且绝不打扰模型
        pats = proactive.detect_patterns(
            now=BASE, timeline_fn=lambda days: _five_days_report_events())
        done = [{"ts": BASE.replace(hour=9, minute=10).isoformat(),
                 "kind": "task", "summary": "先生写了日报"}]
        msg = proactive.generate_initiative(
            {"now": BASE.replace(hour=9, minute=30),
             "patterns": pats, "today_events": done},
            llm_caller=self._caller())
        self.assertIsNone(msg)
        self.assertEqual(self.captured, [])

    def test_no_material_returns_none(self):
        self.assertIsNone(proactive.generate_initiative(
            {"now": BASE}, llm_caller=self._caller()))
        self.assertEqual(self.captured, [])


class TestShouldInitiateHighConfidence(unittest.TestCase):
    """should_initiate：高置信预判只放宽「刚交互」闸门，其余两道一律不动。"""

    def setUp(self):
        proactive._last_initiative_at = 0.0

    def test_high_confidence_relaxes_idle_gate_to_two_minutes(self):
        # 3 分钟前刚交互：常规（5 分钟门槛）拒绝；高置信（2 分钟门槛）放行
        ctx = {"now": BASE, "last_user_activity": BASE - timedelta(minutes=3)}
        self.assertFalse(proactive.should_initiate(ctx))
        ctx["high_confidence_anticipation"] = True
        self.assertTrue(proactive.should_initiate(ctx))

    def test_high_confidence_still_blocks_under_two_minutes(self):
        # 1 分钟前刚交互：放宽也不是没有底线
        self.assertFalse(proactive.should_initiate(
            {"now": BASE,
             "last_user_activity": BASE - timedelta(minutes=1),
             "high_confidence_anticipation": True}))

    def test_quiet_hours_never_relaxed(self):
        # 安静时段对预判一律不放松：深夜再确定的事也不吵他睡觉
        late = BASE.replace(hour=23, minute=30)
        self.assertFalse(proactive.should_initiate(
            {"now": late,
             "last_user_activity": late - timedelta(minutes=10),
             "high_confidence_anticipation": True}))

    def test_rate_limit_never_relaxed(self):
        # 速率限制对预判一律不放松：每小时 ≤1 次不变
        self.assertFalse(proactive.should_initiate(
            {"now": BASE,
             "last_initiative": BASE - timedelta(minutes=10),
             "high_confidence_anticipation": True}))

    def test_regular_gates_unchanged(self):
        # 回归：三重闸门原有行为一字未动
        self.assertTrue(proactive.should_initiate(
            {"now": BASE,
             "last_initiative": BASE - timedelta(hours=2),
             "last_user_activity": BASE - timedelta(minutes=10)}))
        self.assertFalse(proactive.should_initiate(
            {"now": BASE,
             "last_user_activity": BASE - timedelta(minutes=4, seconds=59)}))


class TestMemoryV2Habits(unittest.TestCase):
    """memory_v2.habits：H2 纯加法便捷查询，只读不记账。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="nolan_habits_test_")
        self._orig_store = memory_v2._STORE
        memory_v2._STORE = os.path.join(self._tmpdir, "memory.json")

    def tearDown(self):
        memory_v2._STORE = self._orig_store

    def test_habits_returns_only_habit_category(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        memory_v2.remember("先生每天早上写日报", category="habit")
        memory_v2.remember("先生每周三晚上打篮球", category="habit")
        hs = memory_v2.habits()
        self.assertEqual(len(hs), 2)
        self.assertTrue(all(h["category"] == "habit" for h in hs))

    def test_habits_is_read_only(self):
        e = memory_v2.remember("先生每天早上写日报", category="habit")
        memory_v2.habits()
        memory_v2.habits()
        # 「翻阅」不算「想起」：recall 记账字段一律不动
        hits = memory_v2.recall("日报", limit=1)
        self.assertEqual(hits[0]["recall_count"], e["recall_count"] + 1)  # 只有这次 recall 记账

    def test_habits_empty_store(self):
        self.assertEqual(memory_v2.habits(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
