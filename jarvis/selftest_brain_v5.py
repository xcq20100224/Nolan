# -*- coding: utf-8 -*-
"""
selftest_brain_v5.py —— 大脑工程师_BrainPersona 阶段五自测
覆盖：
  1. 人设 prompt 含「汉弗莱」或「可靠」；
  2. _parse_intent('执行 dir') -> ('run_shell', ...)；
  3. 待确认状态机：monkeypatch hands.execute 返回 [[NEEDS_CONFIRM]] 文本
     -> think 返回确认询问 -> think('确认执行') 触发 confirmed=True 调用
     -> think('取消') 流程正确 -> 新指令覆盖旧 pending。
说明：测试通过规则意图路径（「执行 xxx」）驱动，无需联网、不弹窗、不写存储文件。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis"))

import brain  # noqa: E402
import reminders  # noqa: E402  闹钟断言需直接读写提醒存储（备份/还原）

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


class FakeHands:
    """假的手模块：run_shell 未确认返回 [[NEEDS_CONFIRM]]，确认后返回执行结果。"""

    def __init__(self):
        self.calls = []  # 记录 (name, args) 便于断言

    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "run_shell":
            if not args.get("confirmed"):
                return f"[[NEEDS_CONFIRM]] 命令「{args.get('cmd')}」需要先生确认。"
            return f"先生，命令「{args.get('cmd')}」已执行完毕，结果正常。"
        if name == "get_time":
            return "先生，现在是测试时间。"
        return f"fake:{name}"

    def list_tools(self):
        return []


def main() -> int:
    # ---- 1. 人设 prompt ----
    check(
        "人设 prompt 含「汉弗莱」或「可靠」",
        ("汉弗莱" in brain._SYSTEM_PROMPT) or ("可靠" in brain._SYSTEM_PROMPT),
    )
    check(
        "人设 prompt 不含幽默类措辞",
        "幽默" not in brain._SYSTEM_PROMPT and "笑话" not in brain._SYSTEM_PROMPT.replace("不讲笑话", ""),
    )
    check(
        "兜底模板为正式风格（无「妙语连珠」「英式幽默」等）",
        all("妙语连珠" not in t and "幽默" not in t for t in brain._FALLBACK_REPLIES),
    )

    # ---- 2. 规则意图映射 ----
    intent = brain._parse_intent("执行 dir")
    check("_parse_intent('执行 dir') -> run_shell", intent is not None and intent[0] == "run_shell", repr(intent))
    check("_parse_intent('执行 dir') 参数含 cmd='dir'", intent is not None and intent[1].get("cmd") == "dir", repr(intent))
    intent2 = brain._parse_intent("运行 notepad.exe")
    check("_parse_intent('运行 notepad.exe') -> run_shell", intent2 is not None and intent2[0] == "run_shell", repr(intent2))

    # ---- 3. 待确认状态机 ----
    fake = FakeHands()
    brain.hands = fake          # monkeypatch：替换 hands 模块引用
    brain._pending_shell = None  # 复位状态机

    # 3.1 触发 confirm 级命令 -> 返回确认询问，pending 已存
    reply = brain.think("执行 dir", [])
    check("未确认时 think 返回确认询问", "您确认执行吗" in reply and "dir" in reply, reply)
    check("确认询问后 pending 已存入", brain._pending_shell == {"tool": "run_shell", "args": {"cmd": "dir"}}, repr(brain._pending_shell))

    # 3.2 用户说「确认执行」-> confirmed=True 调用，pending 清空
    reply = brain.think("确认执行", [])
    check("「确认执行」返回执行结果", "已执行完毕" in reply, reply)
    check(
        "「确认执行」触发 confirmed=True 调用",
        ("run_shell", {"cmd": "dir", "confirmed": True}) in fake.calls,
        repr(fake.calls),
    )
    check("确认后 pending 已清空", brain._pending_shell is None, repr(brain._pending_shell))

    # 3.3 取消流程：重新挂起一条 pending，再说「取消」
    brain.think("执行 dir", [])
    check("再次触发后 pending 已存入", brain._pending_shell == {"tool": "run_shell", "args": {"cmd": "dir"}}, repr(brain._pending_shell))
    reply = brain.think("取消", [])
    check("「取消」回复固定话术", reply == "好的先生，已取消该操作。", reply)
    check("取消后 pending 已清空", brain._pending_shell is None, repr(brain._pending_shell))
    check(
        "取消流程未发起 confirmed=True 调用",
        all(not (n == "run_shell" and a.get("confirmed")) for n, a in fake.calls[-1:]),
        repr(fake.calls),
    )

    # 3.4 新指令覆盖旧 pending：挂起 pending 后说「几点了」，走正常流程且 pending 清空
    brain.think("执行 dir", [])
    reply = brain.think("几点了", [])
    check("新指令覆盖后走正常流程", "测试时间" in reply, reply)
    check("新指令覆盖后 pending 已清空", brain._pending_shell is None, repr(brain._pending_shell))

    # 3.5 hands 为 None 时确认分支不崩
    brain.hands = None
    brain._pending_shell = {"tool": "run_shell", "args": {"cmd": "dir"}}
    reply = brain.think("确认", [])
    check("hands 不可用时确认分支如实汇报", "未能执行" in reply, reply)
    check("hands 不可用时 pending 仍被清空", brain._pending_shell is None, repr(brain._pending_shell))

    # ---- 4. 阶段六：媒体控制规则意图 + 能力边界人设 ----
    intent = brain._parse_intent("暂停一下")
    check(
        "_parse_intent('暂停一下') -> media_control play_pause",
        intent == ("media_control", {"action": "play_pause"}),
        repr(intent),
    )
    intent = brain._parse_intent("下一首")
    check(
        "_parse_intent('下一首') -> media_control next",
        intent == ("media_control", {"action": "next"}),
        repr(intent),
    )
    intent = brain._parse_intent("音量小一点")
    check(
        "_parse_intent('音量小一点') -> media_control volume_down",
        intent == ("media_control", {"action": "volume_down"}),
        repr(intent),
    )
    intent = brain._parse_intent("播放我喜欢列表中的第一首歌曲")
    check(
        "_parse_intent('播放我喜欢列表中的第一首歌曲') -> None（不劫持）",
        intent is None,
        repr(intent),
    )

    # 人设 prompt（工具协议段）须含「能力边界」——需 hands 就绪才生成工具协议
    brain.hands = fake
    brain._pending_shell = None
    prompt = brain._build_system_prompt()
    check("人设 prompt 含「能力边界」", "能力边界" in prompt)
    brain.hands = None

    # ---- 5. 闹钟/叫醒意图（大脑工程师_AlarmBrain）----
    # 备份 reminders.txt，测试结束后原样还原，绝不污染先生的真实提醒
    reminders_file = reminders._REMINDERS_FILE
    backup = None
    if os.path.exists(reminders_file):
        with open(reminders_file, "rb") as f:
            backup = f.read()
    # 离线化：大模型层替换为固定回复，避免联网且保证「走闲聊/LLM」路径可断言
    real_llm = brain._think_via_llm
    brain._think_via_llm = lambda *a, **k: "好的先生，我明白了。"
    try:
        before = reminders._read_entries()

        # 5.1 「1分钟后叫醒我」-> 创建提醒，默认内容「起床啦，先生」
        reply = brain.think("1分钟后叫醒我", [])
        check(
            "think('1分钟后叫醒我') 返回确认（含「提醒」或「叫」）",
            ("提醒" in reply) or ("叫" in reply),
            reply,
        )
        after = reminders._read_entries()
        new_entries = [e for e in after if e not in before]
        check(
            "「1分钟后叫醒我」新增一条提醒",
            len(new_entries) == 1,
            repr(new_entries),
        )
        check(
            "新提醒内容为默认「起床啦，先生」",
            bool(new_entries) and new_entries[0][1] == "起床啦，先生",
            repr(new_entries),
        )

        # 5.2 「明天早上七点半叫我起床」-> 返回确认
        before2 = reminders._read_entries()
        reply = brain.think("明天早上七点半叫我起床", [])
        check(
            "think('明天早上七点半叫我起床') 返回确认（含「提醒」或「叫」）",
            ("提醒" in reply) or ("叫" in reply),
            reply,
        )
        after2 = reminders._read_entries()
        check(
            "「明天早上七点半叫我起床」新增一条提醒",
            len([e for e in after2 if e not in before2]) == 1,
            repr(after2),
        )

        # 5.3 「你叫我什么都行」-> 不触发闹钟意图（走闲聊/LLM，不新增提醒）
        before3 = reminders._read_entries()
        reply = brain.think("你叫我什么都行", [])
        after3 = reminders._read_entries()
        check(
            "think('你叫我什么都行') 不新增提醒",
            len(after3) == len(before3),
            repr(after3),
        )
    finally:
        brain._think_via_llm = real_llm
        # 还原 reminders.txt：原本不存在则删除，存在则写回原字节
        if backup is None:
            if os.path.exists(reminders_file):
                os.remove(reminders_file)
        else:
            with open(reminders_file, "wb") as f:
                f.write(backup)

    print(f"\n结果：{_PASSED} 通过，{_FAILED} 失败")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
