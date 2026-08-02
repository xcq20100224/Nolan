# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 长期记忆模块（memory.py）· 阶段三
职责：把用户明确要求记住的事实写入本地文本文件，并能口语化地复述与删除。

第一性原理：不做向量检索、不做自动摘要——记忆就是一行行带时间戳的纯文本，
加载时原文交给大脑注入 prompt，删除时按关键字整行移除。

存储：jarvis\\memory\\long_term.txt（用 __file__ 定位，目录自动创建）
格式：UTF-8，每行一条『[YYYY-MM-DD HH:MM] 事实内容』

接口契约（签名一字不差）：
    def load() -> str               # 返回全部长期记忆原文；无记忆返回空字符串；永不抛异常
    def remember(fact: str) -> str  # 追加一条记忆，返回口语化确认文本
    def recall() -> str             # 口语化列出全部记忆（供播报）
    def forget(keyword: str) -> str # 删除含 keyword 的记忆，返回口语化结果
"""

import os
from datetime import datetime

# == 常量与配置 ==

_MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_MEMORY_FILE = os.path.join(_MEMORY_DIR, "long_term.txt")
_ENCODING = "utf-8"

# 中文数字（用于口语化计数与列举，超出范围时退回阿拉伯数字）
_CN_NUMS = ["零", "一", "两", "三", "四", "五", "六", "七", "八", "九", "十"]


def _cn_num(n: int) -> str:
    """把非负整数转成口语化中文数字（0~10 用汉字，更大用阿拉伯数字）。"""
    if 0 <= n < len(_CN_NUMS):
        return _CN_NUMS[n]
    return str(n)


# == 内部读写原语（全部 try/except 兜底，文件损坏/不存在不崩溃）==

def _read_lines() -> list:
    """读出全部记忆行（去掉空行）；文件不存在或读取失败返回空列表。"""
    try:
        if not os.path.exists(_MEMORY_FILE):
            return []
        with open(_MEMORY_FILE, "r", encoding=_ENCODING, errors="replace") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except Exception:
        return []


def _write_lines(lines: list) -> bool:
    """把记忆行整体写回文件（目录自动创建）；写失败返回 False。"""
    try:
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        with open(_MEMORY_FILE, "w", encoding=_ENCODING) as f:
            for line in lines:
                f.write(line + "\n")
        return True
    except Exception:
        return False


def _append_line(line: str) -> bool:
    """追加一行记忆（目录自动创建）；写失败返回 False。"""
    try:
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        with open(_MEMORY_FILE, "a", encoding=_ENCODING) as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


# == 契约接口 ==

# 记忆分类（J5 结构化）：remember 时按关键词自动归类，recall 时分组播报。
# 同伴的本质是「懂」——分类让记忆从流水账变成对主人的结构化理解。
_CATEGORY_RULES = (
    ("偏好", ("喜欢", "不爱", "讨厌", "偏好", "常喝", "爱吃", "最爱")),
    ("习惯", ("每天", "经常", "通常", "习惯", "一般", "总是", "每周")),
    ("工作", ("项目", "会议", "工作", "评审", "同事", "客户", "汇报", "deadline", "截止")),
    ("人际", ("朋友", "家人", "父亲", "母亲", "老婆", "老公", "孩子", "叫", "名字")),
)
_DEFAULT_CATEGORY = "事实"


def _classify(fact: str) -> str:
    """按关键词把一条记忆归类；都不命中归入「事实」。"""
    for tag, keys in _CATEGORY_RULES:
        if any(k in fact for k in keys):
            return tag
    return _DEFAULT_CATEGORY


def _strip_tag(line: str) -> str:
    """去掉行内的【类别】标签，返回纯内容（兼容旧格式无标签行）。"""
    import re as _re
    return _re.sub(r"【[^】]+】", "", line)


def load() -> str:
    """返回全部长期记忆原文（供大脑注入 prompt）；无记忆返回空字符串；永不抛异常。"""
    try:
        return "\n".join(_read_lines())
    except Exception:
        return ""


def remember(fact: str) -> str:
    """追加一条记忆（自动归类），返回口语化确认文本；空内容返回提示。"""
    try:
        fact = (fact or "").strip()
        if not fact:
            return "抱歉先生，我没有听清要记住的内容，请您再说一遍。"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag = _classify(fact)
        if _append_line(f"[{stamp}]【{tag}】{fact}"):
            return f"好的先生，我已经记住：{fact}"
        return "抱歉先生，记忆写入文件时出了点问题，这条恐怕没存下来。"
    except Exception:
        return "抱歉先生，记忆功能暂时出了点状况，这条没记住。"


def recall() -> str:
    """按类别分组、口语化列出全部记忆（供播报）。"""
    try:
        lines = _read_lines()
        if not lines:
            return "抱歉先生，我目前还不了解您，您可以对我说「记住……」"
        groups: dict = {}
        order: list = []  # 保持首次出现顺序的类别列表
        for line in lines:
            import re as _re
            m = _re.search(r"【([^】]+)】", line)
            tag = m.group(1) if m else _DEFAULT_CATEGORY
            if tag not in groups:
                groups[tag] = []
                order.append(tag)
            groups[tag].append(_strip_tag(line))
        parts = []
        idx = 0  # 全局连续编号（跨类别不重置，保持播报可指认「第几件」）
        for tag in order:
            body_items = []
            for it in groups[tag]:
                idx += 1
                body_items.append(f"{_cn_num(idx)}、{it.split(']', 1)[-1].strip()}")
            parts.append(f"{tag}方面：" + "；".join(body_items))
        return f"先生，关于您我目前记得{_cn_num(len(lines))}件事。" + "。".join(parts) + "。"
    except Exception:
        return "抱歉先生，读取记忆时出了点状况，暂时想不起来了。"


def forget(keyword: str) -> str:
    """删除所有含 keyword 的记忆行，返回删除了几条的口语化结果。"""
    try:
        keyword = (keyword or "").strip()
        if not keyword:
            return "抱歉先生，我没有听清要忘掉的关键词，请您再说一遍。"
        lines = _read_lines()
        kept = [line for line in lines if keyword not in line]
        removed = len(lines) - len(kept)
        if removed == 0:
            return f"先生，我的记忆里没有找到包含「{keyword}」的内容，也许是您记错了。"
        if _write_lines(kept):
            return f"好的先生，我已经忘掉了{_cn_num(removed)}条与「{keyword}」有关的记忆。"
        return "抱歉先生，更新记忆文件时出了点问题，删除没有生效。"
    except Exception:
        return "抱歉先生，删除记忆时出了点状况，暂时无法操作。"


# == 独立自检（不依赖其他模块）==

if __name__ == "__main__":
    print("memory.py 独立自检：")
    print("  recall ->", recall())
    print("  remember ->", remember("自检：先生喜欢黑咖啡"))
    print("  recall ->", recall())
    print("  forget ->", forget("自检"))
    print("  load 长度 ->", len(load()))
