# -*- coding: utf-8 -*-
"""
speak_filter 纯单测（零 LLM、零 I/O、零服务）。

第一性原理：耳朵带宽是最贵的信道——这里锁死的每一条用例，
都是「绝不允许被念出来」的内容形态。核心用例是真实事故原文：
混合回复（口语 + PowerShell 工具 JSON）必须只留下口语开场白。

运行：python -m unittest jarvis.test_speak_filter -v
（或在 jarvis 目录内 python -m unittest test_speak_filter -v）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import speak_filter  # noqa: E402
from speak_filter import speakable, is_speakable  # noqa: E402


# 事故现场原文（用户真实截图）：口语开场白 + run_shell 工具 JSON（内含
# 转义引号与一大串 PowerShell）。期望：只留口语开场白，JSON/代码一个字不剩。
ACCIDENT_REPLY = (
    "先生，我将用PowerPoint为您制作一份物理主题的演示文稿。"
    "由于涉及创建文件，我先通过命令行生成PPT文件。 "
    '{"tool": "run_shell", "args": {"cmd": "powershell -Command \\"Add-Type '
    '-AssemblyName Microsoft.Office.Interop.PowerPoint; $ppt = New-Object '
    '-ComObject PowerPoint.Application; $pres = $ppt.Presentations.Add(); '
    '$slide = $pres.Slides.Add(1, 1); $slide.Shapes.Title.TextFrame.TextRange.'
    'Text = \\"物理入门\\"; $pres.SaveAs(\\"C:\\\\Users\\\\J1896\\\\physics.pptx\\")\\""}}'
)
ACCIDENT_SPOKEN = (
    "先生，我将用PowerPoint为您制作一份物理主题的演示文稿。"
    "由于涉及创建文件，我先通过命令行生成PPT文件。"
)


class TestAccidentScene(unittest.TestCase):
    """事故原文用例（死契约）：混合回复只留口语，代码绝不上嘴。"""

    def test_accident_reply_keeps_only_colloquial(self):
        out = speakable(ACCIDENT_REPLY)
        self.assertEqual(out, ACCIDENT_SPOKEN)

    def test_accident_reply_contains_no_code(self):
        out = speakable(ACCIDENT_REPLY)
        for banned in ("{", "}", '"tool"', "powershell", "PowerShell",
                       "Add-Type", "run_shell", "C:\\", "physics.pptx"):
            self.assertNotIn(banned, out, "泄漏了不可念内容：%s" % banned)

    def test_accident_reply_is_speakable_after_filter(self):
        self.assertTrue(is_speakable(ACCIDENT_REPLY))


class TestPureUnspeakable(unittest.TestCase):
    """纯不可念内容：过滤后必须为空串（调用方用通用话术兜底）。"""

    def test_pure_tool_json(self):
        self.assertEqual("", speakable('{"tool": "write_file", "args": {"name": "a.txt", "content": "x"}}'))

    def test_pure_json_array(self):
        self.assertEqual("", speakable('[{"step": 1}, {"step": 2}]'))

    def test_pure_code_fence(self):
        text = "```python\nimport os\nprint(os.listdir())\n```"
        self.assertEqual("", speakable(text))

    def test_pure_json_fence(self):
        text = '```json\n{"tool": "run_shell", "args": {"cmd": "dir"}}\n```'
        self.assertEqual("", speakable(text))

    def test_pure_shell_line(self):
        self.assertEqual("", speakable("powershell -Command Get-ChildItem"))

    def test_pure_base64(self):
        self.assertEqual("", speakable("aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgYmFzZTY0IHN0cmluZw=="))

    def test_tiny_remnant_below_threshold(self):
        # 剥离后只剩「好的。」两个有效字，不足 4 个，视为没啥可念
        text = "好的。```\nx = 1\n```"
        self.assertEqual("", speakable(text))

    def test_empty_and_none(self):
        self.assertEqual("", speakable(""))
        self.assertEqual("", speakable("   \n  "))
        self.assertEqual("", speakable(None))


class TestNormalChat(unittest.TestCase):
    """正常闲聊：原样通过，一个标点都不许动。"""

    def test_plain_chat_unchanged(self):
        text = "先生，现在是下午两点三十分，今天天气不错。"
        self.assertEqual(text, speakable(text))

    def test_chat_with_english_unchanged(self):
        text = "先生，PowerPoint 已经为您打开了，请讲下一步。"
        self.assertEqual(text, speakable(text))

    def test_file_name_kept(self):
        # 文件名（日报.txt）不是路径也不是代码，是可念内容，必须保留
        text = "先生，总结已经写到文件柜「日报.txt」了，请过目。"
        self.assertEqual(text, speakable(text))

    def test_is_speakable_true(self):
        self.assertTrue(is_speakable("先生，您好，有什么可以为您效劳的？"))
        self.assertFalse(is_speakable('{"tool": "x", "args": {}}'))


class TestUrlPathStrip(unittest.TestCase):
    """URL / Windows 路径 / 仓库相对路径剥离。"""

    def test_url_stripped(self):
        out = speakable("我查到了结果，详见 https://example.com/a?b=c 这里还有补充说明。")
        self.assertNotIn("http", out)
        self.assertIn("我查到了结果", out)
        self.assertIn("补充说明", out)

    def test_www_url_stripped(self):
        out = speakable("入口在 www.example.com 上，先生可以稍后查看。")
        self.assertNotIn("www.example.com", out)

    def test_windows_path_stripped(self):
        out = speakable("文件已保存到 C:\\Users\\J1896\\Documents\\日报.txt 请查收。")
        self.assertNotIn("C:\\", out)
        self.assertIn("文件已保存到", out)
        self.assertIn("请查收", out)

    def test_repo_relative_path_stripped(self):
        out = speakable("配置在 jarvis\\llm_config.json 里，先生可以改。")
        self.assertNotIn("jarvis\\", out)

    def test_mixed_code_and_chat(self):
        text = "先生，命令是 `dir /b`，已经在终端跑完了，结果在屏幕上。"
        out = speakable(text)
        self.assertNotIn("dir /b", out)
        self.assertIn("已经在终端跑完了", out)


class TestTruncation(unittest.TestCase):
    """长回复截断：念重点，细节请看屏幕。"""

    def test_long_reply_truncated_with_tail(self):
        body = "先生，这是第%d条要点，内容非常详实。"
        long_text = "".join(body % i for i in range(30))  # 远超 200 字
        out = speakable(long_text)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("详细内容我放在屏幕上了。"))

    def test_short_reply_not_truncated(self):
        text = "先生，这句话不到两百字，应该原样保留不截断。"
        self.assertEqual(text, speakable(text))

    def test_no_truncation_when_disabled(self):
        long_text = "先生，这是第%d条要点，内容非常详实。" * 30
        out = speakable(long_text, max_chars=None)
        self.assertGreater(len(out), 200)
        self.assertNotIn("详细内容我放在屏幕上了", out)


class TestFenceVariants(unittest.TestCase):
    """围栏形态变体：未闭合围栏、~~~ 围栏、围栏内工具 JSON。"""

    def test_unclosed_fence_amputated(self):
        text = "先生，请看这段代码：```python\nimport os\nos.getcwd()"
        out = speakable(text)
        self.assertNotIn("import", out)
        self.assertIn("先生，请看这段代码", out)

    def test_tilde_fence(self):
        text = "前面的部分保留。~~~\nsome code here\n~~~"
        out = speakable(text)
        self.assertNotIn("some code", out)
        self.assertIn("前面的部分保留", out)

    def test_json_in_fence_with_chat(self):
        text = ('先生，我来写入文件。\n```json\n'
                '{"tool": "write_file", "args": {"name": "日记.txt", "content": "开心"}}\n```')
        out = speakable(text)
        self.assertEqual("先生，我来写入文件。", out)


class TestMalformedToolJson(unittest.TestCase):
    """残缺工具 JSON（花括号不闭合 / 截断）：截肢后只留口语。"""

    def test_truncated_tool_json(self):
        text = ('先生，我先执行命令。{"tool": "run_shell", "args": {"cmd": '
                '"powershell -Command \\"Add-Type -AssemblyName')
        out = speakable(text)
        self.assertEqual("先生，我先执行命令。", out)

    def test_single_quote_tool_json(self):
        text = "先生，我来试试。{'tool': 'run_shell', 'args': {'cmd': 'dir'}}"
        out = speakable(text)
        self.assertNotIn("tool", out)
        self.assertIn("先生，我来试试。", out)


if __name__ == "__main__":
    unittest.main()
