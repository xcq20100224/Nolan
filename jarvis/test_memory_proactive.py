# -*- coding: utf-8 -*-
"""
test_memory_proactive.py —— memory_v2 / proactive 纯单元测试
无网络、无 GUI、无外部依赖：GLM 调用全部 mock，存储全部隔离到临时目录。

覆盖：
    memory_v2.remember        去重合并（相似内容不重复堆积，recall 记账）
    memory_v2.recall          关键词 + 时间 + 频率加权排序
    memory_v2.profile_summary 长度硬约束
    memory_v2.extract_from_turn  mock 萃取（正常 JSON / 包裹废话 / 空结果）
    memory_v2.forget / stats  删除与统计
    proactive.should_initiate 三种拒绝场景（安静时段/速率限制/用户活跃）+ 一种通过
    proactive.generate_initiative  mock 生成 / 模型异常兜底 / 无素材沉默

运行：python -m unittest jarvis.test_memory_proactive -v
     或在 jarvis 目录内 python test_memory_proactive.py
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_v2   # noqa: E402
import proactive   # noqa: E402


class _StoreIsolatedTestCase(unittest.TestCase):
    """每个用例一套全新的临时 memory.json，互不污染，也不碰真实数据目录。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="nolan_mem_test_")
        self._orig_store = memory_v2._STORE
        memory_v2._STORE = os.path.join(self._tmpdir, "memory.json")

    def tearDown(self):
        memory_v2._STORE = self._orig_store


