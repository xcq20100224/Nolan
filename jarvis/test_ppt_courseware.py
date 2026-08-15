# -*- coding: utf-8 -*-
"""
ppt_maker 教学课件型（N3）单元测试：纯 mock，LLM 0 真实调用。
运行：python -m unittest test_ppt_courseware -v   （在 jarvis/ 目录下）

覆盖：
- _rule_intent：课件型触发词、「给/教…学生」句式、与讲话型的分界；
- _parse_intent：课件型与讲话型同权 LLM 精修、失败回退规则、报告型不烧调用；
- _intent_block：课件型硬约束块（课堂结构/版式偏好/教师口吻/禁市场腔）；
- make_ppt 集成：课件型保留研究（research_chars>0）、讲话型跳过研究、
  进度埋点文案。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppt_maker


def _outline_json(n=3, title="一元二次方程解法"):
    """合法大纲：n 页 bullets，够 make_ppt 走完管线即可。"""
    return json.dumps({
        "title": title,
        "pages": [
            {
                "page_title": f"第{i}节",
                "core_point": f"第{i}页核心：讲清一个具体知识点，配一道例题与解析思路。",
                "keywords": [f"关键词{i}"],
            }
            for i in range(1, n + 1)
        ],
    }, ensure_ascii=False)


def _page_json(tag="要点"):
    """合法精写页：4 条 60 字要点，稳过 220 严线（课堂讲解）。"""
    bullets = [(f"{tag}{j}通过课堂实例验证：配方步骤固定为移项、化一、配方、开方四步，"
                f"学生板演正确率从四成提升到九成，易错点集中在符号处理。")
               for j in range(1, 5)]
    return json.dumps({
        "bullets": bullets,
        "speaker_note": ("同学们，我们先看这一页的核心结论，再逐条过推导步骤，"
                         "每一步都找一位同学复述关键操作，最后留一分钟做随堂练习。" * 2),
    }, ensure_ascii=False)


class _MockLLM:
    """记录调用与 prompt，按脚本返回；脚本元素为 Exception 时抛出。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if not self.script:
            return None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _MockProgress:
    """进度总线打桩：记录全部 emit 文案。"""

    def __init__(self):
        self.events = []

    def emit(self, step, i=None, n=None):
        self.events.append(step)


