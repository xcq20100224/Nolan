# -*- coding: utf-8 -*-
"""
triggers.py —— 条件触发引擎（P4 · 主动性进阶）

第一性原理：提醒（reminders）是「时间到 → 做」；条件触发是「条件成立 → 做」。
物理世界只有两种触发源：
    周期型（interval）——「每隔 30 分钟提醒我喝水」：时间循环，无需判断；
    条件型（condition）——「如果明天下雨就提醒我带伞」：需用联网搜索评估真假。
动作只有两种：
    消息型（提醒我/告诉我 X）——到点把 X 说给主人听（默认，安全）；
    执行型（其它一切指令）——到点把指令交给大脑执行（经注入的 executor，
    本模块不反向依赖 brain，杜绝循环导入）。

对外接口契约（签名不可改）：
    def add(raw: str) -> str | None
        解析自然语言触发任务并落库，返回口语化确认；解析不出返回 None（放行）。
    def list_pending() -> str
        口语化列出在册触发任务；无任务返回固定话术。
    def check_due(executor=None, evaluator=None) -> list[str]
        评估所有到点任务：命中即触发（动作型经 executor 执行），
        返回要播报给主人的消息列表；无触发返回 []。
        evaluator(cond) -> bool|None 由调用方注入（brain.eval_condition），
        未注入时条件型任务顺延不评估（绝不瞎猜触发）。

存储：memory/triggers.json（JSON 数组，与 reminders 同目录同风格）。
"""

import json
import os
import re
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_STORE = os.path.join(_DIR, "memory", "triggers.json")
_LOCK = threading.Lock()

# 条件型任务的默认评估间隔（分钟）：LLM 联网评估有成本，30 分钟一拍足够
_DEFAULT_CHECK_MIN = 30
# 循环条件型（每当/每次/一旦）触发后的冷却期（秒）：防条件持续为真时轰炸
_RECUR_COOLDOWN_SEC = 3600


# ========== 存储 ==========
def _read() -> list[dict]:
    try:
        with open(_STORE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _STORE)


# ========== 中文数字 → 分钟 ==========
_CN_NUM = {"一": 1, "两": 2, "半": 0.5}


def _interval_to_min(num_s: str, unit: str) -> float | None:
    try:
        n = _CN_NUM.get(num_s, None)
        if n is None:
            n = float(num_s)
    except ValueError:
        return None
    if unit == "秒":
        return max(n / 60.0, 0.2)  # 最小 12 秒，防零间隔死循环
    if unit == "分钟":
        return n
    if unit in ("小时", "钟头"):
        return n * 60
    if unit == "天":
        return n * 1440
    return None


# ========== 解析 ==========
_INTERVAL_RE = re.compile(
    r"每隔?\s*(\d+(?:\.\d+)?|一|两|半)\s*(个)?(秒|分钟|小时|钟头|天)")
_COND_HEAD_RE = re.compile(r"^(如果|若|要是)(.+?)[，,]?\s*(?:就|则|的话)[，,]?\s*(.+)$")
_COND_RECUR_RE = re.compile(r"^(每当|每次|一旦)(.+?)[，,]?\s*(?:就|时|的时候)[，,]?\s*(.+)$")
# 动作剥离词：句首的祈使语气词，落库前剔除
_ACTION_STRIP = "帮我请把那就"


def _clean_action(s: str) -> str:
    return s.strip(" ，。！？：:" + _ACTION_STRIP)


