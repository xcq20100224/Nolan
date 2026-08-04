# -*- coding: utf-8 -*-
"""
H3 分级授权 · 纯单元测试（test_auth.py，零依赖，直接 python 运行）
覆盖：空策略=default / 白名单命中=auto / 黑名单优先 / 非法正则跳过 /
缺文件不抛 / brain 拦截处三档行为（mock hands 与 auth_policy）。
死契约用例：缺省配置下 default 档行为与现行确认流程一字不差。
运行：python jarvis/test_auth.py
"""

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import auth_policy  # noqa: E402

_REAL_POLICY_PATH = os.path.join(_HERE, "auth_policy.json")
_PASSED = []
_FAILED = []


def case(name):
    """测试用例装饰器：收集结果，异常即失败，绝不中断后续用例。"""
    def deco(fn):
        try:
            fn()
            _PASSED.append(name)
            print(f"  PASS  {name}")
        except Exception as e:  # noqa: BLE001
            _FAILED.append((name, e))
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        return fn
    return deco


@contextlib.contextmanager
def _policy_file(data):
    """把策略写入临时文件并指向它；data 为 None 时模拟缺文件。退出后还原。"""
    tmpdir = tempfile.mkdtemp(prefix="auth_test_")
    if data is None:
        path = os.path.join(tmpdir, "no_such_policy.json")
    else:
        path = os.path.join(tmpdir, "auth_policy.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    old = auth_policy._POLICY_PATH
    auth_policy._POLICY_PATH = path
    auth_policy._reset_cache()
    try:
        yield path
    finally:
        auth_policy._POLICY_PATH = old
        auth_policy._reset_cache()


# ==== auth_policy.decide 单测 ====

@case("缺文件不抛且全部 default（空策略=现行流程）")
def _():
    with _policy_file(None):
        assert auth_policy.decide("run_shell", {"cmd": "dir"}) == "default"
        assert auth_policy.decide("gui_control", {"task": "在记事本打字"}) == "default"
        assert auth_policy.decide("run_shell", {"cmd": "del x"}) == "default"


@case("真实仓库中策略文件确实不存在（缺文件=默认行为契约）")
def _():
    assert not os.path.exists(_REAL_POLICY_PATH), "契约破坏：真实 auth_policy.json 不应存在"


@case("白名单命中返回 auto")
def _():
    with _policy_file({
        "whitelist": [
            {"tool": "gui_control", "app_pattern": "记事本|notepad"},
            {"tool": "run_shell", "cmd_pattern": "^dir\\b"},
        ],
        "blacklist": [],
    }):
        assert auth_policy.decide("gui_control", {"task": "在记事本里打字"}) == "auto"
        assert auth_policy.decide("run_shell", {"cmd": "dir C:\\"}) == "auto"
        assert auth_policy.decide("run_shell", {"cmd": "echo hi"}) == "default"
        assert auth_policy.decide("gui_control", {"task": "在浏览器里点击"}) == "default"


@case("黑名单优先于白名单")
def _():
    with _policy_file({
        "whitelist": [{"tool": "run_shell", "cmd_pattern": "^dir\\b"}],
        "blacklist": [{"tool": "run_shell", "cmd_pattern": "dir.*密码"}],
    }):
        assert auth_policy.decide("run_shell", {"cmd": "dir 密码.txt"}) == "confirm"  # 同中黑白 → 黑胜
        assert auth_policy.decide("run_shell", {"cmd": "dir"}) == "auto"


@case("黑名单命中即使原本安全也 confirm")
def _():
    with _policy_file({
        "whitelist": [],
        "blacklist": [{"tool": "run_shell", "cmd_pattern": "rm |del |format |shutdown |pay|支付|密码"}],
    }):
        assert auth_policy.decide("run_shell", {"cmd": "del a.txt"}) == "confirm"
        assert auth_policy.decide("run_shell", {"cmd": "shutdown /s"}) == "confirm"
        assert auth_policy.decide("run_shell", {"cmd": "echo hello"}) == "default"


@case("非法正则跳过该条且不抛")
def _():
    buf = io.StringIO()
    with _policy_file({
        "whitelist": [{"tool": "run_shell", "cmd_pattern": "([broken"}],
        "blacklist": [{"tool": "run_shell", "cmd_pattern": "*bad"}],
    }):
        with contextlib.redirect_stdout(buf):
            # 坏正则规则被跳过 → 不命中 → default；只打印警告不抛异常
            assert auth_policy.decide("run_shell", {"cmd": "anything"}) == "default"
    assert "正则不合法" in buf.getvalue()


@case("损坏 JSON 按空策略处理且不抛")
def _():
    buf = io.StringIO()
    with _policy_file("{ 这不是合法JSON ..."):
        with contextlib.redirect_stdout(buf):
            assert auth_policy.decide("run_shell", {"cmd": "dir"}) == "default"
    assert "加载失败" in buf.getvalue()


@case("工具名不匹配不命中")
def _():
    with _policy_file({"whitelist": [{"tool": "run_shell", "cmd_pattern": "记事本"}]}):
        assert auth_policy.decide("gui_control", {"task": "记事本"}) == "default"


@case("异常入参降级 default 不抛")
def _():
    with _policy_file(None):
        assert auth_policy.decide("run_shell", None) == "default"
        assert auth_policy.decide("run_shell", {"cmd": 123}) == "default"


# ==== brain 拦截处三档行为（mock hands 与 auth_policy） ====

def _import_brain_with_stubs():
    """stub 掉 brain 的全部外部依赖后导入 brain。"""
    for name in ("hands", "memory", "memory_v2", "episodic", "reminders", "triggers", "httpx"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    if "brain" in sys.modules:
        return importlib.reload(sys.modules["brain"])
    return importlib.import_module("brain")


class _FakeHands:
    """记录调用、按脚本返回结果的 hands 替身。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, tool, args):
        self.calls.append((tool, dict(args)))
        return self.results.pop(0) if self.results else "OK"


class _FakePolicy:
    """固定判定结果的 auth_policy 替身。"""

    def __init__(self, decision):
        self.decision = decision

    def decide(self, tool, args):
        return self.decision


@case("brain·default档：auth_policy 缺失时确认流程一字不差（死契约）")
def _():
    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = None  # 模拟模块缺失
    brain.hands = _FakeHands(["[[NEEDS_CONFIRM]] 需要确认"])
    reply = brain._execute_tool("gui_control", {"task": "在记事本里打字"})
    assert "您确认执行吗" in reply
    assert "接管您的鼠标和键盘" in reply  # 原文案逐字保留（高考 56 题确认话术）
    assert brain._pending_shell == {"tool": "gui_control", "args": {"task": "在记事本里打字"}}
    assert len(brain.hands.calls) == 1  # 未确认前绝不重放执行
    brain.auth_policy = None


@case("brain·default档：策略判定 default 时同样走现行流程")
def _():
    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = _FakePolicy("default")
    brain.hands = _FakeHands(["[[NEEDS_CONFIRM]] 需要确认"])
    reply = brain._execute_tool("run_shell", {"cmd": "dir"})
    assert reply == "先生，这条命令有一定风险：「dir」。您确认执行吗？"
    assert brain._pending_shell is not None
    brain.auth_policy = None


@case("brain·auto档：白名单放行免确认，confirmed=True 重放")
def _():
    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = _FakePolicy("auto")
    brain.hands = _FakeHands(["[[NEEDS_CONFIRM]] 需要确认", "好的，已在记事本输入。"])
    reply = brain._execute_tool("gui_control", {"task": "在记事本里打字"})
    assert reply == "好的，已在记事本输入。"
    assert len(brain.hands.calls) == 2
    assert brain.hands.calls[1][1].get("confirmed") is True  # 第二次必须带 confirmed=True
    assert brain._pending_shell is None  # 不挂起待确认状态机
    brain.auth_policy = None


@case("brain·confirm档：黑名单前置闸强制确认，hands 未被调用")
def _():
    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = _FakePolicy("confirm")
    brain.hands = _FakeHands(["这条命令本来可以直接执行"])
    reply = brain._execute_tool("run_shell", {"cmd": "del a.txt"})
    assert "您确认执行吗" in reply
    assert brain._pending_shell == {"tool": "run_shell", "args": {"cmd": "del a.txt"}}
    assert len(brain.hands.calls) == 0  # 执行前拦截，hands 根本没被调用
    brain.auth_policy = None


@case("brain·confirmed 重放跳过黑名单前置闸")
def _():
    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = _FakePolicy("confirm")
    brain.hands = _FakeHands(["已删除。"])
    reply = brain._execute_tool("run_shell", {"cmd": "del a.txt", "confirmed": True})
    assert reply == "已删除。"
    assert len(brain.hands.calls) == 1
    brain.auth_policy = None


@case("brain·策略模块异常降级为现行流程")
def _():
    class _BrokenPolicy:
        def decide(self, tool, args):
            raise RuntimeError("策略系统爆炸")

    brain = _import_brain_with_stubs()
    brain._pending_shell = None
    brain.auth_policy = _BrokenPolicy()
    brain.hands = _FakeHands(["[[NEEDS_CONFIRM]] 需要确认"])
    reply = brain._execute_tool("run_shell", {"cmd": "dir"})
    assert "您确认执行吗" in reply
    assert brain._pending_shell is not None
    brain.auth_policy = None


if __name__ == "__main__":
    print("== H3 分级授权 · 单元测试 ==")
    print(f"\n结果：{len(_PASSED)} 通过 / {len(_FAILED)} 失败")
    if _FAILED:
        for name, e in _FAILED:
            print(f"  失败: {name} -> {type(e).__name__}: {e}")
        sys.exit(1)
    print("全部通过。")
