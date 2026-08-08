# -*- coding: utf-8 -*-
"""PPT 纠正路由（_ppt_correction_route / _record_ppt_context）单元测试。

病例来源（2026-08-08 真机截图）：
  主人：「给我做一个开学季的PPT，10页」→ Nolan 做好
  主人：「不对，不对，是站在班主任的视角，告诉同学们要开学了」
        → Nolan 却在聊天里写了一段开学通知文稿（没改 PPT）
  主人：「不，是PPT」
        → Nolan 发明了新主题「苏州外国语学校」、页数回落 8
修复后：纠正轮由规则层确定性路由，继承上一轮主题/页数/风格，不许 LLM 即兴。

运行：python test_ppt_correction.py
"""
import time
import unittest

import brain


def _fresh_ctx():
    return {
        "topic": "开学季",
        "pages": 10,
        "style": "课堂讲解",
        "file": "开学季_20260808-1119.pptx",
        "ts": time.time(),
    }


class TestCorrectionRoute(unittest.TestCase):
    def setUp(self):
        brain._LAST_PPT = _fresh_ctx()

    def tearDown(self):
        brain._LAST_PPT = None

    def test_perspective_correction_regenerates_with_inheritance(self):
        """病例轮 2：视角纠正 → 重做，主题合并、页数继承 10"""
        r = brain._ppt_correction_route("不对，不对，是站在班主任的视角，告诉同学们要开学了")
        self.assertIsNotNone(r)
        tool, args = r
        self.assertEqual(tool, "make_ppt")
        self.assertIn("开学季", args["topic"])
        self.assertIn("班主任", args["topic"])
        self.assertEqual(args["pages"], 10)
        self.assertEqual(args["style"], "课堂讲解")

    def test_bare_negation_ppt_keeps_original_brief(self):
        """病例轮 3：「不，是PPT」→ 按原 brief 重做，绝不发明新主题"""
        r = brain._ppt_correction_route("不，是PPT")
        tool, args = r
        self.assertEqual(tool, "make_ppt")
        self.assertEqual(args["topic"], "开学季")
        self.assertEqual(args["pages"], 10)

    def test_page_targeted_edit_goes_to_editor(self):
        """指向具体页/版式 → edit_ppt 原位修改，文件名继承"""
        r = brain._ppt_correction_route("把第3页换成大数字版式")
        tool, args = r
        self.assertEqual(tool, "edit_ppt")
        self.assertEqual(args["file_name"], "开学季_20260808-1119.pptx")
        self.assertIn("大数字", args["instruction"])

    def test_page_count_override(self):
        """纠正里另指页数以新值为准"""
        r = brain._ppt_correction_route("不对，做成12页")
        tool, args = r
        self.assertEqual(tool, "make_ppt")
        self.assertEqual(args["pages"], 12)

    def test_normal_chat_not_hijacked(self):
        self.assertIsNone(brain._ppt_correction_route("今天天气怎么样"))

    def test_other_domain_change_not_hijacked(self):
        """「换个背景/换首歌」不许被 PPT 路由劫持"""
        self.assertIsNone(brain._ppt_correction_route("帮我换个聊天背景"))
        self.assertIsNone(brain._ppt_correction_route("不对，换首歌"))

    def test_stale_context_expires(self):
        brain._LAST_PPT["ts"] = time.time() - 3 * 3600
        self.assertIsNone(brain._ppt_correction_route("不对，重来"))

    def test_no_context_no_route(self):
        brain._LAST_PPT = None
        self.assertIsNone(brain._ppt_correction_route("不对，重来"))

    def test_fresh_new_request_not_hijacked(self):
        """全新的 PPT 请求（无纠正信号）走 LLM 正常路径，不继承旧主题"""
        self.assertIsNone(brain._ppt_correction_route("帮我做一个关于人工智能的PPT"))


class TestRecordContext(unittest.TestCase):
    def tearDown(self):
        brain._LAST_PPT = None

    def test_record_on_make_ppt_success(self):
        result = ("好的先生，PPT 已经做好并放进文件柜了：《开学季》，"
                  "文件名 开学季_20260808-1119.pptx，共 10 页。")
        brain._record_ppt_context("make_ppt",
                                  {"topic": "开学季", "pages": 10, "style": "课堂讲解"},
                                  result)
        ctx = brain._LAST_PPT
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["topic"], "开学季")
        self.assertEqual(ctx["pages"], 10)
        self.assertEqual(ctx["file"], "开学季_20260808-1119.pptx")

    def test_failure_result_not_recorded(self):
        brain._record_ppt_context("make_ppt", {"topic": "X", "pages": 8}, "抱歉先生，PPT 没做成。")
        self.assertIsNone(brain._LAST_PPT)

    def test_edit_ppt_refreshes_ttl(self):
        brain._LAST_PPT = _fresh_ctx()
        brain._LAST_PPT["ts"] = time.time() - 7000  # 接近过期的边缘之外
        brain._record_ppt_context("edit_ppt", {"file_name": "a.pptx"}, "好的先生，第3页已改好。")
        self.assertAlmostEqual(brain._LAST_PPT["ts"], time.time(), delta=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
