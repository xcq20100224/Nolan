# -*- coding: utf-8 -*-
"""
T3 确认梯度 · auth_policy 测试（直接运行：python test_auth_policy.py）

覆盖：
- 三档各自的代表性工具判定（auto / default / confirm）
- run_shell 只读白名单 -> auto
- run_shell 破坏/敏感命令 -> confirm
- gui_control 支付等关键词 -> confirm
- wechat_send_file 真人目标 -> confirm，文件传输助手 -> default
- 未知工具 / 未识别参数 -> default（零回退死契约）
- 用户 JSON 黑/白名单与内置梯度的优先级
- 策略文件缺失 / 损坏 / 结构异常 / 坏正则 —— 不抛异常，行为安全
- decide 在模块异常路径下绝不抛
"""

import json
import os
import tempfile

import auth_policy
from auth_policy import AUTO, CONFIRM, DEFAULT, decide

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def check(name: str, actual, expected) -> None:
    """逐条断言并登记结果（不因单条失败中断后续测试）。"""
    global _PASS, _FAIL
    if actual == expected:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        _FAILURES.append(name)
        print(f"  FAIL  {name}：期望 {expected!r}，实际 {actual!r}")


def use_policy_file(content) -> None:
    """把 _POLICY_PATH 指向一个临时文件（content 为 None 表示文件不存在）。"""
    if content is None:
        auth_policy._POLICY_PATH = os.path.join(
            tempfile.gettempdir(), "auth_policy_不存在的文件.json")
    else:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        auth_policy._POLICY_PATH = path
    auth_policy._reset_cache()


# ---------------------------------------------------------------------------
# 一、内置梯度（无策略文件）—— T3 的核心行为
# ---------------------------------------------------------------------------
def test_builtin_gradient_without_policy_file() -> None:
    print("[1] 内置梯度（策略文件缺失）")
    use_policy_file(None)
    # 直接做：纯读取 / 沙盒可逆工具族
    for tool in ("search_web", "read_file", "get_time", "capture_screen",
                 "make_ppt", "edit_ppt", "write_file", "open_app"):
        check(f"auto:{tool}", decide(tool, {}), AUTO)
    # 做完汇报：发给自己（文件传输助手）与媒体控制维持 default
    check("default:wechat_send_file 文件传输助手",
          decide("wechat_send_file", {"file_name": "a.pptx", "target": "文件传输助手"}),
          DEFAULT)
    check("default:wechat_send_file 缺省目标",
          decide("wechat_send_file", {"file_name": "a.pptx"}), DEFAULT)
    check("default:media_control",
          decide("media_control", {"action": "pause"}), DEFAULT)


def test_run_shell_readonly_whitelist_auto() -> None:
    print("[2] run_shell 只读白名单 -> auto")
    use_policy_file(None)
    for cmd in ("dir", "dir C:\\Users", "echo hello", "type readme.txt",
                "where python", "whoami", "ipconfig", "ipconfig /all",
                "ver", "date /t", "  DIR  /b"):
        check(f"auto:run_shell {cmd!r}", decide("run_shell", {"cmd": cmd}), AUTO)


def test_run_shell_destructive_confirm() -> None:
    print("[3] run_shell 破坏/敏感命令 -> confirm")
    use_policy_file(None)
    for cmd in ("del C:\\important.txt", "DEL /f /q x", "rm -rf build",
                "rmdir /s /q tmp", "format C:", "shutdown /s /t 0",
                "shutdown /r", "restart-computer", "taskkill /f /im wechat.exe",
                "reg add HKLM\\Software", "regedit /s x.reg",
                "net user test 123 /add", "sc delete wuauserv",
                "copy a.txt C:\\Windows\\", "move a.txt D:\\", "mkdir C:\\newdir",
                "echo hello > C:\\x.txt", "echo hi >> log.txt",
                "curl http://x.com/a.exe -o a.exe", "powershell -enc AAA",
                "ipconfig /flushdns"):
        check(f"confirm:run_shell {cmd!r}", decide("run_shell", {"cmd": cmd}), CONFIRM)


def test_gui_control_keywords_confirm() -> None:
    print("[4] gui_control 支付/发给真人等关键词 -> confirm")
    use_policy_file(None)
    for task in ("帮我完成微信支付", "给张三付款100元", "微信转账给李四",
                 "给群里发红包", "把这条消息发送给王总", "删除好友张三",
                 "删除联系人李四"):
        check(f"confirm:gui_control {task!r}",
              decide("gui_control", {"task": task}), CONFIRM)
    check("default:gui_control 普通任务",
          decide("gui_control", {"task": "在记事本里打一段字"}), DEFAULT)


def test_wechat_real_person_confirm() -> None:
    print("[5] wechat_send_file 真人目标 -> confirm")
    use_policy_file(None)
    check("confirm:发真人", decide(
        "wechat_send_file", {"file_name": "a.pptx", "target": "王总"}), CONFIRM)
    check("confirm:发群", decide(
        "wechat_send_file", {"file_name": "a.pptx", "target": "产品讨论群"}), CONFIRM)
    check("default:filehelper 别名", decide(
        "wechat_send_file", {"file_name": "a.pptx", "target": "FileHelper"}), DEFAULT)


def test_unknown_tool_default() -> None:
    print("[6] 未知工具 / 未识别参数 -> default（死契约）")
    use_policy_file(None)
    check("default:未知工具", decide("some_future_tool", {"x": 1}), DEFAULT)
    check("default:run_shell 普通命令", decide("run_shell", {"cmd": "python app.py"}), DEFAULT)
    check("default:run_shell 非字符串 cmd", decide("run_shell", {"cmd": 123}), DEFAULT)
    check("default:gui_control 缺 task", decide("gui_control", {}), DEFAULT)
    check("default:args 为 None", decide("read_file", None), AUTO)  # None 容错且仍命中 auto


