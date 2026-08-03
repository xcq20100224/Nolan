# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 主动提醒模块（reminders.py）· 阶段四
职责：解析中文时间表达、存储提醒、到点弹出。

第一性原理：不做重复规则、不做铃声、不做后台调度——提醒就是一行
『时间|内容』，大脑每轮循环问一次 check_due()，到点就播报并删除。

存储：jarvis\\memory\\reminders.txt（用 __file__ 定位，目录自动创建）
格式：UTF-8，每行『YYYY-MM-DDTHH:MM|内容』

接口契约（签名一字不差，被 brain / GUI / 测试依赖）：
    def add(raw: str) -> str              # raw = 「提醒我」之后的原文；返回口语化确认或引导语
    def list_pending() -> str             # 口语化列出未来提醒；无提醒返回固定话术
    def check_due() -> list[str]          # 弹出所有到点提醒（从存储移除）；无到点返回 []
    def parse_time(text: str) -> datetime | None  # 中文时间表达式解析
"""

import os
import re
import threading
from datetime import datetime, timedelta

# == 常量与配置 ==

_MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_REMINDERS_FILE = os.path.join(_MEMORY_DIR, "reminders.txt")
_ENCODING = "utf-8"

# 存储串行化：/api/due 每 15 秒读改写 vs brain 提醒意图写，并发会互相吃掉
# 提醒——所有对外接口的读写临界区（读-改-写）都收进同一把锁。
_STORE_LOCK = threading.Lock()

# 中文数字（1~99，含「两」）
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 数字词：阿拉伯数字或中文数字（1~3 个汉字足够覆盖 1~99）
_NUM = r"(?:\d+|[零一二两三四五六七八九十]{1,3})"

# 时间表达式正则（用于在原文开头提取最长的前缀）
_RE_REL = re.compile(rf"^(?:\s*({_NUM})\s*(分钟|小时|秒钟|秒)后)")
_RE_ABS = re.compile(
    rf"^\s*(?:(今天|明天|后天)\s*)?"
    rf"(?:(早上|上午|中午|下午|晚上)\s*)?"
    rf"({_NUM})\s*点"
    rf"(?:\s*(半)|\s*({_NUM})\s*分?)?"
)


# == 基础小工具 ==

def _cn_to_int(s: str):
    """把阿拉伯数字或 1~99 的中文数字（含十/两）转成 int；不合法返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1   # 「十五」= 15
        ones = _CN_DIGITS.get(right, 0) if right else 0  # 「二十」= 20
        if (left and left not in _CN_DIGITS) or (right and right not in _CN_DIGITS):
            return None
        return tens * 10 + ones
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    return None


def _day_period(dt: datetime) -> str:
    """按小时给出口语化时段词，用于播报确认。"""
    h = dt.hour
    if h < 6:
        return "凌晨"
    if h < 9:
        return "早上"
    if h < 12:
        return "上午"
    if h == 12:
        return "中午"
    if h < 18:
        return "下午"
    return "晚上"


def _hour12(dt: datetime) -> int:
    """24 小时制转 12 小时制口语钟点。"""
    h = dt.hour % 12
    return h if h else 12


def _spoken_time(dt: datetime) -> str:
    """把时刻转成口语化片段：『X月X日上午九点整』『3月5日晚上七点半』。"""
    minute = f"{dt.minute}分" if dt.minute else "整"
    if dt.minute == 30:
        minute = "半"
    return f"{dt.month}月{dt.day}日{_day_period(dt)}{_hour12(dt)}点{minute}"


# == 存储原语（全部 try/except 兜底）==

