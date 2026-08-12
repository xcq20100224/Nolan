# -*- coding: utf-8 -*-
"""
debrief 主动交代 + progress.run_steps 单元测试。

覆盖：
  - make_ppt 成功（有步骤 → 含链路与文件名；无步骤 → 兜底概括句）
  - make_ppt 失败 / 非文本结果 / 待确认 → None
  - edit_ppt / write_file / wechat_send_file / run_shell / gui_control 成功
  - 非长任务工具（get_time）→ None
  - run_steps 独立语义：begin 重置 / emit 追加 / 副本隔离 / 无订阅零开销

纯内存，零网络零 LLM。运行：python test_debrief.py（在 jarvis/ 目录下）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import debrief
import progress

# 与 hands._make_ppt 成功话术同构的样本
_PPT_OK = (
    "好的先生，PPT 已经做好并放进文件柜了：《季度汇报》，"
    "文件名 季度汇报.pptx，共 10 页，"
    "每一页的备注栏里都写好了演讲稿，您照着讲就行。"
)
_PPT_STEPS = ["正在联网查资料…", "正在设计大纲…", "正在精写第 1/3 页：标题碎片示例",
              "正在生成配图 1/2：封面", "正在排版渲染…", "PPT 做好了，存档完成"]


class _Base(unittest.TestCase):
    """每个用例前后都把进度总线收干净，防串味。"""

    def tearDown(self):
        progress.end()
        progress.drain()
        progress.begin()   # begin 重置步骤流水
        progress.end()


class TestRunSteps(_Base):
    """progress.run_steps 独立语义。"""

    def test_begin_resets_steps(self):
        progress.begin()
        progress.emit("旧步骤")
        progress.end()
        progress.begin()  # 新一轮：旧步骤被重置
        self.assertEqual(progress.run_steps(), [])

    def test_emit_appends_step_texts_in_order(self):
        progress.begin()
        progress.emit("第一步")
        progress.emit("第二步", i=2, n=5)
        progress.emit("第三步")
        self.assertEqual(progress.run_steps(), ["第一步", "第二步", "第三步"])

    def test_run_steps_returns_copy(self):
        progress.begin()
        progress.emit("步骤甲")
        steps = progress.run_steps()
        steps.append("篡改")
        self.assertEqual(progress.run_steps(), ["步骤甲"])  # 总线未被污染

    def test_steps_readable_after_end(self):
        # end 只退订阅态，不清步骤——工具结束后仍能复盘
        progress.begin()
        progress.emit("排版存档")
        progress.end()
        self.assertEqual(progress.run_steps(), ["排版存档"])

    def test_zero_cost_without_subscriber(self):
        # 未 begin：emit 空操作，run_steps 始终为空，且不抛
        self.assertEqual(progress.run_steps(), [])
        progress.emit("没人听")
        self.assertEqual(progress.run_steps(), [])
        self.assertEqual(progress.drain(), [])

    def test_run_steps_never_raises(self):
        # 重复 begin/end 交错也不抛
        progress.begin()
        progress.begin()
        progress.emit("x")
        progress.end()
        progress.end()
        self.assertIsInstance(progress.run_steps(), list)


class TestDebriefGate(_Base):
    """note() 的准入闸门：不适用情形一律 None。"""

    def test_non_long_tool_returns_none(self):
        self.assertIsNone(debrief.note("get_time", {}, "先生，现在是下午三点。"))

    def test_non_str_result_returns_none(self):
        self.assertIsNone(debrief.note("make_ppt", {}, {"ok": True}))
        self.assertIsNone(debrief.note("make_ppt", {}, None))

    def test_failure_result_returns_none(self):
        self.assertIsNone(
            debrief.note("make_ppt", {}, "抱歉先生，PPT 没做成：模型超时。"))

    def test_needs_confirm_returns_none(self):
        self.assertIsNone(
            debrief.note("run_shell", {"cmd": "dir"}, "[[NEEDS_CONFIRM]] 有风险"))

    def test_empty_result_returns_none(self):
        self.assertIsNone(debrief.note("make_ppt", {}, "   "))

    def test_never_raises_on_garbage(self):
        # 入参再离谱也不抛
        self.assertIsNone(debrief.note(None, None, 12345))
        self.assertIsNone(debrief.note("make_ppt", "不是字典", "好的先生"))


class TestMakePpt(_Base):
    """make_ppt：有步骤 → 链路 + 文件名；无步骤 → 兜底概括。"""

    def test_success_with_steps(self):
        progress.begin()
        for s in _PPT_STEPS:
            progress.emit(s)
        progress.end()
        text = debrief.note("make_ppt", {"topic": "季度汇报"}, _PPT_OK)
        self.assertIsNotNone(text)
        # 语态/页码/标题碎片被规范化，链路只剩干净的节点名
        self.assertIn("联网查资料→设计大纲→逐页精写→生成配图→排版渲染→存档完成", text)
        self.assertIn("季度汇报.pptx", text)
        self.assertIn("文件柜", text)
        self.assertIn("演讲稿", text)
        self.assertIn("先生", text)

    def test_success_without_steps(self):
        # 无订阅（零开销路径）：没有步骤，走按工具概括
        text = debrief.note("make_ppt", {"topic": "季度汇报"}, _PPT_OK)
        self.assertIsNotNone(text)
        self.assertIn("PPT 制作流程", text)
        self.assertIn("季度汇报.pptx", text)

    def test_repeated_steps_canonical_dedupe(self):
        # 12 次逐页精写归一+去重后只剩一个节点（真机 PPT 埋点形态）
        progress.begin()
        for i in range(1, 13):
            progress.emit(f"正在精写第 {i}/12 页：标题碎片{i}")
        progress.end()
        text = debrief.note("make_ppt", {}, _PPT_OK)
        self.assertIsNotNone(text)
        chain = text.split("我按 ", 1)[1].split(" 的流程", 1)[0]
        self.assertEqual(chain, "逐页精写")

    def test_chain_compresses_when_too_many_steps(self):
        # 互不相同的长步骤才走首/中/尾代表性压缩
        progress.begin()
        for i in range(1, 13):
            progress.emit(f"特殊自定义环节编号{i}号")
        progress.end()
        text = debrief.note("make_ppt", {}, _PPT_OK)
        self.assertIsNotNone(text)
        self.assertIn("→", text)  # 压缩成首/中/尾链路
        chain = text.split("我按 ", 1)[1].split(" 的流程", 1)[0]
        self.assertLessEqual(len(chain), 60)

    def test_filename_with_corner_bracket(self):
        result = "好的先生，PPT 做好了，文件名「年度总结.pptx」，共 8 页。"
        text = debrief.note("make_ppt", {}, result)
        self.assertIn("年度总结.pptx", text)


class TestOtherTools(_Base):
    """edit_ppt / write_file / wechat_send_file / run_shell / gui_control。"""

    def test_edit_ppt_success(self):
        result = "好的先生，第 3 页已经改好了，文件名 季度汇报.pptx。"
        text = debrief.note(
            "edit_ppt", {"file_name": "季度汇报.pptx", "instruction": "改标题"}, result)
        self.assertIsNotNone(text)
        self.assertIn("季度汇报.pptx", text)
        self.assertIn("备注栏", text)

    def test_edit_ppt_failure_returns_none(self):
        self.assertIsNone(debrief.note(
            "edit_ppt", {}, "抱歉先生，PPT 修改模块现在不可用。"))

    def test_write_file_success(self):
        result = "好的先生，内容已经写进文件「会议纪要.md」了。"
        text = debrief.note(
            "write_file", {"name": "会议纪要.md", "content": "..."}, result)
        self.assertIsNotNone(text)
        self.assertIn("会议纪要.md", text)
        self.assertIn("文件柜", text)

    def test_wechat_send_file_success(self):
        result = "好的先生，文件已经发到微信了。"
        text = debrief.note(
            "wechat_send_file",
            {"file_name": "季度汇报.pptx", "target": "文件传输助手"},
            result)
        self.assertIsNotNone(text)
        self.assertIn("微信", text)
        self.assertIn("文件传输助手", text)
        self.assertIn("聊天记录", text)

    def test_wechat_send_file_custom_target(self):
        text = debrief.note(
            "wechat_send_file",
            {"file_name": "a.pptx", "target": "张总"},
            "好的先生，文件已经发出去了。")
        self.assertIn("张总", text)

    def test_run_shell_success(self):
        text = debrief.note("run_shell", {"cmd": "dir"}, "好的先生，命令跑完了。")
        self.assertIsNotNone(text)
        self.assertIn("屏幕", text)

    def test_gui_control_success(self):
        text = debrief.note(
            "gui_control", {"task": "打开记事本"}, "好的先生，操作完成了。")
        self.assertIsNotNone(text)
        self.assertIn("窗口", text)

    def test_gui_control_failure_returns_none(self):
        self.assertIsNone(debrief.note(
            "gui_control", {}, "抱歉先生，视觉模块现在不可用。"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
