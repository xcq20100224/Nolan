# -*- coding: utf-8 -*-
"""
Nolan · 主动交代（debrief.py）

为什么存在：Nolan 做完长任务（如生成 PPT）只说一句结果，先生不知道它
做了什么、去哪检查。信任来自可验证——长任务成功后，自动附一段「主动
交代」：我做了哪些步骤 + 您可以去哪里验证。

设计三原则（与 progress 总线同源）：
  1. 绝不抛异常——任何意外一律返回 None（不附交代），绝不能拖垮主流程；
  2. 纯函数零 IO——只读入参与 progress.run_steps()，不写文件、不联网；
  3. 失败不画蛇添足——结果以「抱歉」开头（失败/不可用）或不是文本时，
     返回 None：失败话术另有分工，交代只出现在成功之后。

API 契约：
    note(tool, args, result) -> str | None
        仅对长任务工具生效（make_ppt / edit_ppt / write_file /
        wechat_send_file / run_shell / gui_control），其余返回 None。
        成功时返回 2-3 句管家口吻的交代文本，调用方拼接到结果话术尾部。
"""
from __future__ import annotations

import re

import progress

# 长任务工具白名单：只有这些工具完成后才值得主动交代
_LONG_TOOLS = {
    "make_ppt",
    "edit_ppt",
    "write_file",
    "wechat_send_file",
    "run_shell",
    "gui_control",
}

# 从成功话术里提取文件名：「文件名 X.pptx」式样（与 hands._make_ppt 话术对齐）
_FILE_RE = re.compile(r"文件名[「\s]*([^\s，。」]+)")
# write_file 话术是「写进文件『X』」，用书名号兜底提取
_BRACKET_RE = re.compile(r"「([^」]+)」")

# 无进度步骤时的兜底概括（按工具给一句人话）
_FALLBACK_DID = {
    "make_ppt": "我完成了一轮完整的 PPT 制作流程",
    "edit_ppt": "我按您的要求完成了这份 PPT 的修改",
    "write_file": "我把内容逐字写进了文件，并做了写后校验",
    "wechat_send_file": "我把文件通过微信发了出去",
    "run_shell": "我执行了您交代的命令",
    "gui_control": "我在屏幕上逐步完成了您交代的操作",
}

# 步骤链路总长上限（契约 ≤60 字）
_MAX_CHAIN_CHARS = 60
# 链路超长时取代表性节点，单节点再截断的上限
_MAX_NODE_CHARS = 18

# 原始埋点文本 → 规范节点名：「正在精写第 2/3 页：精力应聚焦于…」这种
# 带页码与标题碎片的原文直接进链路不可读，先归一成短名（关键词命中即归一）
_STEP_CANON = (
    ("查资料", "联网查资料"),
    ("资料就绪", "联网查资料"),
    ("大纲", "设计大纲"),
    ("精写", "逐页精写"),
    ("重写", "质量重写"),
    ("配图", "生成配图"),
    ("排版", "排版渲染"),
    ("存档", "存档完成"),
)


def _canon_step(s: str) -> str:
    """单步文本规范化：去「正在…」语态，按关键词归一为短节点名。"""
    t = str(s or "").strip()
    for prefix in ("正在",):
        if t.startswith(prefix):
            t = t[len(prefix):]
    t = t.rstrip("…。").strip()
    for key, canon in _STEP_CANON:
        if key in t:
            return canon
    return t[:_MAX_NODE_CHARS]


def _steps_chain(steps) -> str:
    """把步骤列表压缩成「联网查资料→设计大纲→排版存档」式链路。

    先逐步规范化（去语态/归一短名），相邻去重；总长 ≤60 字；
    步骤太多时取首/中/尾代表性节点。任何意外返回空串（调用方走兜底概括）。
    """
    try:
        cleaned = []
        for s in steps or []:
            c = _canon_step(s)
            if c and (not cleaned or cleaned[-1] != c):
                cleaned.append(c)
        if not cleaned:
            return ""
        chain = "→".join(cleaned)
        if len(chain) <= _MAX_CHAIN_CHARS:
            return chain
        # 步骤太多：取首/中/尾代表性节点（去重保序）
        picks = []
        for idx in (0, len(cleaned) // 2, len(cleaned) - 1):
            if cleaned[idx] not in picks:
                picks.append(cleaned[idx])
        chain = "→".join(picks)
        if len(chain) <= _MAX_CHAIN_CHARS:
            return chain
        # 节点本身太长：逐节点截断后再拼
        return "→".join(p[:_MAX_NODE_CHARS] for p in picks)[:_MAX_CHAIN_CHARS]
    except Exception:
        return ""


def _extract_file_name(result: str, args: dict) -> str:
    """从结果话术提取文件名；提取不到时用 args 里的文件名兜底。"""
    name = ""
    try:
        m = _FILE_RE.search(result)
        if m:
            name = m.group(1).strip()
        if not name:
            m = _BRACKET_RE.search(result)
            if m:
                name = m.group(1).strip()
    except Exception:
        name = ""
    if not name:
        try:
            name = str((args or {}).get("file_name")
                       or (args or {}).get("name") or "").strip()
        except Exception:
            name = ""
    return name


def _where_to_check(tool: str, args: dict, result: str) -> str:
    """「您可以在哪里检查」——按工具指明验证去处。"""
    if tool in ("make_ppt", "edit_ppt"):
        name = _extract_file_name(result, args)
        if name:
            return f"您可以在文件柜里打开 {name} 检查，每页的备注栏里都附有演讲稿。"
        return "您可以到文件柜里查看成品，每页的备注栏里都附有演讲稿。"
    if tool == "write_file":
        name = _extract_file_name(result, args)
        if name:
            return f"您可以在文件柜里打开 {name} 核对内容。"
        return "您可以到文件柜里核对刚写入的文件。"
    if tool == "wechat_send_file":
        try:
            target = str((args or {}).get("target") or "").strip() or "文件传输助手"
        except Exception:
            target = "文件传输助手"
        return f"您可以在微信「{target}」的聊天记录里确认文件已经送达。"
    if tool == "run_shell":
        return "您可以在屏幕上看到刚才命令的实际执行效果。"
    if tool == "gui_control":
        return "您可以看一眼刚才的窗口，确认屏幕上的实际效果。"
    return ""


def note(tool: str, args: dict, result) -> "str | None":
    """长任务成功后生成「主动交代」文本；不适用/失败/意外一律返回 None。"""
    try:
        if tool not in _LONG_TOOLS:
            return None
        if args is not None and not isinstance(args, dict):
            return None  # 入参形态意外：按契约静默退场
        if not isinstance(result, str) or not result.strip():
            return None
        # 失败/不可用/待确认：另有专门话术，不画蛇添足
        if result.startswith("抱歉") or result.startswith("[[NEEDS_CONFIRM]]"):
            return None
        # ①我做了什么：优先用进度总线的真实步骤链路，无步骤则按工具概括
        try:
            steps = progress.run_steps()
        except Exception:
            steps = []
        chain = _steps_chain(steps)
        if chain:
            did = f"我按 {chain} 的流程完成了这项任务"
        else:
            did = _FALLBACK_DID.get(tool, "我完成了这项任务")
        # ②您可以在哪里检查
        where = _where_to_check(tool, args or {}, result)
        if not where:
            return None
        return f"先生，跟您交代一下：{did}。{where}"
    except Exception:
        return None