def _parse(raw: str) -> dict | None:
    """把一句自然语言解析成触发任务字典；解析不出返回 None。"""
    text = raw.strip(" ，。！？：:")
    if not text:
        return None

    # 1) 周期型：每隔 N 秒/分钟/小时/天 做 X
    m = _INTERVAL_RE.search(text)
    if m:
        minutes = _interval_to_min(m.group(1), m.group(3))
        if minutes is None:
            return None
        action = _clean_action(_INTERVAL_RE.sub("", text, count=1))
        if not action:
            return None
        return {
            "kind": "interval", "recurring": True,
            "condition": "", "action": action,
            "interval_min": minutes,
        }

    # 2) 循环条件型：每当/每次/一旦 X 就 Y
    m = _COND_RECUR_RE.match(text)
    if m:
        cond, action = m.group(2).strip(), _clean_action(m.group(3))
        if cond and action:
            return {
                "kind": "condition", "recurring": True,
                "condition": cond, "action": action,
                "interval_min": _DEFAULT_CHECK_MIN,
            }
        return None

    # 3) 单次条件型：如果/若/要是 X 就 Y
    m = _COND_HEAD_RE.match(text)
    if m:
        cond, action = m.group(2).strip(), _clean_action(m.group(3))
        if cond and action:
            return {
                "kind": "condition", "recurring": False,
                "condition": cond, "action": action,
                "interval_min": _DEFAULT_CHECK_MIN,
            }
    return None


def _is_message_action(action: str) -> bool:
    """动作是否为消息型（到点只说不动手）：提醒我/告诉我/叫醒我 开头。"""
    return any(action.startswith(k) for k in ("提醒我", "告诉我", "叫醒我", "叫我"))


# ========== 对外接口 ==========
def add(raw: str) -> str | None:
    """解析并落库一个触发任务，返回口语化确认；解析不出返回 None。"""
    parsed = _parse(raw)
    if parsed is None:
        return None
    now = time.time()
    entry = {
        "id": "t_%d" % int(now * 1000),
        "kind": parsed["kind"],
        "recurring": parsed["recurring"],
        "condition": parsed["condition"],
        "action": parsed["action"],
        "interval_min": parsed["interval_min"],
        "next_check": now + min(parsed["interval_min"] * 60, 300),  # 首次评估不晚于 5 分钟
        "cooldown_until": 0.0,
        "enabled": True,
        "created": now,
    }
    with _LOCK:
        entries = _read()
        entries.append(entry)
        _write(entries)

    action = entry["action"]
    if entry["kind"] == "interval":
        iv = entry["interval_min"]
        spoken = ("%g 分钟" % iv) if iv >= 1 else ("%d 秒" % round(iv * 60))
        return "好的先生，每隔 %s 我会%s。已记在触发列表里。" % (spoken, action)
    if entry["recurring"]:
        return ("好的先生，每当「%s」成立时，我会%s。"
                "我会定时核实，触发后一小时冷却，不会反复打扰。" % (entry["condition"], action))
    return ("好的先生，我会定时核实「%s」，一旦成立就%s，只提醒一次。"
            % (entry["condition"], action))


def list_pending() -> str:
    """口语化列出在册触发任务；无任务返回固定话术。"""
    entries = [e for e in _read() if e.get("enabled")]
    if not entries:
        return "先生，目前还没有在册的条件触发任务。"
    lines = ["先生，您有 %d 个条件触发任务：" % len(entries)]
    for i, e in enumerate(entries, 1):
        if e["kind"] == "interval":
            lines.append("第 %d 个：每隔 %g 分钟，%s。" % (i, e["interval_min"], e["action"]))
        else:
            tag = "每当" if e["recurring"] else "如果"
            lines.append("第 %d 个：%s「%s」成立，就%s。" % (i, tag, e["condition"], e["action"]))
    return "\n".join(lines)


def _fire(entry: dict, executor) -> str:
    """触发一个任务，返回要播报的消息文本。执行型动作经 executor 跑大脑。"""
    action = entry["action"]
    if _is_message_action(action) or executor is None:
        msg = re.sub(r"^(提醒我|告诉我|叫醒我|叫我)", "", action).strip(" ，。")
        return "条件触发：%s（源自「%s」）。" % (msg or action, _describe(entry))
    try:
        reply = executor(action)
    except Exception as exc:
        return "条件触发：%s，但执行出错了（%s）。" % (_describe(entry), exc)
    if not isinstance(reply, str) or not reply.strip():
        reply = "执行完了，没有更多要说的。"
    return "条件触发：%s。%s" % (_describe(entry), reply.strip())