# ---------------------------------------------------------------------------
# 二、用户 JSON 与内置梯度的优先级
# ---------------------------------------------------------------------------
def test_user_json_priority() -> None:
    print("[7] 用户 JSON 规则与内置梯度的优先级")
    use_policy_file({
        "whitelist": [{"tool": "gui_control", "app_pattern": "记事本|notepad"},
                      {"tool": "run_shell", "cmd_pattern": "^del\\b"}],  # 试图洗白危险命令
        "blacklist": [{"tool": "search_web"},  # 用户把 auto 工具拉进黑名单
                      {"tool": "run_shell", "cmd_pattern": "部署"}],
    })
    # 用户黑名单 > 内置 auto
    check("confirm:用户黑名单压过内置 auto", decide("search_web", {"q": "天气"}), CONFIRM)
    # 用户白名单正常生效（内置梯度未覆盖的形态）
    check("auto:用户白名单 gui_control 记事本",
          decide("gui_control", {"task": "在记事本里打字"}), AUTO)
    # 安全侧恒优先：内置 confirm 压过用户白名单
    check("confirm:内置黑名单压过用户白名单（del）",
          decide("run_shell", {"cmd": "del C:\\x"}), CONFIRM)
    check("confirm:用户黑名单自定义关键词",
          decide("run_shell", {"cmd": "python 部署.py"}), CONFIRM)


# ---------------------------------------------------------------------------
# 三、异常路径：绝不抛异常，行为安全
# ---------------------------------------------------------------------------
def test_broken_policy_file_no_raise() -> None:
    print("[8] 策略文件损坏/结构异常 -> 内置梯度照常，不抛异常")
    use_policy_file("{这根本不是 JSON")
    check("坏 JSON 后 auto 仍生效", decide("search_web", {}), AUTO)
    check("坏 JSON 后 confirm 仍生效",
          decide("run_shell", {"cmd": "shutdown /s"}), CONFIRM)
    use_policy_file(["顶层不是对象"])
    check("结构异常后 default 正常", decide("unknown_tool", {}), DEFAULT)
    use_policy_file({"whitelist": "不是数组", "blacklist": None})
    check("字段类型异常后 auto 仍生效", decide("read_file", {}), AUTO)


def test_bad_rule_entries_no_raise() -> None:
    print("[9] 坏规则条目 / 坏正则 -> 跳过并警告，不抛异常")
    use_policy_file({
        "whitelist": [None, 42, {"tool": "read_file", "name_pattern": "(坏正则"},
                      {"tool": "get_time"}],
        "blacklist": [{"tool": "run_shell", "cmd_pattern": "[unclosed"}],
    })
    check("坏正则条目被跳过", decide("read_file", {"name": "x"}), AUTO)  # 内置 auto 兜底
    check("合法条目仍生效", decide("get_time", {}), AUTO)
    check("坏黑名单正则不误伤", decide("run_shell", {"cmd": "dir"}), AUTO)


def test_decide_never_raises_on_internal_failure() -> None:
    print("[10] 模块异常路径不抛（兜底 default）")
    # 模拟策略缓存被污染成不可遍历结构
    auth_policy._POLICY_PATH = None  # type: ignore[assignment]
    auth_policy._reset_cache()
    check("_POLICY_PATH 为 None 不抛", decide("search_web", {}), DEFAULT)
    # 恢复
    auth_policy._POLICY_PATH = os.path.join(
        os.path.dirname(os.path.abspath(auth_policy.__file__)), "auth_policy.json")
    auth_policy._reset_cache()
    check("恢复后内置梯度正常", decide("capture_screen", {}), AUTO)


def test_real_default_policy_file_synced() -> None:
    print("[11] 默认策略文件（auth_policy.json）与内置梯度同步")
    real = os.path.join(os.path.dirname(os.path.abspath(auth_policy.__file__)),
                        "auth_policy.json")
    check("默认策略文件存在", os.path.exists(real), True)
    auth_policy._POLICY_PATH = real
    auth_policy._reset_cache()
    # 加载真实文件后，三档代表性判定应与内置梯度一致
    check("真实文件:auto", decide("make_ppt", {"topic": "周报"}), AUTO)
    check("真实文件:confirm run_shell", decide("run_shell", {"cmd": "format D:"}), CONFIRM)
    check("真实文件:confirm gui 支付",
          decide("gui_control", {"task": "帮我付款"}), CONFIRM)
    check("真实文件:confirm 发真人",
          decide("wechat_send_file", {"target": "张三"}), CONFIRM)
    check("真实文件:default", decide("media_control", {"action": "play"}), DEFAULT)


def main() -> int:
    test_builtin_gradient_without_policy_file()
    test_run_shell_readonly_whitelist_auto()
    test_run_shell_destructive_confirm()
    test_gui_control_keywords_confirm()
    test_wechat_real_person_confirm()
    test_unknown_tool_default()
    test_user_json_priority()
    test_broken_policy_file_no_raise()
    test_bad_rule_entries_no_raise()
    test_decide_never_raises_on_internal_failure()
    test_real_default_policy_file_synced()
    print(f"\n结果：{_PASS} 通过，{_FAIL} 失败")
    if _FAILURES:
        print("失败条目：")
        for name in _FAILURES:
            print(f"  - {name}")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
