# -*- coding: utf-8 -*-
"""
brain 工具 JSON 泄漏兜底单测（mock LLM，真实调用 0 次，零网络、零服务）。

死契约：回复里检测到工具 JSON 但解析/执行失败时，think() 返回值
必须剥离 JSON 和代码、只留口语部分；口语为空用通用话术兜底——
原始 JSON/代码永远不许成为 think() 的返回值。本文件锁住这条契约，
用例原型是真实事故截图（混合回复 + PowerShell 工具 JSON 被念出来）。

运行：python -m unittest jarvis.test_brain_leak -v
（或在 jarvis 目录内 python -m unittest test_brain_leak -v）
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402

# 事故原型输入（真实用户截图原话）
ACCIDENT_INPUT = "你帮我做一个有关物理的PPT，直接用PowerPoint软件"

# 事故原型：口语开场白 + 合法工具 JSON（内含转义引号与 PowerShell）
MIXED_VALID = (
    "先生，我将用PowerPoint为您制作一份物理主题的演示文稿。"
    "由于涉及创建文件，我先通过命令行生成PPT文件。\n"
    '{"tool": "run_shell", "args": {"cmd": "powershell -Command \\"Add-Type '
    '-AssemblyName Microsoft.Office.Interop.PowerPoint\\""}}'
)

# 残缺版：LLM 输出被截断，花括号不闭合，三种提取策略都必须失败
MIXED_BROKEN = (
    "先生，我将用PowerPoint为您制作一份物理主题的演示文稿。"
    "由于涉及创建文件，我先通过命令行生成PPT文件。\n"
    '{"tool": "run_shell", "args": {"cmd": "powershell -Command \\"Add-Type '
    '-AssemblyName Microsoft.Office.Interop.PowerPoint; $ppt = New-Object'
)

# markdown 围栏包裹的工具 JSON
FENCED_JSON = (
    "好的先生，马上执行。\n```json\n"
    '{"tool": "write_file", "args": {"name": "物理大纲.txt", "content": "力学、光学、电磁学"}}\n'
    "```"
)

_FAKE_CFG = {"api_key": "fake-key", "base_url": "http://127.0.0.1:9",
             "model": "fake-model", "extra_body": ""}

# 断言用：这些字符串出现在 think 返回值里就是泄漏
_BANNED = ('"tool"', "powershell", "PowerShell", "{", "}", "run_shell", "Add-Type")


class _FakeHands:
    """假手：记录调用，绝不执行任何真实动作。"""

    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [{"name": "run_shell", "args": {"cmd": "命令"},
                 "description": "执行命令"},
                {"name": "write_file", "args": {"name": "文件名", "content": "内容"},
                 "description": "写文件"}]

    def execute(self, tool, args):
        self.calls.append((tool, args))
        return "好的先生，命令已经执行完成了。"


class _BrainLeakTestBase(unittest.TestCase):
    """公共隔离：切断记忆/提醒/触发/授权等旁路模块，mock 掉 LLM 配置。"""

    def setUp(self):
        self._patches = [
            mock.patch.object(brain, "memory", None),
            mock.patch.object(brain, "memory_v2", None),
            mock.patch.object(brain, "episodic", None),
            mock.patch.object(brain, "reminders", None),
            mock.patch.object(brain, "triggers", None),
            mock.patch.object(brain, "auth_policy", None),
            mock.patch.object(brain, "_load_llm_config", return_value=dict(_FAKE_CFG)),
        ]
        for p in self._patches:
            p.start()
        brain._pending_shell = None  # 别被其他测试遗留的待确认状态污染

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        brain._pending_shell = None


class TestBrokenToolJsonLeak(_BrainLeakTestBase):
    """场景一（事故核心）：工具 JSON 残缺解析失败，重发 4 轮仍残缺——
    最终返回值必须只留口语开场白，JSON/PowerShell 一个字都不许出现。"""

    def test_broken_json_stripped_after_budget(self):
        fake_hands = _FakeHands()
        with mock.patch.object(brain, "hands", fake_hands), \
                mock.patch.object(brain, "_request_llm",
                                  return_value=MIXED_BROKEN) as llm:
            reply = brain.think(ACCIDENT_INPUT, [])
        # 自愈机制确实重发了（1 次首发 + 3 次纠正 = 4 轮预算耗尽）
        self.assertEqual(4, llm.call_count)
        # 工具从未被执行（解析不出来的指令不许瞎跑）
        self.assertEqual([], fake_hands.calls)
        # 死契约：只留口语开场白
        self.assertEqual(
            "先生，我将用PowerPoint为您制作一份物理主题的演示文稿。"
            "由于涉及创建文件，我先通过命令行生成PPT文件。",
            reply)
        for banned in _BANNED:
            self.assertNotIn(banned, reply, "泄漏了不可念内容：%s" % banned)

    def test_pure_broken_json_falls_back_to_generic(self):
        # 连口语开场白都没有的纯残缺 JSON：返回通用兜底话术
        with mock.patch.object(brain, "hands", _FakeHands()), \
                mock.patch.object(brain, "_request_llm",
                                  return_value='{"tool": "run_shell", "args": {"cmd": "powershell -Command'):
            reply = brain.think(ACCIDENT_INPUT, [])
        self.assertEqual("先生，我在处理这个任务，请稍候看结果。", reply)
        for banned in _BANNED:
            self.assertNotIn(banned, reply, "泄漏了不可念内容：%s" % banned)

    def test_hands_none_valid_json_also_contained(self):
        # hands 模块掉线时工具 JSON 无从执行：同样不许原样泄漏给先生
        with mock.patch.object(brain, "hands", None), \
                mock.patch.object(brain, "_request_llm", return_value=MIXED_VALID):
            reply = brain.think(ACCIDENT_INPUT, [])
        for banned in _BANNED:
            self.assertNotIn(banned, reply, "泄漏了不可念内容：%s" % banned)
        self.assertIn("先生，我将用PowerPoint为您制作一份物理主题的演示文稿", reply)


class TestMixedReplyExtraction(_BrainLeakTestBase):
    """场景二：混合/围栏回复中的合法工具 JSON 要被救回来执行，
    而不是整个当文本念出来——强化提取器的正向用例。"""

    def test_mixed_text_plus_json_executes(self):
        fake_hands = _FakeHands()
        final_words = "先生，物理演示文稿已经生成完毕，放在您的文档目录里了。"
        with mock.patch.object(brain, "hands", fake_hands), \
                mock.patch.object(brain, "_request_llm",
                                  side_effect=[MIXED_VALID, final_words]):
            reply = brain.think(ACCIDENT_INPUT, [])
        # 前置口语文本 + 跨行 JSON：被提取执行，没有当文本播报
        self.assertEqual([("run_shell", mock.ANY)], fake_hands.calls)
        # T1 主动交代会追加汇报行：主体回复不变，交代随行
        self.assertTrue(reply.startswith(final_words), reply)
        self.assertIn("交代", reply)

    def test_fenced_json_executes(self):
        fake_hands = _FakeHands()
        final_words = "先生，物理大纲已经写好了，请您过目。"
        with mock.patch.object(brain, "hands", fake_hands), \
                mock.patch.object(brain, "_request_llm",
                                  side_effect=[FENCED_JSON, final_words]):
            reply = brain.think("请你开始制作物理课件的准备工作", [])
        self.assertEqual([("write_file", mock.ANY)], fake_hands.calls)
        self.assertEqual("物理大纲.txt", fake_hands.calls[0][1]["name"])
        # T1 主动交代会追加汇报行：主体回复不变，交代随行
        self.assertTrue(reply.startswith(final_words), reply)
        self.assertIn("交代", reply)

    def test_multiline_escaped_json_executes(self):
        # 跨行 + 转义引号 + 字符串内含 \n 转义
        tricky = (
            '前言：\n{\n  "tool": "write_file",\n  "args": {\n'
            '    "name": "笔记.txt",\n'
            '    "content": "第一行\\n第二行 \\"引用\\""\n  }\n}'
        )
        fake_hands = _FakeHands()
        with mock.patch.object(brain, "hands", fake_hands), \
                mock.patch.object(brain, "_request_llm",
                                  side_effect=[tricky, "先生，笔记写好了。"]):
            reply = brain.think("帮我把物理课件的笔记整理一下", [])
        self.assertEqual([("write_file", mock.ANY)], fake_hands.calls)
        self.assertIn("第一行\n第二行", fake_hands.calls[0][1]["content"])
        # T1 主动交代会追加汇报行：主体回复不变，交代随行
        self.assertTrue(reply.startswith("先生，笔记写好了。"), reply)
        self.assertIn("交代", reply)


class TestSpeakGuardAtExit(_BrainLeakTestBase):
    """场景三：think() 出口闸——任何返回路径都不许带出代码/JSON。"""

    def test_guard_strips_json_from_any_reply(self):
        with mock.patch.object(brain, "_think_impl",
                               return_value='先生，结果如下。{"data": [1, 2, 3]} 请查收。'):
            reply = brain.think("随便说点什么", [])
        self.assertNotIn("{", reply)
        self.assertIn("先生，结果如下。", reply)
        self.assertIn("请查收。", reply)

    def test_guard_generic_fallback_on_pure_code(self):
        with mock.patch.object(brain, "_think_impl",
                               return_value="```python\nimport os\nos.system('x')\n```"):
            reply = brain.think("随便说点什么", [])
        self.assertEqual("先生，我在处理这个任务，请稍候看结果。", reply)

    def test_guard_passes_plain_chat_untouched(self):
        plain = "先生，牛顿第一定律说的是：一切物体在不受外力时保持静止或匀速直线运动。"
        with mock.patch.object(brain, "_think_impl", return_value=plain):
            reply = brain.think("什么是牛顿第一定律", [])
        self.assertEqual(plain, reply)

    def test_exit_signal_passthrough(self):
        self.assertEqual("__EXIT__", brain.think("再见", []))

    def test_llm_plain_text_via_loop_untouched(self):
        plain = "先生，PPT 的事我来安排，请您稍等片刻。"
        with mock.patch.object(brain, "hands", _FakeHands()), \
                mock.patch.object(brain, "_request_llm", return_value=plain):
            reply = brain.think(ACCIDENT_INPUT, [])
        self.assertEqual(plain, reply)


if __name__ == "__main__":
    unittest.main()