class CoursewareIntentTest(unittest.TestCase):
    """规则层 + LLM 精修层的课件型判定。"""

    # 1. 触发词：课件/讲课/知识点/例题/教案 等命中即课件型
    def test_rule_courseware_keywords(self):
        for t in ["一元二次方程解法课件", "光合作用知识点讲解",
                  "高一物理例题精讲", "高三数学复习教案", "文言文教学课件",
                  "备课：勾股定理"]:
            self.assertEqual(ppt_maker._rule_intent(t)["content_type"],
                             ppt_maker.COURSEWARE, t)

    # 2. 「给/教…学生…讲」句式命中课件型，受众提取出年级
    def test_rule_courseware_sentence_pattern(self):
        i = ppt_maker._rule_intent("一元二次方程解法课件（给初三学生上课用）")
        self.assertEqual(i["content_type"], ppt_maker.COURSEWARE)
        self.assertEqual(i["audience"], "初三学生")
        self.assertEqual(i["perspective"], "任课老师")
        i2 = ppt_maker._rule_intent("给初三学生讲一元二次方程")
        self.assertEqual(i2["content_type"], ppt_maker.COURSEWARE)
        self.assertEqual(i2["audience"], "初三学生")
        i3 = ppt_maker._rule_intent("教二年级学生认识乘法")
        self.assertEqual(i3["content_type"], ppt_maker.COURSEWARE)
        self.assertEqual(i3["audience"], "二年级学生")

    # 3. 讲话型不被课件型误吞：动员叮嘱维持讲话动员型
    def test_rule_speech_not_swallowed(self):
        i = ppt_maker._rule_intent("开学季（站在班主任的视角，告诉同学们要开学了）")
        self.assertEqual(i["content_type"], ppt_maker.SPEECH)
        self.assertEqual(i["audience"], "同学们")
        self.assertEqual(i["perspective"], "班主任")
        # 「给学生的开学寄语」是讲话不是上课；「教育学生诚信」是德育讲话
        self.assertEqual(ppt_maker._rule_intent("给学生的开学寄语")["content_type"],
                         ppt_maker.SPEECH)
        self.assertEqual(ppt_maker._rule_intent("教育学生诚信的重要性（国旗下讲话）")["content_type"],
                         ppt_maker.SPEECH)

    # 4. 报告型不受影响
    def test_rule_report_unchanged(self):
        i = ppt_maker._rule_intent("全球新能源汽车产业格局")
        self.assertEqual(i["content_type"], ppt_maker.REPORT)

    # 5. 课件型 LLM 精修：采纳合法精修结果（同权）
    def test_parse_intent_courseware_llm_refines(self):
        refine = json.dumps({
            "content_type": "教学课件型",
            "audience": "九年级学生",
            "perspective": "数学老师",
            "purpose": "教会求根公式与配方法",
        }, ensure_ascii=False)
        mock = _MockLLM([refine])
        i = ppt_maker._parse_intent("一元二次方程解法课件", mock)
        self.assertEqual(i["content_type"], ppt_maker.COURSEWARE)
        self.assertEqual(i["audience"], "九年级学生")
        self.assertEqual(i["perspective"], "数学老师")
        self.assertEqual(i["purpose"], "教会求根公式与配方法")
        self.assertEqual(len(mock.calls), 1)          # 课件型也烧这次精修调用

    # 6. LLM 精修可以把课件型纠正为讲话型（合法三值之一即采纳）
    def test_parse_intent_llm_can_override_to_speech(self):
        refine = json.dumps({"content_type": "讲话动员型",
                             "audience": "同学们", "perspective": "班主任",
                             "purpose": "课前动员"}, ensure_ascii=False)
        i = ppt_maker._parse_intent("给同学们讲课前动员几句", _MockLLM([refine]))
        self.assertEqual(i["content_type"], ppt_maker.SPEECH)

    # 7. LLM 失败/垃圾 -> 静默回退规则结果，绝不抛异常
    def test_parse_intent_llm_failure_falls_back(self):
        i = ppt_maker._parse_intent("一元二次方程解法课件",
                                    _MockLLM([RuntimeError("网络炸了")]))
        self.assertEqual(i["content_type"], ppt_maker.COURSEWARE)
        i2 = ppt_maker._parse_intent("一元二次方程解法课件", _MockLLM(["垃圾输出"]))
        self.assertEqual(i2["content_type"], ppt_maker.COURSEWARE)

    # 8. 报告型不烧精修调用（行为不变）
    def test_parse_intent_report_skips_llm(self):
        mock = _MockLLM([])
        i = ppt_maker._parse_intent("全球新能源汽车产业格局", mock)
        self.assertEqual(i["content_type"], ppt_maker.REPORT)
        self.assertEqual(mock.calls, [])

    # 9. 课件型硬约束块：课堂结构 + 版式偏好 + 教师口吻 + 禁市场腔
    def test_intent_block_courseware(self):
        block = ppt_maker._intent_block({
            "content_type": ppt_maker.COURSEWARE,
            "audience": "初三学生", "perspective": "数学老师",
            "purpose": "教会一元二次方程解法"})
        self.assertIn("教学课件型", block)
        self.assertIn("初三学生", block)
        self.assertIn("数学老师", block)
        self.assertIn("导入", block)
        self.assertIn("知识点讲解", block)
        self.assertIn("例题", block)
        self.assertIn("题干", block)
        self.assertIn("小结", block)
        self.assertIn("课后任务", block)
        self.assertIn("process", block)               # 解题步骤版式偏好
        self.assertIn("我们先看", block)              # 教师授课口吻示例
        self.assertIn("市场分析腔", block)            # 禁报告式措辞
        self.assertIn("串题", block)                  # 串题丢弃约束

    # 10. 讲话型/报告型硬约束块行为不变
    def test_intent_block_speech_report_unchanged(self):
        s = ppt_maker._intent_block({
            "content_type": ppt_maker.SPEECH,
            "audience": "同学们", "perspective": "班主任", "purpose": "收心"})
        self.assertIn("讲话动员型", s)
        self.assertNotIn("教学课件型", s)
        r = ppt_maker._intent_block({
            "content_type": ppt_maker.REPORT,
            "audience": "从业者", "perspective": "分析师", "purpose": "辅助决策"})
        self.assertIn("分析报告型", r)
        self.assertNotIn("教学课件型", r)
        self.assertEqual(ppt_maker._intent_block(None), "")


