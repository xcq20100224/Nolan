# -*- coding: utf-8 -*-
"""
T3 确认梯度 · 策略模块（auth_policy.py，由 H3 分级授权演化而来）

第一性原理动机 —— 信任的物理本质：按风险分级，不按一律。
「一律要确认」把无害操作（记事本打字）和危险操作（删文件/支付）收了同一笔
「不信任税」；「一律不确认」又把支付和删库交给了运气。得体的确认策略应当与
风险成正比，因此本模块实现三档确认梯度：

  直接做（auto）    —— 纯读取 / 可逆 / 低风险，免确认直接执行，做完顺手汇报；
  做完汇报（default）—— 走现行确认流程（hands 要确认就问、不要就执行），
                        执行后由话术层如实交代结果；
  先问一句（confirm）—— 破坏性 / 涉及真人 / 涉及资金，执行前必须亲口确认。

为什么梯度写在代码里而不是只写在策略文件里：H3 时代「缺策略文件 = 全部 default」
是为了接入期的绝对安全（零回退死契约）；T3 的梯度经过评估本身就是新的得体缺省，
因此内置为代码数据表，策略文件缺失 / 损坏时梯度依旧生效，且任何未识别情形仍
一律 default——死契约的内核（异常不抛、未识别不放宽、绝不解除 hands/VLM 硬编码
的密码/支付/删除安全禁令）原样保留。

策略文件：与本模块同目录的 auth_policy.json（可选，用于在内置梯度之上追加
用户自定义的白名单 / 黑名单规则；缺文件是合法形态，不警告）。
判定优先级（黑名单恒高于白名单，安全侧永远优先）：
  用户 JSON 黑名单 > 内置 confirm > 用户 JSON 白名单 > 内置 auto > default
正则不合法时跳过该条规则并打印警告，绝不抛异常。
"""

import json
import os
import re

# 策略文件路径（与本模块同目录；测试可 monkeypatch 本常量指向临时文件）
_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_policy.json")

# 判定结果三档
AUTO = "auto"        # 直接做：免确认直接执行
CONFIRM = "confirm"  # 先问一句：必须确认（即使该工具原本不需要确认）
DEFAULT = "default"  # 做完汇报 / 现行流程：未命中任何规则，行为原样

# ---------------------------------------------------------------------------
# 内置梯度规则表（代码内数据表；每档附「为什么」）
# ---------------------------------------------------------------------------

# 【直接做 · auto】为什么：这些工具要么只读（搜索/读文件/看时间/截屏），
# 要么写入范围被 hands 的文件柜沙盒锁死（write_file/make_ppt/edit_ppt），
# 要么只是打开应用（open_app，本身不改数据）——最坏结果都可逆、可重做，
# 收「不信任税」不得体。
_BUILTIN_AUTO_TOOLS = (
    "search_web",     # 联网搜索：纯读取
    "read_file",      # 读文件柜：纯读取
    "get_time",       # 查时间：纯读取
    "capture_screen", # 截屏：只读保存进文件柜
    "make_ppt",       # 做 PPT：只写文件柜沙盒，可重做
    "edit_ppt",       # 改 PPT：只改文件柜沙盒内的成品
    "write_file",     # 写文件：hands 已限定文件柜沙盒
    "open_app",       # 打开应用：不修改任何数据
)

# run_shell 只读白名单（梯度里唯一放新的口子）。
# 为什么：dir/echo/type/where/whoami/ipconfig/ver/date 这类命令只往 stdout 读信息，
# 不碰文件系统与系统状态；但带重定向（>）或写入参数的形态一律不算，
# 由下方黑名单先截住（confirm 优先于 auto）。
_BUILTIN_RUN_SHELL_READONLY_RE = re.compile(
    r"^\s*(?:dir|echo|type|where|whoami|ver)\b"   # 纯读取命令族
    r"|^\s*ipconfig(?:\s+/(?:all|displaydns))?\s*$"  # ipconfig 仅限只读开关（排除 /flushdns 等）
    r"|^\s*date(?:\s+/t)?\s*$",                      # date 仅限查看（/t 或无参），排除设日期
    re.IGNORECASE,
)

