# -*- coding: utf-8 -*-
"""
test_wechat_send.py —— wechat_send 的纯 mock 测试

零真机、零 GUI、零剪贴板、零 subprocess：eyes / skills / hands 三个
依赖模块整体替换为 Mock，剪贴板登台函数打桩，文件柜指向临时目录。
每条断言只验证一件事：分层降级链的路由与话术的诚实性。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wechat_send


class WechatSendTest(unittest.TestCase):

    def setUp(self):
        # 文件柜指向临时目录，并备一个真实文件「汇报.pptx」
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.files_dir = self._tmpdir.name
        with open(os.path.join(self.files_dir, "汇报.pptx"), "wb") as f:
            f.write(b"fake-pptx")

        # 替换模块级依赖（防御式导入的全局名），测试后还原
        self._orig = (wechat_send._FILES_DIR, wechat_send._eyes,
                      wechat_send._skills, wechat_send._hands,
                      wechat_send._wechat_kbd)
        self.addCleanup(self._restore)

        wechat_send._FILES_DIR = self.files_dir
        self.eyes = mock.Mock()
        self.skills = mock.Mock()
        self.hands = mock.Mock()
        wechat_send._eyes = self.eyes
        wechat_send._skills = self.skills
        wechat_send._hands = self.hands
        # L1.6 键盘流打桩：默认诚实失败降级（不碰真键盘/剪贴板/VLM），
        # 各层路由与话术断言维持原样
        self.kbd = mock.Mock()
        self.kbd.send_file_via_keyboard.return_value = {
            "ok": False, "stage": "打桩", "detail": ""}
        wechat_send._wechat_kbd = self.kbd

        # 剪贴板登台打桩：默认成功（不碰真剪贴板）
        stage = mock.patch.object(wechat_send, "_stage_file_to_clipboard",
                                  return_value=True)
        self.stage = stage.start()
        self.addCleanup(stage.stop)

        # 默认路由：技能库未命中（各测试按需改）
        self.skills.find.return_value = None

    def _restore(self):
        (wechat_send._FILES_DIR, wechat_send._eyes,
         wechat_send._skills, wechat_send._hands,
         wechat_send._wechat_kbd) = self._orig

    # ---- L0：文件校验 ----

    def test_missing_file_rejected(self):
        msg = wechat_send.send_file("不存在.txt")
        self.assertIn("找不到", msg)
        self.assertIn("不存在.txt", msg)
        self.skills.find.assert_not_called()
        self.eyes.perform.assert_not_called()

    def test_path_traversal_rejected(self):
        for bad in ("../x.txt", "..\\x.txt", "C:\\a\\b.txt",
                    "a/b.txt", "uploads/x.txt", "", "   ", ".."):
            with self.subTest(bad=bad):
                msg = wechat_send.send_file(bad)
                self.assertIn("纯文件名", msg)
        self.skills.find.assert_not_called()
        self.eyes.perform.assert_not_called()

    # ---- L1：技能快路 ----

    def test_skill_hit_goes_replay(self):
        steps = [{"action": "key", "keys": "ctrl+v", "text": ""}]
        self.skills.find.return_value = (
            "在微信里把文件发给「文件传输助手」", steps)
        self.eyes.replay.return_value = \
            "先生，按我已掌握的技能为您完成了：在微信里把文件发给「文件传输助手」。"
        msg = wechat_send.send_file("汇报.pptx")
        self.skills.find.assert_called_once_with(
            "在微信里把文件发给「文件传输助手」")
        self.eyes.replay.assert_called_once_with(
            "在微信里把文件发给「文件传输助手」", steps, target_hint="微信")
        self.eyes.perform.assert_not_called()
        self.assertIn("汇报.pptx", msg)
        self.assertIn("发给文件传输助手", msg)

    def test_replay_miss_falls_back_to_perform(self):
        self.skills.find.return_value = (
            "在微信里把文件发给「文件传输助手」",
            [{"action": "key", "keys": "ctrl+v", "text": ""}])
        self.eyes.replay.return_value = None  # 重放放不进现实
        self.eyes.perform.return_value = "文件卡片已经出现在会话里了。"
        msg = wechat_send.send_file("汇报.pptx")
        self.eyes.replay.assert_called_once()
        self.eyes.perform.assert_called_once()
        # 降级后成功，同样固化技能
        self.skills.record.assert_called_once()
        self.assertIn("发给文件传输助手", msg)

    # ---- L2：视觉闭环 ----

    def test_perform_success_records_skill(self):
        self.eyes.perform.return_value = "已经把文件发给文件传输助手了。"
        msg = wechat_send.send_file("汇报.pptx", target="张三")
        # perform 收到精心构造的任务文案与窗口 hint
        args, kwargs = self.eyes.perform.call_args
        self.assertEqual(kwargs.get("target_hint"), "微信")
        self.assertIn("张三", args[0])
        self.assertIn(os.path.join(self.files_dir, "汇报.pptx"), args[0])
        # 打开微信前导走 hands.open_app，应用名原文
        self.hands.execute.assert_called_once_with("open_app", {"app": "微信"})
        # 成功固化：参数化任务名 + 规范动作序列
        rec_args, _ = self.skills.record.call_args
        self.assertEqual(rec_args[0], "在微信里把文件发给「张三」")
        self.assertTrue(any(s.get("text") == "张三" for s in rec_args[1]))
        # 成功话术含文件名
        self.assertIn("汇报.pptx", msg)
        self.assertIn("发给张三", msg)

    def test_default_target_is_file_helper(self):
        self.eyes.perform.return_value = "发好了。"
        wechat_send.send_file("汇报.pptx")
        self.skills.find.assert_called_once_with(
            "在微信里把文件发给「文件传输助手」")

    def test_clipboard_stage_failure_switches_route(self):
        self.stage.return_value = False
        self.eyes.perform.return_value = "发好了。"
        wechat_send.send_file("汇报.pptx")
        task = self.eyes.perform.call_args[0][0]
        self.assertIn("文件选择对话框", task)  # 备选路线写进任务文案
        self.assertNotIn("系统已经把这个文件复制到剪贴板", task)

    # ---- L3：诚实话术 ----

    def test_perform_failure_honest_report(self):
        self.eyes.perform.return_value = \
            "先生，任务未能完成。卡在第 3 步：连续两次操作未产生预期效果。"
        msg = wechat_send.send_file("汇报.pptx")
        self.assertIn("没能自动完成", msg)
        self.assertIn("汇报.pptx", msg)
        self.assertIn("手动", msg)
        self.assertNotIn("已发送", msg)
        self.skills.record.assert_not_called()

    def test_wechat_not_logged_in(self):
        self.eyes.perform.return_value = (
            "先生，任务未能完成。卡在第 1 步：屏幕上没有找到微信。"
            "当前屏幕显示的是：微信登录二维码界面。")
        msg = wechat_send.send_file("汇报.pptx")
        self.assertIn("请先登录微信", msg)
        self.assertIn("汇报.pptx", msg)
        self.assertNotIn("已发送", msg)
        self.skills.record.assert_not_called()

    def test_failsafe_passthrough(self):
        self.eyes.perform.return_value = \
            "先生，检测到您将鼠标移至屏幕角落，操作已被安全中止。"
        msg = wechat_send.send_file("汇报.pptx")
        self.assertIn("安全中止", msg)
        self.assertNotIn("已发送", msg)
        self.skills.record.assert_not_called()

    def test_eyes_unavailable(self):
        wechat_send._eyes = None
        msg = wechat_send.send_file("汇报.pptx")
        self.assertIn("没能自动完成", msg)
        self.assertIn("汇报.pptx", msg)
        self.assertNotIn("已发送", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
