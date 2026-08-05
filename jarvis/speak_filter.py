# -*- coding: utf-8 -*-
"""
Nolan 说话卫生模块（speak_filter.py）· 可念性过滤器（纯函数，零副作用）

第一性原理动机：耳朵是最贵的信道。眼睛可以一眼跳过代码块，
听觉只能线性地、不可逆地忍受每一个字——把 JSON、PowerShell、
文件路径、base64 念出来，等于拿先生不可逆的生命时长播噪音。
代码和 JSON 是 Nolan 的思考，不是台词；Nolan 的嘴只说人话。

本模块不碰任何 I/O，供三处接线共用同一套「不可念内容」标准：
  - server synth_for / 流式 sentence 前：t = speakable(reply) or 兜底话术
  - brain think 返回前：可见与可念同一标准（max_chars=None，不截断可见文本）
  - CLI speak 前：同上
"""

import json
import re

__all__ = ["speakable", "is_speakable"]

# 长回复截断参数：耳朵带宽有限，长文念重点，细节请先生看屏幕
_HARD_LIMIT = 200   # 可念文本硬上限（字）
_SOFT_LIMIT = 160   # 截断时在句读处收口的软上限
_TAIL = "……详细内容我放在屏幕上了。"

# 有效字符门槛：剥离后剩下的中文字/字母/数字不足 4 个，视为「没啥可念」
_MIN_EFFECTIVE = 4

# == 各类「不可念内容」的模式 ==

# markdown 代码栅栏块：```lang ... ```（DOTALL，跨行）
_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_TILDE_FENCE_RE = re.compile(r"~~~[^\n]*\n.*?~~~", re.DOTALL)
# 孤立栅栏标记行（未闭合围栏的残余开头，如 ```json）
_FENCE_MARK_LINE_RE = re.compile(r"^```[^\n]*$", re.MULTILINE)

# 行内代码 `...`（不跨行）
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# URL：http(s):// 或 www. 开头，到空白或中文句读为止
_URL_RE = re.compile(r"https?://\S+|www\.[^\s，。！？；：「」『』“”]+")

# Windows 绝对路径（C:\...）与本仓库常见相对路径（jarvis\... 等）
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。！？；：「」『』“”]*")
_REL_PATH_RE = re.compile(
    r"\b(?:jarvis|nolan-web|tools|dist|launch|node_modules)\\[^\s，。！？；：「」『』“”]*",
    re.IGNORECASE)

# base64 / 哈希串：40 个以上的 base64 字母表连续字符（正常口语不会出现）
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# shell / PowerShell 命令行：行首是常见 shell 或命令关键字，整行不可念
_SHELL_LINE_RE = re.compile(
    r"^\s*(?:powershell|pwsh|cmd(?:\.exe)?(?:\s|/|$)|bash\b|sh\b"
    r"|add-type\b|invoke-\w+|set-\w+|new-object\b|get-\w+|\$\w+\s*=|[A-Za-z]:\\?>)",
    re.IGNORECASE)

# 工具 JSON 残影：{"tool": ... / 'tool': ... ——用于检测未被平衡扫描捕获的
# 残缺工具 JSON（如 LLM 输出被截断、花括号不闭合），做最后截肢
_TOOL_HINT_RE = re.compile(r"[\{\"']\s*tool[\"']\s*:", re.IGNORECASE)

# 有效字符：中日韩统一表意文字 + 拉丁字母 + 数字
_EFFECTIVE_RE = re.compile(r"[一-鿿぀-ヿ㐀-䶿A-Za-z0-9]")

# 连续空白（含换行）折叠：口语文本不需要排版空白
_WS_RE = re.compile(r"\s+")


def _strip_fences(text: str) -> str:
    """剥离 markdown 代码块（``` 与 ~~~ 围栏）；未闭合围栏从标记处截到文末。

    未闭合意味着 LLM 输出被截断，围栏后面只会是残缺的代码，没有可念价值；
    顺带删掉孤立的栅栏标记行（如残留的一行 ```json）。
    """
    text = _FENCE_RE.sub(" ", text)
    text = _TILDE_FENCE_RE.sub(" ", text)
    # 未闭合围栏：从第一个残余 ``` 起全部截肢
    idx = text.find("```")
    if idx >= 0:
        text = text[:idx]
    idx = text.find("~~~")
    if idx >= 0:
        text = text[:idx]
    return text


