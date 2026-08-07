# -*- coding: utf-8 -*-
"""
ppt_maker 两阶段 pipeline 单元测试：纯 mock，LLM 0 真实调用。
运行：python -m unittest test_ppt_maker -v   （在 jarvis/ 目录下）
落盘目录重定向到临时目录，不污染真实 files/。

mock 脚本约定：script 列表按调用顺序出队；元素是 str 则返回，是 Exception 则抛出。
调用顺序 = 1 次大纲 + 每页 1~3 次精写（重写才会追加）。
"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppt_maker
from pptx import Presentation


# ---------------------------------------------------------------- mock 素材

def _outline_json(n=4, title="人工智能简介"):
    """合法大纲：n 页，每页 page_title/core_point/keywords 齐全。"""
    return json.dumps({
        "title": title,
        "pages": [
            {
                "page_title": f"章节{i}",
                "core_point": f"第{i}页核心论点：该环节的关键机制决定了整体效果，必须讲清原理与数据。",
                "keywords": [f"关键词{i}甲", f"关键词{i}乙"],
            }
            for i in range(1, n + 1)
        ],
    }, ensure_ascii=False)


def _long_bullet(tag):
    """60 字达标要点（30-60 字区间）。"""
    return (f"{tag}通过真实场景验证：头部试点项目把处理周期从三天压缩到四小时，"
            f"错误率下降约六成，验证了该机制在量产环境中的可行性与收益。")


def _page_json(tag="要点", n_bullets=4, bullet_len="long", note=True):
    """合法精写页：默认 4 条 60 字要点（总 240 字，过 220 严线）。"""
    if bullet_len == "long":
        bullets = [_long_bullet(f"{tag}{j}") for j in range(1, n_bullets + 1)]
    else:
        bullets = [f"{tag}{j}短句。" for j in range(1, n_bullets + 1)]
    data = {"bullets": bullets}
    if note:
        data["speaker_note"] = (
            "这一页先抛出听众最关心的问题，再逐条拆解机制与数据，"
            "每条讲完停顿半拍确认大家跟上，最后一句小结自然过渡到下一页。" * 2)
    return json.dumps(data, ensure_ascii=False)


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


def _script_for(n_pages, page_json=None):
    """1 次大纲 + 每页 1 次精写 的标准脚本。"""
    return [_outline_json(n_pages)] + [page_json or _page_json(f"P{i}") for i in range(1, n_pages + 1)]


def _body_text(prs, slide_idx):
    """抽某页正文文本框的全部文字。"""
    return "\n".join(
        sh.text_frame.text for sh in prs.slides[slide_idx].shapes if sh.has_text_frame)


# ---------------------------------------------------------------- 测试

class PptMakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppt_maker_test_"))
        self._orig_dir = ppt_maker.FILES_DIR
        ppt_maker.FILES_DIR = self.tmp

    def tearDown(self):
        ppt_maker.FILES_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # 1. 标准两阶段 -> 真 pptx：页数、标题、notes 非空、调用次数 = 1 + N
    def test_standard_pipeline_builds_real_pptx(self):
        mock = _MockLLM(_script_for(4))
        r = ppt_maker.make_ppt("人工智能简介", pages=4, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["pages"], 5)               # 4 内容页 + 封面
        self.assertEqual(r["title"], "人工智能简介")
        self.assertTrue(r["file_name"].endswith(".pptx"))
        path = Path(r["path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.tmp)       # 落在（重定向后的）files 目录

        prs = Presentation(str(path))
        self.assertEqual(len(list(prs.slides)), 5)
        cover_text = _body_text(prs, 0)
        self.assertIn("人工智能简介", cover_text)
        for i, slide in enumerate(prs.slides):
            self.assertTrue(slide.has_notes_slide, f"slide {i} 缺 notes_slide")
            self.assertTrue(slide.notes_slide.notes_text_frame.text.strip(),
                            f"slide {i} notes 为空")
        self.assertAlmostEqual(prs.slide_width / prs.slide_height, 16 / 9, places=2)
        self.assertEqual(len(mock.calls), 5)          # 1 大纲 + 4 精写
        # 运行统计：每页 4 要点、240 字、0 重写
        self.assertEqual(len(ppt_maker.last_run["page_stats"]), 4)
        for st in ppt_maker.last_run["page_stats"]:
            self.assertEqual(st["bullets"], 4)
            self.assertGreaterEqual(st["chars"], 220)
            self.assertEqual(st["rewrites"], 0)
            self.assertFalse(st["fallback"])

    # 2. 大纲 JSON 带围栏/废话 -> 仍能解析
    def test_fenced_outline_parses(self):
        fenced = "好的，这是大纲：\n```json\n" + _outline_json(3) + "\n```\n希望对你有帮助。"
        mock = _MockLLM([fenced] + [_page_json(f"P{i}") for i in range(1, 4)])
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["pages"], 4)

    # 3. 大纲阶段垃圾 -> 修复重试 1 次仍垃圾 -> ok=False，不造文件
    def test_outline_garbage_fails_clean(self):
        mock = _MockLLM(["这不是JSON，随便聊聊吧", "还是垃圾 {{{"])
        r = ppt_maker.make_ppt("人工智能简介", llm_caller=mock)
        self.assertFalse(r["ok"])
        self.assertIn("JSON", r["error"])
        self.assertEqual(len(mock.calls), 2)          # 恰好修复重试一次
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 3b. 大纲阶段 LLM 无响应（None）-> ok=False，不造空壳
    def test_none_outline_fails_clean(self):
        r = ppt_maker.make_ppt("人工智能简介", llm_caller=_MockLLM([None, None]))
        self.assertFalse(r["ok"])
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 3c. 大纲首解失败、修复重试成功 -> ok
    def test_outline_retry_succeeds(self):
        mock = _MockLLM(["垃圾输出", _outline_json(3)]
                        + [_page_json(f"P{i}") for i in range(1, 4)])
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(mock.calls), 2 + 3)      # 大纲 2 次 + 3 页各 1 次

    # 4. 某页精写调用异常 -> 兜底页顶上，页数完整，整单不失败
    def test_page_exception_uses_fallback(self):
        script = [_outline_json(4),
                  _page_json("P1"),
                  RuntimeError("网络抖动"),           # 第 2 页精写炸掉
                  _page_json("P3"),
                  _page_json("P4")]
        mock = _MockLLM(script)
        r = ppt_maker.make_ppt("人工智能简介", pages=4, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["pages"], 5)               # 页数不缺
        prs = Presentation(str(path := Path(r["path"])))
        self.assertEqual(len(list(prs.slides)), 5)
        # 兜底页（物理第 3 页 = 封面 + 内容第 2 页）含大纲 core_point 内容
        body = _body_text(prs, 2)
        self.assertIn("核心论点", body)
        # 兜底页 notes 也非空
        self.assertTrue(prs.slides[2].notes_slide.notes_text_frame.text.strip())
        st = ppt_maker.last_run["page_stats"][1]
        self.assertTrue(st["fallback"])

    # 4b. 某页精写连吐垃圾（非 JSON）-> 3 次尝试耗尽后兜底页顶上
    def test_page_garbage_falls_back_after_attempts(self):
        script = [_outline_json(3),
                  "垃圾一", "垃圾二", "垃圾三",       # 第 1 页 3 次全废
                  _page_json("P2"),
                  _page_json("P3")]
        mock = _MockLLM(script)
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["pages"], 4)
        self.assertTrue(ppt_maker.last_run["page_stats"][0]["fallback"])
        self.assertEqual(len(mock.calls), 1 + 3 + 2)  # 大纲 + 第1页3次 + 后两页

    # 5. 质量闸触发重写：第一次 2 要点共 30 字，第二次给足 -> 用达标版
    def test_quality_gate_triggers_rewrite(self):
        thin = _page_json("薄", n_bullets=2, bullet_len="short")  # 2 条共约 12 字
        good = _page_json("厚")
        script = [_outline_json(3), thin, good, _page_json("P2"), _page_json("P3")]
        mock = _MockLLM(script)
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        # 重写真实发生：第 1 页调了 2 次（总 1 + 2 + 1 + 1 = 5 次）
        self.assertEqual(len(mock.calls), 5)
        self.assertEqual(ppt_maker.last_run["page_stats"][0]["rewrites"], 1)
        # 最终用的是达标版（厚版标记在物理第 2 页正文里，薄版不在）
        prs = Presentation(r["path"])
        body = _body_text(prs, 1)
        self.assertIn("厚1通过真实场景验证", body)
        self.assertNotIn("薄1", body)

    # 6. 重写 2 次仍不达标 -> 取历次最好（字数最多者），页仍生成
    def test_best_of_failed_rewrites(self):
        # 三版都只有 3 条（过不了要点数闸），但字数递增：第三版最好
        v1 = _page_json("甲", n_bullets=3, bullet_len="short")
        v2 = json.dumps({"bullets": [f"乙{j}这是一条中等长度的要点，带一些解释但不够充分。"
                                     for j in range(1, 4)],
                         "speaker_note": "备注。" * 30}, ensure_ascii=False)
        v3 = _page_json("丙", n_bullets=3, bullet_len="long")     # 3×60=180 字，仍差
        script = [_outline_json(3), v1, v2, v3, _page_json("P2"), _page_json("P3")]
        mock = _MockLLM(script)
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(mock.calls), 1 + 3 + 2)  # 第 1 页打满 3 次
        st = ppt_maker.last_run["page_stats"][0]
        self.assertEqual(st["rewrites"], 2)
        self.assertFalse(st["fallback"])
        # 最终采用字数最多的 v3（丙版）
        prs = Presentation(r["path"])
        body = _body_text(prs, 1)
        self.assertIn("丙1通过真实场景验证", body)
        self.assertNotIn("甲1", body)
        self.assertNotIn("乙1", body)

    # 7. 页数钳制 3-20（大纲 prompt 中的请求页数为证）
    def test_pages_clamped(self):
        mock_lo = _MockLLM(_script_for(3))
        r = ppt_maker.make_ppt("t", pages=1, llm_caller=mock_lo)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertIn("3 页", mock_lo.calls[0])       # 1 -> 钳到 3

        mock_hi = _MockLLM(_script_for(20))
        r = ppt_maker.make_ppt("t", pages=99, llm_caller=mock_hi)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertIn("20 页", mock_hi.calls[0])      # 99 -> 钳到 20
        self.assertEqual(r["pages"], 21)              # 20 内容页 + 封面

    # 8. 文件名安全性：非法字符被清洗，Windows 合法
    def test_filename_sanitized(self):
        nasty = '人工智能/简介:<>*?"| 版'
        r = ppt_maker.make_ppt(nasty, pages=3,
                               llm_caller=_MockLLM(_script_for(3)))
        self.assertTrue(r["ok"], r.get("error"))
        name = r["file_name"]
        self.assertNotRegex(name, r'[\\/:*?"<>|]')
        self.assertRegex(name, r"^[\w一-鿿-]+_\d{8}-\d{4}(-\d+)?\.pptx$")
        self.assertIn("人工智能", name)
        self.assertIn("简介", name)

    # 8b. 空主题 -> ok=False
    def test_empty_topic_fails(self):
        r = ppt_maker.make_ppt("   ", llm_caller=_MockLLM([]))
        self.assertFalse(r["ok"])

    # 8c. 大纲缺 pages 结构 -> ok=False
    def test_missing_outline_pages_fails(self):
        bad = json.dumps({"title": "只有标题没有页"}, ensure_ascii=False)
        r = ppt_maker.make_ppt("t", llm_caller=_MockLLM([bad, bad]))
        self.assertFalse(r["ok"])
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 9. 精写页缺 speaker_note -> 兜底合成，notes 仍非空
    def test_missing_note_synthesized(self):
        script = [_outline_json(3)] + [_page_json(f"P{i}", note=False) for i in range(1, 4)]
        r = ppt_maker.make_ppt("t", pages=3, llm_caller=_MockLLM(script))
        self.assertTrue(r["ok"], r.get("error"))
        prs = Presentation(r["path"])
        for slide in prs.slides:
            self.assertTrue(slide.notes_slide.notes_text_frame.text.strip())

    # 10. 高密度内容触发字号自适应（不溢出：三档 18/16/14）
    def test_dense_content_shrinks_font(self):
        from pptx.util import Pt
        dense = _page_json("密", n_bullets=6, bullet_len="long")   # 6×60=360 字
        script = [_outline_json(3), dense, _page_json("P2"), _page_json("P3")]
        r = ppt_maker.make_ppt("t", pages=3, llm_caller=_MockLLM(script))
        self.assertTrue(r["ok"], r.get("error"))
        prs = Presentation(r["path"])
        # 第 1 内容页（物理 idx=1）：360 字应缩到 14pt 档
        sizes = set()
        for sh in prs.slides[1].shapes:
            if sh.has_text_frame:
                for p in sh.text_frame.paragraphs:
                    for run in p.runs:
                        if run.text.startswith("•"):
                            sizes.add(run.font.size)
        self.assertEqual(sizes, {Pt(14)})
        # 第 2 内容页 240 字保持 16pt 档
        sizes2 = set()
        for sh in prs.slides[2].shapes:
            if sh.has_text_frame:
                for p in sh.text_frame.paragraphs:
                    for run in p.runs:
                        if run.text.startswith("•"):
                            sizes2.add(run.font.size)
        self.assertEqual(sizes2, {Pt(16)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