def _read_entries() -> list:
    """读出全部提醒，返回 [(datetime, 内容), ...]；坏行跳过，文件不存在返回 []。"""
    entries = []
    try:
        if not os.path.exists(_REMINDERS_FILE):
            return []
        with open(_REMINDERS_FILE, "r", encoding=_ENCODING, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                ts, _, content = line.partition("|")
                content = content.strip()
                try:
                    when = datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M")
                except ValueError:
                    continue
                if content:
                    entries.append((when, content))
    except Exception:
        return []
    return entries


def _write_entries(entries: list) -> bool:
    """把 [(datetime, 内容), ...] 写回存储文件；目录自动创建；失败返回 False。"""
    try:
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        with open(_REMINDERS_FILE, "w", encoding=_ENCODING) as f:
            for when, content in entries:
                f.write(f"{when.strftime('%Y-%m-%dT%H:%M')}|{content}\n")
        return True
    except Exception:
        return False


# == 时间解析 ==

def parse_time(text: str):
    """
    解析中文时间表达式，返回 datetime；全部不匹配返回 None。
    支持：
      『X分钟后』『X小时后』（X 为阿拉伯数字或 1~99 中文数字）
      『（今天|明天|后天）（早上|上午|中午|下午|晚上）X点（半|Y分）』
        - X 支持阿拉伯/中文数字
        - 下午/晚上且 X<12 则 +12；中午固定 12 点；分钟默认 0
        - 未指明日期且今天已过点 → 自动算明天
    """
    text = (text or "").strip()
    if not text:
        return None
    # 组合口语词归一化：明早=明天早上、明晚=明天晚上（正则不增词表，预处理后走标准路径）
    for alias, std in (("明早", "明天早上"), ("明晚", "明天晚上"),
                       ("今早", "今天早上"), ("今晚", "今天晚上")):
        if text.startswith(alias):
            text = std + text[len(alias):]
            break
    now = datetime.now()

    # 相对时间：X秒钟后 / X秒后 / X分钟后 / X小时后
    m = _RE_REL.match(text)
    if m and m.end() == len(text):
        n = _cn_to_int(m.group(1))
        if n is None:
            return None
        unit = m.group(2)
        if unit == "分钟":
            delta = timedelta(minutes=n)
        elif unit == "小时":
            delta = timedelta(hours=n)
        else:  # 秒 / 秒钟
            delta = timedelta(seconds=n)
        # 秒级提醒保留秒精度（截断到分钟会被 add 误判为已过点而顺延到明天）；
        # 存储仍按分钟精度落盘，弹出粒度最差不差于 1 分钟，可接受
        if unit in ("秒", "秒钟"):
            return (now + delta).replace(microsecond=0)
        return (now + delta).replace(second=0, microsecond=0)

    # 特例：『（今天|明天|后天）中午』没有「X点」，固定 12:00
    m = re.match(r"^\s*(?:(今天|明天|后天)\s*)?中午\s*$", text)
    if m:
        day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(m.group(1), 0)
        dt = (now + timedelta(days=day_offset)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        if m.group(1) is None and dt <= now:
            dt += timedelta(days=1)
        return dt

    # 绝对时间：（今天|明天|后天）（时段）X点（半|Y分）
    m = _RE_ABS.match(text)
    if m and m.end() == len(text):
        day_word, period, hour_w, half, minute_w = m.groups()
        day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day_word, 0)
        if period == "中午":
            hour = 12
        else:
            hour = _cn_to_int(hour_w)
            if hour is None or hour > 24:
                return None
            if period in ("下午", "晚上") and hour < 12:
                hour += 12
            if hour == 24:
                hour = 0
                day_offset += 1
        if half:
            minute = 30
        elif minute_w:
            minute = _cn_to_int(minute_w)
            if minute is None or minute >= 60:
                return None
        else:
            minute = 0
        if hour >= 24:
            return None
        dt = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        # 未指明日期且今天已过点 → 明天
        if day_word is None and dt <= now:
            dt += timedelta(days=1)
        return dt

    return None


def _extract_time_prefix(raw: str):
    """
    在原文开头提取最长的时间表达式前缀。
    返回 (datetime, 剩余内容)；解析不出返回 (None, raw)。
    """
    raw = raw.strip()
    # 组合口语词归一化：明早=明天早上、明晚=明天晚上——正则在原始串上匹配，
    # 归一化必须发生在前缀提取之前（放在 parse_time 里对本函数无效，实测踩过）
    for alias, std in (("明早", "明天早上"), ("明晚", "明天晚上"),
                       ("今早", "今天早上"), ("今晚", "今天晚上")):
        if raw.startswith(alias):
            raw = std + raw[len(alias):]
            break
    # 相对时间优先（绝对时间正则以「点」结尾，不会误吞相对表达）
    for regex in (_RE_REL, _RE_ABS):
        m = regex.match(raw)
        if m:
            prefix = raw[: m.end()]
            when = parse_time(prefix)
            if when is not None:
                return when, raw[m.end():].strip()
    return None, raw


# == 对外契约 ==

def add(raw: str) -> str:
    """
    新增提醒。raw 为「提醒我」之后的原文：时间前缀 + 内容。
    解析不出时间 → 不存储，返回引导语；
    内容为空 → 不存储，追问；
    成功 → 存储并返回口语化确认；今天已过点自动顺延到明天并说明。
    """
    raw = (raw or "").strip()
    if not raw:
        return ("抱歉先生，我没听清提醒时间，您可以这样说："
                "提醒我十分钟后喝水，或者提醒我明天早上九点开会。")

    when, content = _extract_time_prefix(raw)

    if when is None:
        return ("抱歉先生，我没听清提醒时间，您可以这样说："
                "提醒我十分钟后喝水，或者提醒我明天早上九点开会。")

    if not content:
        return "时间是记住了，但要提醒您做什么事呢？"

    # 过去时间（今天已过点）自动顺延到明天并说明
    rolled = False
    now = datetime.now()
    if when <= now:
        when = when + timedelta(days=1)
        rolled = True

    with _STORE_LOCK:  # 读-改-写同一把锁，杜绝与 check_due 并发互吃
        entries = _read_entries()
        entries.append((when, content))
        if not _write_entries(entries):
            return "抱歉先生，提醒没有存下来，请稍后再试一次。"

    confirm = f"好的先生，我会在{_spoken_time(when)}提醒您：{content}。"
    if rolled:
        confirm = f"先生，今天这个时间已经过了，我帮您顺延到明天。{confirm}"
    return confirm


def list_pending() -> str:
    """口语化列出未来提醒（按时间排序）；无提醒返回固定话术。"""
    try:
        with _STORE_LOCK:  # 读临界区入锁，与 add / check_due 串行化
            now = datetime.now()
            pending = sorted((w, c) for w, c in _read_entries() if w > now)
        if not pending:
            return "先生，目前没有任何待提醒事项。"
        items = []
        for when, content in pending:
            items.append(f"{_spoken_time(when)}，{content}")
        head = "先生，您有" + (
            "一条" if len(pending) == 1 else f"{len(pending)}条"
        ) + "待提醒事项："
        return head + "；".join(items) + "。"
    except Exception:
        return "先生，目前没有任何待提醒事项。"


def check_due() -> list:
    """
    弹出所有到点提醒：返回播报文本列表（每条『先生，提醒时间到：内容。』），
    并从存储中移除；无到点返回 []；永不抛异常。
    """
    try:
        with _STORE_LOCK:  # 读-改-写同一把锁，与 add 串行化
            now = datetime.now()
            due, keep = [], []
            for when, content in _read_entries():
                if when <= now:
                    due.append(f"先生，提醒时间到：{content}。")
                else:
                    keep.append((when, content))
            if due:
                _write_entries(keep)
            return due
    except Exception:
        return []
