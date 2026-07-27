# -*- coding: utf-8 -*-
"""
selftest_brain_v6.py —— 大脑通用化工程师_AgentLoop 阶段八自测
覆盖：
  1. Agent 循环（多步工具链）：假 LLM 第一次返回工具 JSON、第二次返回最终文本
     -> 验证循环正确执行、假 hands 恰好被调用一次、
        第二轮请求消息中含「[工具结果] ...」回灌；
  2. 4 轮上限：假 LLM 连续 5 次返回工具 JSON
     -> 验证最多 4 轮工具调用生效（LLM 请求 4 次、hands 执行 4 次），
        且直接返回第 4 轮工具结果文本；
  3. 待确认拦截保持现有行为：LLM 路径命中 [[NEEDS_CONFIRM]]
     -> 循环中断，直接返回确认询问，pending 挂起，LLM 仅请求一次；
  4. 人设 prompt 含「不设限」或「拆解」。
  5. 链式协作指引：构建后的 system prompt（工具协议段）含「串联」或「capture_screen」。
  6. open_app 动词过滤（阶段九）：_parse_intent('打开执行') 不映射 open_app（返回 None）；
     _parse_intent('打开微信') 仍返回 ('open_app', {'app': '微信'})。
  7. 任务前导指引（阶段十前导内置）：构建后的 system prompt 含
     「自己确保目标应用已经打开」或「自动检测窗口」。
说明：monkeypatch 假 LLM（替换 brain.httpx）与假 hands，全程离线、不联网、
不写存储文件、不弹窗；既有 selftest_brain_v5.py 不改动且须保持全绿。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
        print(f"[PASS] {name}")
    else:
        _FAILED += 1
        print(f"[FAIL] {name}  {detail}")


class FakeResp:
    """假的大模型 HTTP 响应：仅实现 brain 用到的两个方法。"""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class FakeHTTP:
    """假的 httpx 模块替身：按脚本依次返回预定回复，并记录每次请求体。"""

    HTTPError = brain.httpx.HTTPError  # 保持 brain 内 except 子句可正常求值

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls = []  # 记录每次请求的 payload，便于断言回灌消息

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        content = self.replies.pop(0) if self.replies else "好的先生，我明白了。"
        return FakeResp(content)


class FakeHands:
    """假的手模块：get_time 返回固定结果，记录每次调用。"""

    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, dict(args or {})))
        if name == "get_time":
            return "先生，现在是测试时间。"
        return f"fake:{name}"

    def list_tools(self):
        return []


class FakeHandsConfirm(FakeHands):
    """假的手模块（待确认版）：run_shell 未确认时返回 [[NEEDS_CONFIRM]]。"""

    def execute(self, name, args):
        self.calls.append((name, dict(args or {})))
        if name == "run_shell" and not (args or {}).get("confirmed"):
            return f"[[NEEDS_CONFIRM]] 命令「{args.get('cmd')}」需要先生确认。"
        return super().execute(name, args)


def main() -> int:
    # ---- 0. 通用 monkeypatch：假 API 配置 + 假 hands，测试结束统一还原 ----
    real_config = brain._load_llm_config
    real_httpx = brain.httpx
    real_hands = brain.hands
    brain._load_llm_config = lambda: {
        "api_key": "fake-key",
        "base_url": "http://fake-llm.local/v1",
        "model": "fake-model",
    }
    brain._pending_shell = None
    try:
        # ---- 1. 人设通用化 ----
        check(
            "人设 prompt 含「不设限」或「拆解」",
            ("不设限" in brain._SYSTEM_PROMPT) or ("拆解" in brain._SYSTEM_PROMPT),
            brain._SYSTEM_PROMPT,
        )

        # ---- 1b. 链式协作指引（工具协议段）----
        # 用假 hands 构建完整 system prompt（含工具协议段），全程离线
        brain.hands = FakeHands()
        built_prompt = brain._build_system_prompt()
        check(
            "工具协议段含「串联」或「capture_screen」",
            ("串联" in built_prompt) or ("capture_screen" in built_prompt),
            built_prompt,
        )

        # ---- 1c. 任务前导指引（阶段九：工具协议段）----
        check(
            "工具协议段含「自己确保目标应用已经打开」或「自动检测窗口」",
            ("自己确保目标应用已经打开" in built_prompt) or ("自动检测窗口" in built_prompt),
            built_prompt,
        )

        # ---- 1c2. search_web 使用指引（工具协议段）----
        check(
            "工具协议段含「search_web」（研究/新闻类任务指引）",
            "search_web" in built_prompt,
            built_prompt,
        )

        # ---- 1d. open_app 动词过滤（阶段九：规则意图层，纯函数离线断言）----
        check(
            "_parse_intent('打开执行') 不映射 open_app（返回 None）",
            brain._parse_intent("打开执行") is None,
            repr(brain._parse_intent("打开执行")),
        )
        check(
            "_parse_intent('打开微信') 仍返回 ('open_app', {'app': '微信'})",
            brain._parse_intent("打开微信") == ("open_app", {"app": "微信"}),
            repr(brain._parse_intent("打开微信")),
        )

        # ---- 2. Agent 循环：工具 JSON -> 执行 -> 回灌 -> 最终文本 ----
        fake_hands = FakeHands()
        brain.hands = fake_hands
        fake_http = FakeHTTP([
            '{"tool": "get_time", "args": {}}',
            "报告先生，现在是测试时间，今天的安排已为您整理妥当。",
        ])
        brain.httpx = fake_http

        reply = brain.think("请帮我整理一下今天的安排", [])
        check(
            "循环后返回 LLM 最终文本",
            reply == "报告先生，现在是测试时间，今天的安排已为您整理妥当。",
            reply,
        )
        check(
            "假 hands 恰好被调用一次",
            fake_hands.calls == [("get_time", {})],
            repr(fake_hands.calls),
        )
        check("LLM 共请求两次", len(fake_http.calls) == 2, repr(len(fake_http.calls)))
        # 第二轮请求的消息里应含「[工具结果] ...」回灌（作为 user 消息）
        second_messages = fake_http.calls[1]["messages"] if len(fake_http.calls) >= 2 else []
        tool_result_msgs = [
            m for m in second_messages
            if m.get("role") == "user" and str(m.get("content", "")).startswith("[工具结果] ")
        ]
        check(
            "第二轮请求含「[工具结果] 」user 回灌消息",
            len(tool_result_msgs) == 1
            and tool_result_msgs[0]["content"] == "[工具结果] 先生，现在是测试时间。",
            repr(second_messages),
        )
        check("循环未挂起待确认事项", brain._pending_shell is None, repr(brain._pending_shell))

        # ---- 3. 4 轮上限：假 LLM 连续 5 次返回工具 JSON ----
        fake_hands2 = FakeHands()
        brain.hands = fake_hands2
        fake_http2 = FakeHTTP(['{"tool": "get_time", "args": {}}'] * 5)
        brain.httpx = fake_http2

        reply = brain.think("请帮我整理一下今天的安排", [])
        check(
            "4 轮上限生效：LLM 仅请求 4 次",
            len(fake_http2.calls) == brain._MAX_TOOL_ROUNDS == 4,
            repr(len(fake_http2.calls)),
        )
        check(
            "4 轮上限生效：hands 仅执行 4 次",
            len(fake_hands2.calls) == 4
            and all(c == ("get_time", {}) for c in fake_hands2.calls),
            repr(fake_hands2.calls),
        )
        check(
            "第 4 轮仍返回工具 JSON：直接返回该结果文本",
            reply == "先生，现在是测试时间。",
            reply,
        )
        check("上限路径未挂起待确认事项", brain._pending_shell is None, repr(brain._pending_shell))

        # ---- 4. 待确认拦截保持现有行为（LLM 路径） ----
        fake_hands3 = FakeHandsConfirm()
        brain.hands = fake_hands3
        fake_http3 = FakeHTTP([
            '{"tool": "run_shell", "args": {"cmd": "dir"}}',
            "这条不应被请求到。",
        ])
        brain.httpx = fake_http3

        reply = brain.think("请帮我整理一下今天的安排", [])
        check(
            "命中 [[NEEDS_CONFIRM]]：循环中断并返回确认询问",
            "您确认执行吗" in reply,
            reply,
        )
        check(
            "命中 [[NEEDS_CONFIRM]]：pending 已挂起",
            brain._pending_shell == {"tool": "run_shell", "args": {"cmd": "dir"}},
            repr(brain._pending_shell),
        )
        check(
            "命中 [[NEEDS_CONFIRM]]：LLM 仅请求一次（不再回灌）",
            len(fake_http3.calls) == 1,
            repr(len(fake_http3.calls)),
        )
        brain._pending_shell = None  # 清理状态机，不影响其他自测
    finally:
        brain._load_llm_config = real_config
        brain.httpx = real_httpx
        brain.hands = real_hands
        brain._pending_shell = None

    print(f"\n结果：{_PASSED} 通过，{_FAILED} 失败")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
