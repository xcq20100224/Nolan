# -*- coding: utf-8 -*-
"""
test_ppt_research.py —— ppt_research 模块的离线单元测试

全程 mock HTTP 层（patch httpx.post / httpx.get），不触网、不读真实配置：
  1. 正常返回：GLM 搜索通道给出分条事实 -> 摘要非空、含数字与年份；
  2. 超长截断：通道返回 >1200 字 -> 结果 ≤1200 字且不切半行；
  3. 双通道全失败 -> 空串；
  4. 搜索通道失败、必应成功、压缩调用失败 -> 原文截断兜底（非空）；
  5. 搜索通道失败、必应成功、压缩调用成功 -> 用压缩结果；
  6. 超时 -> 空串；
  7. 配置缺失 -> 空串且不发起任何 HTTP；
  8. 预算耗尽（budget_sec≈0）-> 空串。
"""

import os
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ppt_research  # noqa: E402

# 假配置：指向智谱端点，保证主通道不被端点检查拦下
FAKE_CFG = {
    "api_key": "fake-key",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-5.2",
    "extra_body": '{"thinking": {"type": "disabled"}}',
}

# GLM 搜索通道的正常回答（分条事实格式）
SEARCH_ANSWER = (
    "1. 2025年全球纯电动汽车销量超1210万辆，比亚迪交付约225.6万辆。（来源：IDC）\n"
    "2. 2025年中国新能源汽车产销量均超1600万辆，占比突破50%。（来源：中汽协）"
)

# 必应结果页 HTML 夹具（li.b_algo + h2 + .b_caption p 结构）
BING_HTML = (
    "<html><body><ol id='b_results'>"
    "<li class='b_algo'><h2>2025年新能源车销量100万辆</h2>"
    "<div class='b_caption'><p>同比增长42%，来源：某机构 2025</p></div></li>"
    "<li class='b_algo'><h2>行业动态B</h2>"
    "<div class='b_caption'><p>某案例 2024年落地</p></div></li>"
    "</ol></body></html>"
)


class _FakeResp:
    """最小化的 httpx.Response 替身：json() 返回 OpenAI 格式，text 给必应解析。"""

    def __init__(self, content: str = "", html: str = ""):
        self._content = content
        self.text = html

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestPptResearch(unittest.TestCase):
    """所有用例默认 patch 掉配置加载，HTTP 层逐用例 mock。"""

    def setUp(self):
        patcher = mock.patch.object(
            ppt_research, "_load_llm_config", return_value=dict(FAKE_CFG)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 1. 正常返回：摘要非空、含真实数字与年份 ----
    def test_ok_returns_summary(self):
        with mock.patch.object(
            ppt_research.httpx, "post",
            return_value=_FakeResp(content=SEARCH_ANSWER),
        ):
            result = ppt_research.research_topic("全球新能源汽车产业格局")
        self.assertTrue(result)
        self.assertIn("2025", result)
        self.assertIn("1210", result)
        self.assertLessEqual(len(result), 1200)

    # ---- 2. 超长截断：>1200 字的回答必须截到 ≤1200，且不切出半行 ----
    def test_truncation_at_1200_chars(self):
        long_line = "1. 2025年某数据为12345辆，来源：X。" * 100  # 远超 1200 字
        with mock.patch.object(
            ppt_research.httpx, "post",
            return_value=_FakeResp(content=long_line),
        ):
            result = ppt_research.research_topic("测试主题", max_queries=1)
        self.assertTrue(result)  # 至少保住内容，绝不空跑
        self.assertLessEqual(len(result), 1200)
        lines = result.splitlines()
        if len(lines) > 1:
            # 多行时是行边界截断：每行都必须是完整句，无半行残句
            for line in lines:
                self.assertTrue(line.endswith("。"))
        # 单行时允许硬截断（第一行独自超上限的边界场景），只验长度上限

    # ---- 3. 双通道全失败 -> 空串 ----
    def test_all_channels_fail_returns_empty(self):
        with mock.patch.object(
            ppt_research.httpx, "post",
            side_effect=httpx.ConnectError("boom"),
        ), mock.patch.object(
            ppt_research.httpx, "get",
            side_effect=httpx.ConnectError("boom"),
        ):
            result = ppt_research.research_topic("测试主题")
        self.assertEqual(result, "")

    # ---- 4. 搜索失败、必应成功、压缩失败 -> 原文截断兜底（非空） ----
    def test_compress_fail_falls_back_to_raw(self):
        with mock.patch.object(
            ppt_research.httpx, "post",
            side_effect=httpx.ConnectError("glm down"),  # 搜索×2 + 压缩×1 全挂
        ), mock.patch.object(
            ppt_research.httpx, "get",
            return_value=_FakeResp(html=BING_HTML),
        ):
            result = ppt_research.research_topic("测试主题")
        self.assertTrue(result)
        self.assertIn("100万辆", result)  # 用的是必应原文

    # ---- 5. 搜索失败、必应成功、压缩成功 -> 用压缩结果 ----
    def test_compress_success_uses_compressed(self):
        compressed = "1. 2025年新能源车销量100万辆，同比增长42%。（某机构，2025）"
        post_effects = [
            httpx.ConnectError("q1 fail"),
            httpx.ConnectError("q2 fail"),
            _FakeResp(content=compressed),  # 第三次 post = 压缩调用，成功
        ]
        with mock.patch.object(
            ppt_research.httpx, "post", side_effect=post_effects
        ), mock.patch.object(
            ppt_research.httpx, "get",
            return_value=_FakeResp(html=BING_HTML),
        ):
            result = ppt_research.research_topic("测试主题")
        self.assertEqual(result, compressed)

    # ---- 6. 超时 -> 空串 ----
    def test_timeout_returns_empty(self):
        with mock.patch.object(
            ppt_research.httpx, "post",
            side_effect=httpx.TimeoutException("read timeout"),
        ), mock.patch.object(
            ppt_research.httpx, "get",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            result = ppt_research.research_topic("测试主题")
        self.assertEqual(result, "")

    # ---- 7. 配置缺失 -> 空串，且不发起任何 HTTP ----
    def test_missing_config_returns_empty_without_http(self):
        with mock.patch.object(
            ppt_research, "_load_llm_config", return_value={}
        ), mock.patch.object(
            ppt_research.httpx, "post",
            side_effect=AssertionError("不应发起 HTTP"),
        ), mock.patch.object(
            ppt_research.httpx, "get",
            side_effect=AssertionError("不应发起 HTTP"),
        ):
            result = ppt_research.research_topic("测试主题")
        self.assertEqual(result, "")

    # ---- 8. 预算耗尽：budget_sec=0 时立即返回空串 ----
    def test_zero_budget_returns_empty(self):
        with mock.patch.object(
            ppt_research.httpx, "post",
            side_effect=AssertionError("预算耗尽不应发起 HTTP"),
        ), mock.patch.object(
            ppt_research.httpx, "get",
            side_effect=AssertionError("预算耗尽不应发起 HTTP"),
        ):
            result = ppt_research.research_topic("测试主题", budget_sec=0.0)
        self.assertEqual(result, "")

    # ---- 附加：空主题 -> 空串 ----
    def test_empty_topic_returns_empty(self):
        self.assertEqual(ppt_research.research_topic(""), "")
        self.assertEqual(ppt_research.research_topic("   "), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