def _balanced_spans(text: str, opener: str, closer: str):
    """对 text 做括号平衡扫描（字符串内转义感知），产出每个顶层平衡跨段
    (start, end)——end 为闭括号之后的下标。引号字符串内的括号不计数。"""
    spans = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, c in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            if depth == 0:
                start = i
            depth += 1
        elif c == closer and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
                start = -1
    return spans


def _looks_jsonish(fragment: str) -> bool:
    """判断一段括号文本是否像 JSON：能解析为对象/数组，
    或含 "key": 形态的键值对（覆盖解析失败的残缺工具 JSON）。"""
    try:
        json.loads(fragment, strict=False)
        return True
    except ValueError:
        pass
    return bool(re.search(r"[{\"']\s*[\w一-鿿]+[\"']\s*:", fragment))


def _strip_json_spans(text: str) -> str:
    """剥离 JSON 对象/数组片段：花括号与中括号各做一遍平衡扫描，
    只删「像 JSON」的平衡跨段（正常中文行文不含 {}，误伤面极小）。"""
    spans = []
    for opener, closer in (("{", "}"), ("[", "]")):
        for s, e in _balanced_spans(text, opener, closer):
            fragment = text[s:e]
            # 中括号收紧判定：只有含引号/嵌套括号才算 JSON，
            # 避免把行文里的「步骤[1]」误当数组剥掉
            if opener == "[" and ('"' not in fragment and "{" not in fragment):
                continue
            if _looks_jsonish(fragment):
                spans.append((s, e))
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + " " + text[e:]
    return text


def _amputate_tool_residue(text: str) -> str:
    """工具 JSON 截肢兜底：平衡扫描后仍检测到 "tool": 残影
    （LLM 输出截断、引号错乱导致花括号不闭合），从最早一个 { 起
    截到文末——事故现场口语在前、JSON 在后，截肢后正好留下口播开场白。"""
    if not _TOOL_HINT_RE.search(text):
        return text
    brace = text.find("{")
    return text[:brace] if brace >= 0 else ""


def _strip_shell_lines(text: str) -> str:
    """逐行检查：shell / PowerShell 命令行整行剥离。"""
    kept = [ln for ln in text.split("\n") if not _SHELL_LINE_RE.match(ln)]
    return "\n".join(kept)


def _effective_count(text: str) -> int:
    """有效字符数：中文字、字母、数字的个数（标点空白不算内容）。"""
    return len(_EFFECTIVE_RE.findall(text))


def _truncate(text: str) -> str:
    """口语化截断：硬上限 200 字；超长时取前半，在最近句读处收口，
    接上「详细内容我放在屏幕上了」——长文念重点，细节请先生看屏幕。"""
    if len(text) <= _HARD_LIMIT:
        return text
    head = text[:_SOFT_LIMIT]
    cut = max(head.rfind(p) for p in "。！？；")
    if cut >= 40:  # 句读位置太靠前说明文本结构碎，直接硬切更稳
        head = head[:cut + 1]
    return head.rstrip() + _TAIL


def speakable(text: str, max_chars: int | None = _HARD_LIMIT) -> str:
    """把任意回复文本过滤成「适合念给先生听」的版本。

    剥离顺序：代码围栏 -> JSON 片段 -> 工具 JSON 残影截肢 -> shell 命令行
    -> 行内代码 -> URL -> 文件路径 -> base64 -> 空白折叠。
    剥离后有效字符不足 4 个返回 ""（调用方用通用话术兜底）。

    参数 max_chars：口语化截断上限，默认 200；传 None 不截断
    （brain 出口闸用它——可见文本要保全文，只剥不可念内容，不砍长度）。
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    out = text
    out = _strip_fences(out)
    out = _strip_json_spans(out)
    out = _amputate_tool_residue(out)
    out = _strip_shell_lines(out)
    out = _INLINE_CODE_RE.sub(" ", out)
    out = _URL_RE.sub(" ", out)
    out = _WIN_PATH_RE.sub(" ", out)
    out = _REL_PATH_RE.sub(" ", out)
    out = _BASE64_RE.sub(" ", out)
    # 空白折叠：多行变单行、连续空白变一个空格
    out = _WS_RE.sub(" ", out).strip()
    if _effective_count(out) < _MIN_EFFECTIVE:
        return ""
    if max_chars is not None:
        out = _truncate(out)
    return out


def is_speakable(text: str) -> bool:
    """快速判断这段文本值不值得念：过滤后还有有效口语内容才算。"""
    return bool(speakable(text, max_chars=None))
