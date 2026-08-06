# -*- coding: utf-8 -*-
"""
ppt_maker 单元测试：纯 mock，LLM 0 真实调用。
运行：python -m unittest test_ppt_maker -v   （在 jarvis/ 目录下）
落盘目录重定向到临时目录，不污染真实 files/。
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


def _slides(n):
    return [
        {
            "heading": f"章节{i}",
            "bullets": [f"要点{i}-{j}" for j in range(1, 5)],
            "speaker_notes": f"第{i}页讲章节{i}，先抛问题再逐条展开，建议用时2分钟。" * 3,
        }
        for i in range(1, n + 1)
    ]


def _good_json(n=4):
    return json.dumps({"title": "人工智能简介", "slides": _slides(n)}, ensure_ascii=False)


class _MockLLM:
    """记录调用次数与 prompt，按脚本返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.script.pop(0) if self.script else None


class PptMakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ppt_maker_test_"))
        self._orig_dir = ppt_maker.FILES_DIR
        ppt_maker.FILES_DIR = self.tmp

    def tearDown(self):
        ppt_maker.FILES_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # 1. 标准 JSON -> 真 pptx：页数、标题、每页 notes 非空
    def test_standard_json_builds_real_pptx(self):
        mock = _MockLLM([_good_json(4)])
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
        # 封面含标题
        cover_text = "\n".join(
            sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame)
        self.assertIn("人工智能简介", cover_text)
        # 每页 notes 存在且非空
        for i, slide in enumerate(prs.slides):
            self.assertTrue(slide.has_notes_slide, f"slide {i} 缺 notes_slide")
            notes = slide.notes_slide.notes_text_frame.text.strip()
            self.assertTrue(notes, f"slide {i} notes 为空")
        # 16:9
        self.assertAlmostEqual(prs.slide_width / prs.slide_height, 16 / 9, places=2)
        self.assertEqual(len(mock.calls), 1)

    # 2. markdown 围栏包裹的 JSON -> 仍能解析
    def test_fenced_json_parses(self):
        fenced = "好的，这是大纲：\n```json\n" + _good_json(3) + "\n```\n希望对你有帮助。"
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=_MockLLM([fenced]))
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["pages"], 4)
        prs = Presentation(r["path"])
        for slide in prs.slides:
            self.assertTrue(slide.notes_slide.notes_text_frame.text.strip())

    # 3. LLM 返回垃圾 -> 重试一次仍垃圾 -> ok=False，不造文件
    def test_garbage_json_fails_clean(self):
        mock = _MockLLM(["这不是JSON，随便聊聊吧", "还是垃圾 {{{"])
        r = ppt_maker.make_ppt("人工智能简介", llm_caller=mock)
        self.assertFalse(r["ok"])
        self.assertIn("JSON", r["error"])
        self.assertEqual(len(mock.calls), 2)          # 恰好重试一次
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 3b. LLM 无响应（None）-> ok=False，零 LLM 不造空壳
    def test_none_llm_fails_clean(self):
        r = ppt_maker.make_ppt("人工智能简介", llm_caller=_MockLLM([None]))
        self.assertFalse(r["ok"])
        self.assertIn("无响应", r["error"])
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 3c. 垃圾后重试成功 -> ok
    def test_retry_succeeds(self):
        mock = _MockLLM(["垃圾输出", _good_json(3)])
        r = ppt_maker.make_ppt("人工智能简介", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(len(mock.calls), 2)

    # 4. 页数钳制 3-20
    def test_pages_clamped(self):
        mock_lo = _MockLLM([_good_json(3)])
        r = ppt_maker.make_ppt("t", pages=1, llm_caller=mock_lo)
        self.assertTrue(r["ok"])
        self.assertIn("3 页", mock_lo.calls[0])       # 1 -> 钳到 3

        mock_hi = _MockLLM([_good_json(25)])
        r = ppt_maker.make_ppt("t", pages=99, llm_caller=mock_hi)
        self.assertTrue(r["ok"])
        self.assertIn("20 页", mock_hi.calls[0])      # 99 -> 钳到 20
        self.assertEqual(r["pages"], 21)              # 25 页大纲截到 20 + 封面

    # 5. 文件名安全性：非法字符被清洗，Windows 合法
    def test_filename_sanitized(self):
        nasty = '人工智能/简介:<>*?"| 版'
        r = ppt_maker.make_ppt(nasty, pages=3, llm_caller=_MockLLM([_good_json(3)]))
        self.assertTrue(r["ok"], r.get("error"))
        name = r["file_name"]
        self.assertNotRegex(name, r'[\\/:*?"<>|]')
        self.assertRegex(name, r"^[\w一-鿿-]+_\d{8}-\d{4}(-\d+)?\.pptx$")
        self.assertIn("人工智能", name)
        self.assertIn("简介", name)

    # 5b. 空主题 / 缺结构 -> ok=False
    def test_empty_topic_fails(self):
        r = ppt_maker.make_ppt("   ", llm_caller=_MockLLM([_good_json(3)]))
        self.assertFalse(r["ok"])

    def test_missing_slides_fails(self):
        bad = json.dumps({"title": "只有标题没有页"}, ensure_ascii=False)
        r = ppt_maker.make_ppt("t", llm_caller=_MockLLM([bad, bad]))
        self.assertFalse(r["ok"])
        self.assertEqual(list(self.tmp.glob("*.pptx")), [])

    # 5c. notes 缺失时兜底合成，仍非空
    def test_missing_notes_synthesized(self):
        data = {"title": "T", "slides": [{"heading": "H", "bullets": ["a", "b"]}]}
        mock = _MockLLM([json.dumps(data, ensure_ascii=False)])
        r = ppt_maker.make_ppt("t", pages=3, llm_caller=mock)
        self.assertTrue(r["ok"], r.get("error"))
        prs = Presentation(r["path"])
        for slide in prs.slides:
            self.assertTrue(slide.notes_slide.notes_text_frame.text.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