def _describe(entry: dict) -> str:
    if entry["kind"] == "interval":
        return "每隔 %g 分钟的周期任务" % entry["interval_min"]
    return "条件「%s」已成立" % entry["condition"]


def check_due(executor=None, evaluator=None) -> list[str]:
    """评估所有到点任务，返回触发的消息列表；无触发返回 []。

    executor(cmd) -> str：执行型动作的大脑入口（如 brain.think 的包装）；
    evaluator(cond) -> bool|None：条件评估入口（如 brain.eval_condition），
    未注入时条件型任务顺延一拍，绝不无依据触发。
    """
    now = time.time()
    fired: list[str] = []
    changed = False
    with _LOCK:
        entries = _read()
        for e in entries:
            if not e.get("enabled"):
                continue
            if now < float(e.get("next_check", 0)):
                continue
            iv_sec = float(e.get("interval_min", _DEFAULT_CHECK_MIN)) * 60

            if e["kind"] == "interval":
                fired.append(_fire(e, executor))
                e["next_check"] = now + iv_sec
                changed = True
                continue

            # 条件型：冷却期内跳过
            if now < float(e.get("cooldown_until", 0)):
                e["next_check"] = now + iv_sec
                changed = True
                continue
            verdict = evaluator(e["condition"]) if evaluator is not None else None
            if verdict is True:
                fired.append(_fire(e, executor))
                if e["recurring"]:
                    e["cooldown_until"] = now + _RECUR_COOLDOWN_SEC
                    e["next_check"] = now + _RECUR_COOLDOWN_SEC
                else:
                    e["enabled"] = False  # 单次条件：触发一次即退役
                changed = True
            else:
                # 不成立或无法评估：顺延一拍，绝不为触发而触发
                e["next_check"] = now + iv_sec
                changed = True
        if changed:
            _write(entries)
    return fired


# ========== 模块自测（不碰网络/大脑） ==========
if __name__ == "__main__":
    import tempfile
    _STORE = os.path.join(tempfile.mkdtemp(), "triggers.json")  # 隔离自测存储

    # 周期型
    r = add("每隔30分钟提醒我喝水")
    assert r and "每隔" in r, r
    r = add("每隔 2 小时提醒我站起来活动")
    assert r, r
    # 单次条件型
    r = add("如果明天下雨，就提醒我带伞")
    assert r and "只提醒一次" in r, r
    # 循环条件型
    r = add("每当有重大人工智能新闻，就告诉我")
    assert r and "冷却" in r, r
    # 解析不出 → None（放行给大脑其它层）
    assert add("今天天气怎么样") is None
    # 列表
    s = list_pending()
    assert "4 个" in s, s

    # check_due：周期型到点必触发（消息型不依赖 executor）
    entries = _read()
    for e in entries:
        e["next_check"] = 0  # 全部到点
    _write(entries)
    msgs = check_due(evaluator=lambda c: False)  # 条件不成立
    assert any("喝水" in m for m in msgs), msgs
    assert not any("带伞" in m for m in msgs), msgs  # 条件否 → 不触发

    # 条件成立：单次触发后退役；循环型进入冷却
    entries2 = _read()
    for e in entries2:
        e["next_check"] = 0
    _write(entries2)
    msgs = check_due(evaluator=lambda c: True)
    assert any("带伞" in m for m in msgs), msgs
    assert any("人工智能新闻" in m for m in msgs), msgs
    msgs2 = check_due(evaluator=lambda c: True)
    assert not any("带伞" in m for m in msgs2), msgs2  # 单次已退役

    # 执行型动作：经 executor 执行并带回复
    entries3 = _read()
    for e in entries3:
        e["enabled"] = True
        e["next_check"] = 0
        e["cooldown_until"] = 0
        e["kind"] = "interval"
        e["action"] = "打开记事本"
    _write(entries3)
    msgs = check_due(executor=lambda cmd: "已执行：" + cmd)
    assert any("已执行：打开记事本" in m for m in msgs), msgs

    print("🎉 triggers 自测全过：解析/存储/周期/条件/单次退役/冷却/执行型")
