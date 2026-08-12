# -*- coding: utf-8 -*-
"""时间意图防劫持测试（_parse_intent 时间分支）。

病例来源（2026-08-12 真机）：「帮我做一份PPT，主题是时间管理，3页」
被规则层时间意图劫持，直接报时，PPT 没做成——「时间」只是话题不是意图。

运行：python test_intent_time.py
"""
import unittest

import brain


class TestTimeIntentHijack(unittest.TestCase):
    """任务句里的「时间/日期」只是话题，不许被报时劫持"""

    def test_ppt_topic_time_management_not_hijacked(self):
        r = brain._parse_intent("帮我做一份PPT，主题是时间管理，3页")
        self.assertFalse(r and r[0] == "get_time", f"被劫持: {r}")

    def test_ppt_topic_about_time_not_hijacked(self):
        r = brain._parse_intent("给我做一个关于时间管理的PPT")
        self.assertFalse(r and r[0] == "get_time", f"被劫持: {r}")

    def test_write_essay_about_time_not_hijacked(self):
        r = brain._parse_intent("写一篇关于时间的作文")
        self.assertFalse(r and r[0] == "get_time", f"被劫持: {r}")

    def test_search_schedule_not_hijacked(self):
        r = brain._parse_intent("搜一下奥运会时间安排")
        self.assertFalse(r and r[0] == "get_time", f"被劫持: {r}")

    def test_pure_time_query_still_works(self):
        self.assertEqual(brain._parse_intent("现在几点"), ("get_time", {}))
        self.assertEqual(brain._parse_intent("现在时间"), ("get_time", {}))
        self.assertEqual(brain._parse_intent("今天几号"), ("get_time", {}))
        self.assertEqual(brain._parse_intent("今天星期几"), ("get_time", {}))

    def test_user_attribute_question_not_hijacked(self):
        """「我一般几点起床」疑问主体是主人习惯，不报时"""
        r = brain._parse_intent("我一般几点起床")
        self.assertFalse(r and r[0] == "get_time", f"误报时: {r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