# 【先问一句 · confirm】为什么：以下形态最坏结果不可逆或涉及真人/资金——
# 删改系统、关机重启、写注册表、杀进程、向沙盒外写新文件、网络下载执行、
# 界面层支付转账、把内容发给真人。这些必须先生亲口点头。
_BUILTIN_RUN_SHELL_DENY_RE = re.compile(
    r"\b(?:del|erase|rd|rmdir|rm|format|shutdown|restart|reboot|logoff|"
    r"taskkill|tskill|reg|regedit|diskpart|bcdedit|takeown|icacls|cipher)\b"
    r"|\bnet\s+(?:user|localgroup|share)\b"      # 账户/共享变更
    r"|\bsc\s+(?:delete|stop|config)\b"          # 服务变更
    r"|\bschtasks\s+/delete\b"                   # 删计划任务
    r"|\b(?:copy|move|xcopy|robocopy|ren|rename|mkdir|md)\b"  # 沙盒外新写入路径
    r"|\b(?:curl|wget|bitsadmin|certutil|powershell|pwsh)\b"  # 下载/任意脚本执行
    r"|\bipconfig\s+/(?:flushdns|release|renew|registerdns|setclassid)\b"  # 网络配置写操作
    r"|[>]",                                     # 重定向 = 写文件（含 >>）
    re.IGNORECASE,
)

# gui_control 涉及资金 / 发给真人 / 删除关系人的任务关键词（命中 task 文本即确认）
_BUILTIN_GUI_DENY_RE = re.compile(
    r"支付|付款|转账|发红包|发送给|删除好友|删除联系人"
)

# 文件传输助手的别名表（wechat_send_file 目标判定用；全部小写比较）
_WECHAT_SELF_TARGETS = {"文件传输助手", "filehelper"}

# 【做完汇报 · default】为什么：这一档不写入任何规则表——default 是「未命中」的
# 自然落点。发文件给文件传输助手（发给自己）不构成对外触达，media_control
# （播放/暂停/音量）随时可逆；两者维持现行流程，执行后由话术层如实交代即可。
# 登记在此仅为文档化梯度意图，供测试与人工审计对照：
_TIER_DEFAULT_NOTES = {
    "wechat_send_file": "目标是文件传输助手（发给自己）：做完汇报即可；发真人见 confirm 档",
    "media_control": "播放/暂停/音量等即时可逆操作：做完汇报即可",
}

_policy_cache: dict | None = None  # None 表示尚未加载；{} 表示空策略/加载失败


def _reset_cache() -> None:
    """清空策略缓存（仅供测试使用）。"""
    global _policy_cache
    _policy_cache = None


def _load_policy() -> dict:
    """
    加载用户策略文件，结果缓存。
    文件不存在 / JSON 损坏 / 结构不符，一律按空策略处理并打印警告（不存在时不警告，
    因为缺文件是合法的默认形态）。绝不抛异常。
    注意：空策略不等于「全部 default」——内置梯度规则表照常生效（T3 起）。
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


def _builtin_confirm(tool: str, args: dict) -> bool:
    """内置「先问一句」判定。任何异常返回 False（交由外层兜底为 default）。"""
    if tool == "run_shell":
        cmd = args.get("cmd")
        if isinstance(cmd, str) and _BUILTIN_RUN_SHELL_DENY_RE.search(cmd):
            return True
        return False
    if tool == "gui_control":
        task = args.get("task")
        if isinstance(task, str) and _BUILTIN_GUI_DENY_RE.search(task):
            return True
        return False
    if tool == "wechat_send_file":
        # 目标是真人（非文件传输助手）才需要确认；缺省目标 = 文件传输助手
        target = str(args.get("target") or "").strip().lower()
        return bool(target) and target not in _WECHAT_SELF_TARGETS
    return False


def _builtin_auto(tool: str, args: dict) -> bool:
    """内置「直接做」判定。任何异常返回 False（交由外层兜底为 default）。"""
    if tool in _BUILTIN_AUTO_TOOLS:
        return True
    if tool == "run_shell":
        cmd = args.get("cmd")
        if isinstance(cmd, str) and _BUILTIN_RUN_SHELL_READONLY_RE.search(cmd):
            return True
    return False


def decide(tool: str, args: dict) -> str:
    """
    三档判定，返回 "auto" / "confirm" / "default"。
    优先级：用户 JSON 黑名单 > 内置 confirm > 用户 JSON 白名单 > 内置 auto > default。
    （黑名单恒在白名单之前：安全侧永远优先，用户白名单无权压过内置危险拦截。）
    任何内部异常一律降级为 "default"，保证坏配置、坏正则、模块故障都不会
    改变现行确认流程（零回退死契约：异常不抛、未识别不放宽）。
    """
    try:
        policy = _load_policy()
        args = args or {}
        for rule in policy.get("blacklist", []):
            if _rule_matches(rule, tool, args):
                return CONFIRM
        if _builtin_confirm(tool, args):
            return CONFIRM
        for rule in policy.get("whitelist", []):
            if _rule_matches(rule, tool, args):
                return AUTO
        if _builtin_auto(tool, args):
            return AUTO
        return DEFAULT
    except Exception as e:  # noqa: BLE001 - 兜底防线：策略系统自身故障不影响主流程
        print(f"[auth_policy] 警告：判定异常，按 default 处理（{e}）")
        return DEFAULT