class CoursewarePipelineTest(unittest.TestCase):
    """make_ppt 集成层：研究策略、意图注入、进度埋点。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppt_courseware_test_"))
        self._orig_dir = ppt_maker.FILES_DIR
        ppt_maker.FILES_DIR = self.tmp
        self._orig_render = ppt_maker._render_deck
        ppt_maker._render_deck = None
        self._orig_img_cfg = ppt_maker._load_image_config
        ppt_maker._load_image_config = lambda: None
        self._orig_research = ppt_maker._research_topic
        self._orig_progress = ppt_maker._progress

    def tearDown(self):
        ppt_maker.FILES_DIR = self._orig_dir
        ppt_maker._render_deck = self._orig_render
        ppt_maker._load_image_config = self._orig_img_cfg
        ppt_maker._research_topic = self._orig_research
        ppt_maker._progress = self._orig_progress
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _script(self, n=3, intent_json=None):
        """[意图精修] + 1 大纲 + n 精写 的标准脚本。"""
        head = [intent_json] if intent_json is not None else []
        return head + [_outline_json(n)] + [_page_json(f"P{i}") for i in range(1, n + 1)]

    # 11. 课件型保留联网研究：research_chars>0，研究材料进大纲/精写 prompt
    def test_courseware_keeps_research(self):
        ppt_maker._research_topic = lambda topic, **kw: "求根公式 x = [-b±√(b²-4ac)]/2a（人教版九年级上册）"
        # 意图精修返回垃圾 -> 回退规则（课件型）；然后大纲 + 3 页精写
        mock = _MockLLM(self._script(3, intent_json="垃圾"))
        r = ppt_maker.make_ppt("一元二次方程解法课件（给初三学生上课用）",
                               pages=3, style="课堂讲解", llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(ppt_maker.last_run["intent"]["content_type"],
                         ppt_maker.COURSEWARE)
        self.assertGreater(ppt_maker.last_run["research_chars"], 0)
        # 大纲 prompt 既带研究材料、也带课件型硬约束块
        self.assertIn("真实研究材料", mock.calls[1])
        self.assertIn("教学课件意图", mock.calls[1])
        self.assertIn("真实研究材料", mock.calls[2])   # 精写 prompt 同样带
        self.assertIn("教学课件意图", mock.calls[2])

    # 12. 课件型进度埋点：「明白了：给X年级学生上《课题》」
    def test_courseware_emit_message(self):
        ppt_maker._research_topic = None
        prog = _MockProgress()
        ppt_maker._progress = prog
        mock = _MockLLM(self._script(3, intent_json="垃圾"))
        r = ppt_maker.make_ppt("一元二次方程解法课件（给初三学生上课用）",
                               pages=3, style="课堂讲解", llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertTrue(any("给初三学生上《一元二次方程解法课件》" in e
                            for e in prog.events), prog.events)

    # 13. 讲话型仍跳过研究：research_chars=0，prompt 不带研究段
    def test_speech_still_skips_research(self):
        ppt_maker._research_topic = lambda topic, **kw: "不该被调用的研究材料"
        mock = _MockLLM(self._script(3, intent_json="垃圾"))
        r = ppt_maker.make_ppt("开学季（站在班主任的视角，告诉同学们要开学了）",
                               pages=3, style="课堂讲解", llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(ppt_maker.last_run["intent"]["content_type"],
                         ppt_maker.SPEECH)
        self.assertEqual(ppt_maker.last_run["research_chars"], 0)
        for c in mock.calls:
            self.assertNotIn("真实研究材料", c)

    # 14. 报告型行为不变：研究照常、无意图精修调用
    def test_report_unchanged_pipeline(self):
        ppt_maker._research_topic = lambda topic, **kw: "2025年产销超1600万辆（中汽协）"
        mock = _MockLLM(self._script(3))               # 无意图精修：首个调用即大纲
        r = ppt_maker.make_ppt("全球新能源汽车产业格局", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(ppt_maker.last_run["intent"]["content_type"],
                         ppt_maker.REPORT)
        self.assertGreater(ppt_maker.last_run["research_chars"], 0)
        self.assertIn("真实研究材料", mock.calls[0])   # 第 1 次调用就是大纲
        self.assertEqual(len(mock.calls), 4)           # 1 大纲 + 3 精写


if __name__ == "__main__":
    unittest.main(verbosity=2)
