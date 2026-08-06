# -*- coding: utf-8 -*-
"""
test_wechat_kbd.py —— wechat_kbd 的纯 mock 测试

零真机、零 GUI、零剪贴板、零 VLM：窗口查找 / 键盘注入 / 剪贴板登台 /
截图 / VLM 验收全部打桩，等待原语 _sleep 打桩提速。
每条断言只验证一件事：双路径（已在会话 / 搜索步进）控制回路的路由
与安全闸的否决行为——尤其是「安全闸 A 否决时必须按 Esc 清理现场、
文件绝不发送」。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wechat_kbd


class WechatKbdTest(unittest.TestCase):

    def setUp(self):
        # 真实文件（文件校验要过 os.path.isfile）
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.file_path = os.path.join(self._tmpdir.name, "汇报.pptx")
        with open(self.file_path, "wb") as f:
            f.write(b"fake-pptx")

        # 全部外部触点打桩：默认走「已在目标会话 -> 直接粘贴」的放行路径，
        # 各测试按需覆盖 _vlm_yes 的 side_effect
        patchers = {
            "_sleep": mock.Mock(),
            "_hotkey": mock.Mock(),
            "_press": mock.Mock(),
            "_weixin_running": mock.Mock(return_value=True),
            "_find_wechat_hwnd": mock.Mock(return_value=12345),
            "_wake_to_foreground": mock.Mock(return_value=True),
            "_stage_text_to_clipboard": mock.Mock(return_value=True),
            "_stage_file_to_clipboard": mock.Mock(return_value=True),
            "_shot_window_b64": mock.Mock(return_value="ZmFrZS1zaG90"),
            "_vlm_yes": mock.Mock(side_effect=[(True, "是"), (True, "是")]),
        }
        self.mocks = {}
        for name, m in patchers.items():
            p = mock.patch.object(wechat_kbd, name, m)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

    def send(self, **kwargs):
        return wechat_kbd.send_file_via_keyboard(self.file_path, **kwargs)

    def _press_count(self, vk):
        return len([c for c in self.mocks["_press"].call_args_list
                    if c.args[0] == vk])

    def _hotkey_count(self, *vks):
        return len([c for c in self.mocks["_hotkey"].call_args_list
                    if tuple(c.args) == vks])

    # ---- 前置失败 ----

    def test_wechat_not_running(self):
        self.mocks["_weixin_running"].return_value = False
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "微信没开")
        self.mocks["_hotkey"].assert_not_called()  # 一个键都不许按

    def test_wake_timeout(self):
        self.mocks["_wake_to_foreground"].return_value = False
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "唤起超时")
        self.mocks["_stage_text_to_clipboard"].assert_not_called()

    def test_window_not_found(self):
        self.mocks["_find_wechat_hwnd"].return_value = 0
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "找不到微信窗口")

    def test_file_missing(self):
        r = wechat_kbd.send_file_via_keyboard(
            os.path.join(self._tmpdir.name, "不存在.txt"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "文件校验")
        self.mocks["_weixin_running"].assert_not_called()

    # ---- 正常路径 A：已在目标会话，直接粘贴 ----

    def test_happy_path_direct(self):
        r = self.send(target="文件传输助手")
        self.assertTrue(r["ok"])
        self.assertEqual(r["stage"], "键盘流发送成功")
        # 两道闸都真被调用过：预检（已在会话）+ 发送后验收
        self.assertEqual(self.mocks["_vlm_yes"].call_count, 2)
        q_pre = self.mocks["_vlm_yes"].call_args_list[0].args[1]
        q_b = self.mocks["_vlm_yes"].call_args_list[1].args[1]
        self.assertIn("文件传输助手", q_pre)
        self.assertIn("汇报.pptx", q_b)
        # 直粘路径：不开搜索、不步进、不按 Esc；仅 Ctrl+V(文件) + Enter
        self.assertEqual(
            self._hotkey_count(wechat_kbd._VK_CONTROL, wechat_kbd._VK_F), 0)
        self.assertEqual(
            self._hotkey_count(wechat_kbd._VK_CONTROL, wechat_kbd._VK_V), 1)
        self.assertEqual(self._press_count(wechat_kbd._VK_DOWN), 0)
        self.assertEqual(self._press_count(wechat_kbd._VK_ESCAPE), 0)
        self.assertEqual(self._press_count(wechat_kbd._VK_RETURN), 1)

    # ---- 正常路径 B：搜索 + 标题栏步进验收 + Enter 进会话 ----

    def test_happy_path_via_search(self):
        # 预检否 -> 搜索后第 0 次采样即命中（标题栏已变）-> 进会话复核是 -> B 是
        self.mocks["_vlm_yes"].side_effect = [
            (False, "否"), (True, "是"), (True, "是"), (True, "是")]
        r = self.send(target="文件传输助手")
        self.assertTrue(r["ok"])
        self.assertEqual(
            self._hotkey_count(wechat_kbd._VK_CONTROL, wechat_kbd._VK_F), 1)
        self.assertEqual(
            self._hotkey_count(wechat_kbd._VK_CONTROL, wechat_kbd._VK_V), 2)
        self.assertEqual(self._press_count(wechat_kbd._VK_DOWN), 0)
        self.assertEqual(self._press_count(wechat_kbd._VK_ESCAPE), 0)
        # Enter 两下：进会话 + 发送确认
        self.assertEqual(self._press_count(wechat_kbd._VK_RETURN), 2)
        self.assertEqual(self.mocks["_vlm_yes"].call_count, 4)

    def test_gate_a_steps_down_until_hit(self):
        # 预检否 -> 第 0 次采样否 -> Down 步进 -> 第 1 次采样是 -> 放行
        self.mocks["_vlm_yes"].side_effect = [
            (False, "否"), (False, "否"), (True, "是"),
            (True, "是"), (True, "是")]
        r = self.send()
        self.assertTrue(r["ok"])
        self.assertEqual(self._press_count(wechat_kbd._VK_DOWN), 1,
                         "未命中一次应恰好步进一次 Down")
        self.assertEqual(self.mocks["_vlm_yes"].call_count, 5)

    # ---- 安全闸 A 否决 ----

    def test_gate_a_reject_escapes_and_never_sends(self):
        self.mocks["_vlm_yes"].side_effect = None
        self.mocks["_vlm_yes"].return_value = (False, "否")
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "搜索未命中")
        # Esc 清理现场被调用
        self.assertGreaterEqual(self._press_count(wechat_kbd._VK_ESCAPE), 1)
        # 文件绝不登台、Enter 绝不按
        self.mocks["_stage_file_to_clipboard"].assert_not_called()
        self.assertEqual(self._press_count(wechat_kbd._VK_RETURN), 0)

    def test_gate_a_vlm_exception_counts_as_reject(self):
        self.mocks["_vlm_yes"].side_effect = None
        # _vlm_yes 自身不抛（异常在内部归约为不通过），这里模拟其归约结果
        self.mocks["_vlm_yes"].return_value = (False, "（VLM 调用异常）")
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "搜索未命中")
        self.mocks["_stage_file_to_clipboard"].assert_not_called()

    def test_enter_conversation_recheck_rejects(self):
        # 步进命中但 Enter 后标题栏复核否决：绝不粘贴发送
        self.mocks["_vlm_yes"].side_effect = [
            (False, "否"), (True, "是"), (False, "否")]
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "会话切换未确认")
        self.assertEqual(self._press_count(wechat_kbd._VK_RETURN), 1)  # 仅进会话
        self.mocks["_stage_file_to_clipboard"].assert_not_called()

    # ---- 发送侧失败 ----

    def test_file_clipboard_stage_failure(self):
        self.mocks["_stage_file_to_clipboard"].return_value = False
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "剪贴板登台失败")
        # 文件没登台，发送确认的那下 Enter 绝不许按
        self.assertEqual(self._press_count(wechat_kbd._VK_RETURN), 0)

    def test_text_clipboard_stage_failure(self):
        self.mocks["_vlm_yes"].side_effect = [(False, "否")]  # 进搜索分支
        self.mocks["_stage_text_to_clipboard"].return_value = False
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "剪贴板登台失败")
        self.mocks["_stage_file_to_clipboard"].assert_not_called()

    # ---- 安全闸 B 否决 ----

    def test_gate_b_reject_honest_report(self):
        self.mocks["_vlm_yes"].side_effect = [(True, "是"), (False, "否")]
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "发送未确认")
        self.assertIn("看一眼微信", r["detail"])
        self.assertEqual(self.mocks["_vlm_yes"].call_count, 2)

    # ---- 契约 ----

    def test_never_raises_on_unexpected(self):
        self.mocks["_find_wechat_hwnd"].side_effect = RuntimeError("炸了")
        r = self.send()
        self.assertFalse(r["ok"])  # 归约为 ok=False 的人话，不抛


if __name__ == "__main__":
    unittest.main(verbosity=2)
