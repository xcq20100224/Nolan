# -*- coding: utf-8 -*-
"""
H3 分级授权 · 策略加载模块（auth_policy.py）

第一性原理动机 —— 信任的物理本质：按风险分级，不按一律。
「一律要确认」把无害操作（记事本打字）和危险操作（删文件/支付）收了同一笔
「不信任税」；而真正的信任模型应当与风险成正比：白名单内的低风险操作自主放行，
黑名单内的高风险操作绝不动摇必须确认，其余一切保持现行默认流程。
分级授权因此只管「确认流程」这一层：它能免除确认、能追加确认，
但绝不能解除 VLM prompt 里硬编码的密码/支付/删除安全禁令——那是更底层的闸，
任何策略文件都无权触碰。

策略文件：与本模块同目录的 auth_policy.json（缺文件 = 空策略 = 全部走现行确认流程，
行为与未接入本模块时一字不差，这是默认零回退死契约）。
结构：
{
  "whitelist": [{"tool": "gui_control", "app_pattern": "记事本|notepad"},
                {"tool": "run_shell", "cmd_pattern": "^dir\\b"}],
  "blacklist": [{"tool": "run_shell", "cmd_pattern": "rm |del |format |shutdown |pay|支付|密码"}]
}
判定优先级：黑名单 > 白名单 > 默认。正则不合法时跳过该条规则并打印警告，绝不抛异常。
"""

import json
import os
import re

# 策略文件路径（与本模块同目录；测试可 monkeypatch 本常量指向临时文件）
_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_policy.json")

# 判定结果三档
AUTO = "auto"        # 白名单命中：免确认直接执行
CONFIRM = "confirm"  # 黑名单命中：必须确认（即使该工具原本不需要确认）
DEFAULT = "default"  # 未命中任何规则：走现行确认流程，行为原样

_policy_cache: dict | None = None  # None 表示尚未加载；{} 表示空策略/加载失败


def _reset_cache() -> None:
    """清空策略缓存（仅供测试使用）。"""
    global _policy_cache
    _policy_cache = None


def _load_policy() -> dict:
    """
    加载策略文件，结果缓存。
    文件不存在 / JSON 损坏 / 结构不符，一律按空策略处理并打印警告（不存在时不警告，
    因为缺文件是合法的默认形态）。绝不抛异常。
    """
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache
    if not os.path.exists(_POLICY_PATH):
        _policy_cache = {}
        return _policy_cache
    try:
        with open(_POLICY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("策略文件顶层必须是 JSON 对象")
        wl = data.get("whitelist") or []
        bl = data.get("blacklist") or []
        if not isinstance(wl, list) or not isinstance(bl, list):
            raise ValueError("whitelist / blacklist 必须是数组")
        _policy_cache = {"whitelist": wl, "blacklist": bl}
    except Exception as e:  # noqa: BLE001 - 任何加载异常都降级为空策略
        print(f"[auth_policy] 警告：策略文件加载失败，按空策略处理（{e}）")
        _policy_cache = {}
    return _policy_cache


def _rule_matches(rule: dict, tool: str, args: dict) -> bool:
    """
    判断单条规则是否命中。tool 必须一致；所有 *_pattern 键为 AND 关系。
    正则不合法：打印警告并跳过该条规则（返回 False），绝不抛。
    约定 app_pattern 同时匹配 args["app"] 与 args["task"]（gui_control 只有 task）。
    """
    if not isinstance(rule, dict) or rule.get("tool") != tool:
        return False
    patterns = [(k, v) for k, v in rule.items() if k.endswith("_pattern")]
    if not patterns:
        return True  # 无模式键的规则：对该工具全部生效
    compiled = []
    for key, pat in patterns:
        try:
            compiled.append((key, re.compile(str(pat))))
        except re.error as e:
            print(f"[auth_policy] 警告：规则正则不合法，已跳过该条（{pat!r}: {e}）")
            return False
    for key, rx in compiled:
        base = key[: -len("_pattern")]
        candidates = [args.get(base)]
        if base == "app":
            candidates.append(args.get("task"))
        if not any(isinstance(c, str) and rx.search(c) for c in candidates):
            return False
    return True


def decide(tool: str, args: dict) -> str:
    """
    三档判定：黑名单 > 白名单 > 默认。
    返回 "auto" / "confirm" / "default"。任何内部异常一律降级为 "default"，
    保证缺文件、坏配置、坏正则都不会改变现行确认流程（默认零回退契约）。
    """
    try:
        policy = _load_policy()
        args = args or {}
        for rule in policy.get("blacklist", []):
            if _rule_matches(rule, tool, args):
                return CONFIRM
        for rule in policy.get("whitelist", []):
            if _rule_matches(rule, tool, args):
                return AUTO
        return DEFAULT
    except Exception as e:  # noqa: BLE001 - 兜底防线：策略系统自身故障不影响主流程
        print(f"[auth_policy] 警告：判定异常，按 default 处理（{e}）")
        return DEFAULT