class TestRememberDedup(_StoreIsolatedTestCase):
    """remember 的写入与相似去重。"""

    def test_remember_basic_fields(self):
        e = memory_v2.remember("先生喜欢黑咖啡", category="preference", source="user")
        self.assertTrue(e["id"].startswith("m_"))
        self.assertEqual(e["category"], "preference")
        self.assertEqual(e["source"], "user")
        self.assertEqual(e["recall_count"], 0)
        self.assertIn("created_at", e)
        self.assertIn("last_recalled", e)

    def test_remember_dedup_merges_similar(self):
        e1 = memory_v2.remember("先生喜欢黑咖啡，不加糖", category="preference")
        e2 = memory_v2.remember("先生喜欢黑咖啡", category="preference")
        # 相似内容 → 合并到同一条，id 不变，库内仍只有一条
        self.assertEqual(e1["id"], e2["id"])
        self.assertEqual(memory_v2.stats()["total"], 1)
        # 合并即「又被提起」：recall_count 记账 +1
        self.assertEqual(e2["recall_count"], 1)
        # 保留信息量更大的表述
        self.assertIn("不加糖", e2["content"])

    def test_remember_keeps_distinct_memories(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        memory_v2.remember("先生每周三晚上打篮球", category="habit")
        self.assertEqual(memory_v2.stats()["total"], 2)

    def test_remember_rejects_empty_and_bad_category(self):
        self.assertEqual(memory_v2.remember(""), {})
        e = memory_v2.remember("先生住在北京", category="nonsense")
        self.assertEqual(e["category"], "fact")  # 非法类别回退 fact


class TestRecallRanking(_StoreIsolatedTestCase):
    """recall 的关键词 + 时间 + 频率加权排序。"""

    def test_recall_keyword_relevance_first(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        memory_v2.remember("先生每天早上跑步", category="habit")
        memory_v2.remember("先生的妻子叫小林", category="person")
        hits = memory_v2.recall("咖啡", limit=5)
        # 命中关键词的排最前，且零命中条目不进入结果（防答非所问）
        self.assertEqual(len(hits), 1)
        self.assertIn("咖啡", hits[0]["content"])

    def test_recall_frequency_breaks_tie(self):
        # 两条都含「茶」：一条高频被想起，排序应靠前
        e_hot = memory_v2.remember("先生喝乌龙茶", category="preference")
        memory_v2.remember("先生买茶叶的地方在城南", category="fact")
        for _ in range(5):  # 人为制造频率差
            memory_v2.recall("乌龙", limit=1)
        hits = memory_v2.recall("茶", limit=5)
        self.assertEqual(hits[0]["id"], e_hot["id"])

    def test_recall_updates_recalled_bookkeeping(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        hits = memory_v2.recall("咖啡", limit=1)
        self.assertEqual(hits[0]["recall_count"], 1)

    def test_recall_limit_and_empty_store(self):
        self.assertEqual(memory_v2.recall("任意"), [])
        for i in range(6):
            memory_v2.remember("先生第%d件完全不同的事" % i, category="fact")
        self.assertLessEqual(len(memory_v2.recall("", limit=3)), 3)


class TestProfileSummary(_StoreIsolatedTestCase):
    """profile_summary 的分组与长度硬约束。"""

    def test_empty_store_returns_empty(self):
        self.assertEqual(memory_v2.profile_summary(), "")

    def test_grouped_by_category(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        memory_v2.remember("先生每天七点起床", category="habit")
        s = memory_v2.profile_summary()
        self.assertIn("偏好", s)
        self.assertIn("习惯", s)

    def test_length_hard_cap(self):
        for i in range(30):
            memory_v2.remember(
                "先生的一条相当长的记忆条目编号%d，用来把画像撑得特别特别长" % i,
                category="fact")
        for cap in (50, 100, 400):
            self.assertLessEqual(len(memory_v2.profile_summary(max_chars=cap)), cap)


class TestExtractFromTurn(_StoreIsolatedTestCase):
    """extract_from_turn 的 mock 萃取：GLM 一律替身，零网络。"""

    def test_mock_extract_normal_json(self):
        caller = lambda p: '[{"content": "先生偏好简洁的回答", "category": "preference"}]'
        items = memory_v2.extract_from_turn("以后回答简短点", "好的先生。", llm_caller=caller)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "preference")
        self.assertEqual(items[0]["source"], "extract")

    def test_mock_extract_wrapped_in_markdown(self):
        # 模型包 markdown 代码块 + 废话 → 防御式解析仍应命中
        caller = lambda p: '好的，结果如下：\n```json\n[{"content": "先生在上海工作", "category": "fact"}]\n```\n完毕'
        items = memory_v2.extract_from_turn("我在上海上班", "记下了。", llm_caller=caller)
        self.assertEqual(len(items), 1)
        self.assertIn("上海", items[0]["content"])

    def test_mock_extract_smalltalk_returns_empty(self):
        # 闲聊无可抽：模型返回空数组 → []
        self.assertEqual(memory_v2.extract_from_turn("嗯", "好的。", llm_caller=lambda p: "[]"), [])

    def test_mock_extract_garbage_and_exception(self):
        # 模型返回纯废话 / 抛异常 / 返回 None → 一律安全降级为 []
        self.assertEqual(memory_v2.extract_from_turn("今天不错", "是的先生。",
                                                     llm_caller=lambda p: "我不知道你在说什么"), [])
        def _boom(p):
            raise RuntimeError("网络超时")
        self.assertEqual(memory_v2.extract_from_turn("记住点什么", "好。", llm_caller=_boom), [])
        self.assertEqual(memory_v2.extract_from_turn("x", "y", llm_caller=lambda p: None), [])

    def test_mock_extract_bad_category_falls_back(self):
        caller = lambda p: '[{"content": "先生有台笔记本", "category": "weird"}]'
        items = memory_v2.extract_from_turn("我买了台笔记本", "很好。",
                                            llm_caller=caller)
        self.assertEqual(items[0]["category"], "fact")

    def test_integration_contract_loop(self):
        # 集成契约：for item in extract_from_turn(u, a): remember(**item)
        caller = lambda p: '[{"content": "先生喜欢黑咖啡", "category": "preference", "source": "extract"}]'
        for item in memory_v2.extract_from_turn("我喜欢黑咖啡", "记下了。", llm_caller=caller):
            memory_v2.remember(**item)
        self.assertEqual(memory_v2.stats()["total"], 1)


class TestForgetAndStats(_StoreIsolatedTestCase):
    def test_forget_and_stats(self):
        memory_v2.remember("先生喜欢黑咖啡", category="preference")
        memory_v2.remember("先生每天七点起床", category="habit")
        self.assertEqual(memory_v2.forget("咖啡"), 1)
        self.assertEqual(memory_v2.forget("不存在的关键词"), 0)
        st = memory_v2.stats()
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["by_category"], {"habit": 1})


class TestShouldInitiate(unittest.TestCase):
    """should_initiate：三种拒绝 + 一种通过。now 全部显式注入，与时间无关可复现。"""

    def setUp(self):
        proactive._last_initiative_at = 0.0  # 清内部记账，防用例间串扰
        self.base = datetime(2026, 1, 5, 10, 0, 0)  # 周一上午十点

    def test_reject_quiet_hours(self):
        # 深夜 23:30 与清晨 06:00 均在安静时段
        self.assertFalse(proactive.should_initiate(
            {"now": self.base.replace(hour=23, minute=30)}))
        self.assertFalse(proactive.should_initiate(
            {"now": self.base.replace(hour=6)}))

    def test_reject_rate_limit(self):
        # 十分钟前刚主动开口过，配额每小时 1 次 → 拒绝
        self.assertFalse(proactive.should_initiate(
            {"now": self.base,
             "last_initiative": self.base - timedelta(minutes=10)}))

    def test_reject_user_recently_active(self):
        # 用户一分钟前刚交互 → 不抢话
        self.assertFalse(proactive.should_initiate(
            {"now": self.base,
             "last_user_activity": self.base - timedelta(minutes=1)}))

    def test_pass_all_gates_open(self):
        # 上午十点、两小时没开口、用户十分钟没活动 → 允许
        self.assertTrue(proactive.should_initiate(
            {"now": self.base,
             "last_initiative": self.base - timedelta(hours=2),
             "last_user_activity": self.base - timedelta(minutes=10)}))

    def test_module_internal_bookkeeping_counts(self):
        # context 不带 last_initiative 时，generate 的记账也参与速率限制
        proactive.generate_initiative(
            {"now": self.base, "due_messages": ["条件触发：该喝水了"]},
            llm_caller=lambda p: "先生，该喝水了。")
        self.assertFalse(proactive.should_initiate({"now": self.base}))


class TestGenerateInitiative(unittest.TestCase):
    def setUp(self):
        proactive._last_initiative_at = 0.0
        self.base = datetime(2026, 1, 5, 10, 0, 0)

    def test_mock_llm_generates_message(self):
        ctx = {"now": self.base,
               "profile": "偏好：黑咖啡",
               "due_messages": ["条件触发：该喝水了"]}
        msg = proactive.generate_initiative(ctx, llm_caller=lambda p: "先生，该喝水了。")
        self.assertEqual(msg, "先生，该喝水了。")

    def test_llm_exception_falls_back_to_template(self):
        def _boom(p):
            raise RuntimeError("模型不在线")
        msg = proactive.generate_initiative(
            {"now": self.base, "due_messages": ["条件触发：带伞"]}, _boom)
        self.assertIn("带伞", msg)

    def test_no_material_returns_none(self):
        # 无画像无到期事件 → 沉默是合法决策，绝不没话找话
        self.assertIsNone(proactive.generate_initiative({}, llm_caller=lambda p: "硬编一句话"))

    def test_llm_empty_falls_back(self):
        msg = proactive.generate_initiative(
            {"now": self.base, "due_messages": ["条件触发：站起来活动"]},
            llm_caller=lambda p: "")
        self.assertIn("站起来活动", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
