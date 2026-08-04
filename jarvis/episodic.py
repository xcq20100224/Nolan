# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 情景记忆模块（episodic.py）· H1 时间线经历

第一性原理：人有两套记忆——「语义记忆」（我知道什么）与「情景记忆」（我经历过什么）。
memory_v2.py 是前者：沉淀稳定事实（偏好/习惯/事实），回答「先生是怎样的人」；
本模块是后者：记录流动的经历（对话/任务/结果/错误/里程碑），回答带时间坐标的问题——
「上周三那次报错后来怎么解决的」「昨天让你做的事做完了吗」。
两者互补，互不重复：事实无时间坐标也成立，经历离开时间戳就失去意义。

设计取舍：
- 单机单用户，JSON 文件足够——可读、可人工检查；不上 SQLite/向量库，复杂度是可靠性的敌人。
- 事件只追加、少修改；检索是「关键词 × 时间近 × 显著度高」的粗排，
  千级事件的线性扫描是毫秒级，无需索引。
- 显著度（salience）模拟记忆固化：错误与里程碑天然记得牢（0.8），
  闲聊很快淡忘（0.2）；淘汰时先丢低显著度的旧事件——像人一样先忘掉无关紧要的事。
- 绝不抛异常给调用方：存储损坏自动备份重开，输入异常静默降级。
  记忆模块是辅助系统，宁可丢一条记忆，不可让主流程崩。

存储：jarvis\\data\\episodic.json（__file__ 定位，目录自动创建，原子写入）
数据模型（每条事件）：
    {
        "id":       "e_1736000000000_1",
        "ts":       "2026-01-04T09:30:00",      # ISO 本地时间
        "kind":     "conversation" | "task" | "outcome" | "error" | "milestone",
        "summary":  "先生问新闻，我搜了并写了日报",   # ≤120 字
        "refs":     ["daily_report.md"],         # 相关文件名/应用名
        "salience": 0.2                          # 0~1 显著度
    }

接口契约（签名一字不差，主控按此集成）：
    def log_event(kind: str, summary: str, refs=None, salience=None) -> dict
    def timeline(days: int = 7, kinds=None, limit: int = 20) -> list[dict]
    def search(query: str, days: int = 30, limit: int = 10) -> list[dict]
    def brief_for_prompt(max_chars: int = 300) -> str
    def prune(max_age_days: int = 90, max_events: int = 2000) -> int
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_KINDS = ("conversation", "task", "outcome", "error", "milestone")

# 各类事件的默认显著度：错误/里程碑天然记得牢，闲聊很快淡忘
_DEFAULT_SALIENCE = {
    "error": 0.8,
    "milestone": 0.8,
    "task": 0.5,
    "outcome": 0.5,
    "conversation": 0.2,
}

_SUMMARY_MAX = 120            # 摘要硬上限
_FILE_MAX_BYTES = 500 * 1024  # 文件超 500KB 触发容量淘汰
_BRIEF_WINDOW_HOURS = 48      # 简报只覆盖近 48 小时
_BRIEF_MIN_SALIENCE = 0.5     # 简报只收高显著度事件

# ---------------------------------------------------------------------------
# 存储路径与内部状态（模块级缓存 + RLock，毫秒级读写）
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_STORE_FILE = os.path.join(_DATA_DIR, "episodic.json")

_lock = threading.RLock()
_cache = None          # list[dict]，None 表示尚未从磁盘加载
_id_counter = 0        # 同毫秒内的 id 去重序号


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(ts: str):
    """宽容解析 ISO 时间戳，失败返回 None。"""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _load() -> list:
    """懒加载磁盘事件到缓存；损坏时备份 .corrupt 重开，绝不抛异常。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        events = []
        if os.path.exists(_STORE_FILE):
            try:
                with open(_STORE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    events = [e for e in data if isinstance(e, dict) and "ts" in e]
            except (json.JSONDecodeError, OSError, ValueError):
                # 损坏：备份后从零开始，记忆可以丢，主流程不能崩
                try:
                    os.replace(_STORE_FILE, _STORE_FILE + ".corrupt")
                except OSError:
                    pass
                events = []
        _cache = events
        return _cache


def _save() -> None:
    """原子写入：临时文件 + os.replace。失败静默（不抛给调用方）。"""
    global _cache
    with _lock:
        if _cache is None:
            return
        try:
            os.makedirs(os.path.dirname(_STORE_FILE), exist_ok=True)
            # 容量纪律：写入前检查现有文件体积，超限先淘汰低显著度旧事件；
            # 一轮 prune 仍超则迭代收紧条数上限，直到体积达标（至少保 100 条）
            try:
                if os.path.exists(_STORE_FILE) and \
                        os.path.getsize(_STORE_FILE) > _FILE_MAX_BYTES:
                    _prune_locked()
                    cap = len(_cache)
                    while len(_cache) > 100 and \
                            _estimate_bytes() > _FILE_MAX_BYTES:
                        cap = max(100, int(cap * 0.7))
                        _prune_locked(max_events=cap)
            except OSError:
                pass
            tmp = _STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_cache, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _STORE_FILE)
        except OSError:
            pass


def _estimate_bytes() -> int:
    """估算当前缓存序列化后的字节数（供容量纪律迭代判断）。"""
    try:
        return len(json.dumps(_cache, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _prune_locked(max_age_days: int = 90, max_events: int = 2000) -> int:
    """内部淘汰（须持锁调用）：先丢超龄事件，再按 低显著度→旧时间 丢到上限内。"""
    global _cache
    if _cache is None:
        return 0
    before = len(_cache)
    cutoff = datetime.now() - timedelta(days=max_age_days)
    kept = [e for e in _cache
            if (_parse_ts(e.get("ts", "")) or datetime.min) >= cutoff]
    if len(kept) > max_events:
        # 显著度升序、时间升序：最不重要的排前面先被淘汰
        kept.sort(key=lambda e: (e.get("salience", 0.5),
                                 e.get("ts", "")))
        kept = kept[len(kept) - max_events:]
        kept.sort(key=lambda e: e.get("ts", ""))
    _cache = kept
    return before - len(kept)


def _validate_kind(kind) -> str:
    return kind if kind in _KINDS else "conversation"


def _default_salience(kind: str) -> float:
    return _DEFAULT_SALIENCE.get(kind, 0.2)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def log_event(kind: str, summary: str, refs=None, salience=None) -> dict:
    """写入一条经历。任何异常都不抛出，最坏返回带错误标记的事件 dict。"""
    global _id_counter
    try:
        kind = _validate_kind(kind)
        summary = str(summary or "").strip()[:_SUMMARY_MAX]
        if not isinstance(refs, list):
            refs = [refs] if refs else []
        refs = [str(r) for r in refs][:10]
        if salience is None:
            salience = _default_salience(kind)
        salience = max(0.0, min(1.0, float(salience)))

        with _lock:
            _id_counter += 1
            event = {
                "id": "e_%d_%d" % (int(time.time() * 1000), _id_counter),
                "ts": _now_iso(),
                "kind": kind,
                "summary": summary,
                "refs": refs,
                "salience": salience,
            }
            _load().append(event)
            _save()
        return event
    except Exception as exc:  # 兜底：记忆模块永不炸主流程
        return {"id": "", "ts": _now_iso(), "kind": "error",
                "summary": "log_event 内部异常: %s" % exc,
                "refs": [], "salience": 0.0}


def timeline(days: int = 7, kinds=None, limit: int = 20) -> list:
    """近 days 天的事件时间线，按时间倒序；kinds 可过滤事件类型。"""
    try:
        days = max(0, int(days))
        limit = max(1, int(limit))
        cutoff = datetime.now() - timedelta(days=days)
        kind_set = set(kinds) if kinds else None

        with _lock:
            events = list(_load())
        out = []
        for e in events:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            if kind_set and e.get("kind") not in kind_set:
                continue
            out.append(e)
        out.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return out[:limit]
    except Exception:
        return []


def search(query: str, days: int = 30, limit: int = 10) -> list:
    """关键词检索：命中数 × (1+显著度) × 时间衰减 加权排序。"""
    try:
        query = str(query or "").strip()
        if not query:
            return []
        days = max(1, int(days))
        limit = max(1, int(limit))
        # 查询词切分：英文/数字按词，中文按二元组，提高中文召回
        terms = re.findall(r"[a-z0-9]+", query.lower())
        cjk = re.sub(r"[^一-鿿]", "", query)
        terms += [cjk[i:i + 2] for i in range(len(cjk) - 1)] or \
                 ([cjk] if cjk else [])
        if not terms:
            return []

        cutoff = datetime.now() - timedelta(days=days)
        now = datetime.now()
        scored = []
        with _lock:
            events = list(_load())
        for e in events:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            hay = (e.get("summary", "") + " " +
                   " ".join(e.get("refs", []))).lower()
            hits = sum(1 for t in terms if t.lower() in hay)
            if hits == 0:
                continue
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
            decay = 1.0 / (1.0 + age_days / max(1, days))  # 越近权重越高
            score = hits * (1.0 + e.get("salience", 0.5)) * decay
            scored.append((score, e))
        scored.sort(key=lambda x: (x[0], x[1].get("ts", "")), reverse=True)
        return [e for _, e in scored[:limit]]
    except Exception:
        return []


def _time_label(ts: datetime, now: datetime) -> str:
    """生成「今天 09:30 / 昨天 20:14 / 周三 15:02」式时间标签。"""
    day_diff = (now.date() - ts.date()).days
    hm = ts.strftime("%H:%M")
    if day_diff == 0:
        return "今天 " + hm
    if day_diff == 1:
        return "昨天 " + hm
    if day_diff < 7:
        weekdays = "一二三四五六日"
        return "周%s %s" % (weekdays[ts.weekday()], hm)
    return ts.strftime("%m月%d日 ") + hm

_KIND_LABEL = {
    "conversation": "对话",
    "task": "任务",
    "outcome": "结果",
    "error": "出错",
    "milestone": "里程碑",
}


def brief_for_prompt(max_chars: int = 300) -> str:
    """近 48 小时高显著度事件的中文简报，供注入 system prompt。

    例：「近期经历：昨天 20:14 任务『搜新闻写日报』；今天 09:30 出错『记事本任务失败』」
    长度硬约束：先按 显著度升序+时间正序 丢条，仍超长则截断文本。
    没有可报事件时返回空串（主控拼入前判空即可）。
    """
    try:
        max_chars = max(20, int(max_chars))
        now = datetime.now()
        cutoff = now - timedelta(hours=_BRIEF_WINDOW_HOURS)

        with _lock:
            events = list(_load())
        picks = []
        for e in events:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            if e.get("salience", 0.5) < _BRIEF_MIN_SALIENCE:
                continue
            picks.append((ts, e))
        picks.sort(key=lambda x: x[0])  # 时间正序，读起来是经历流水

        def render(items):
            parts = ["%s %s『%s』" % (
                _time_label(ts, now),
                _KIND_LABEL.get(e.get("kind"), "事件"),
                e.get("summary", "")[:60]) for ts, e in items]
            return "近期经历：" + "；".join(parts)

        # 先丢最不重要的（低显著度优先、越旧优先），直到放下
        while picks and len(render(picks)) > max_chars:
            drop = min(range(len(picks)),
                       key=lambda i: (picks[i][1].get("salience", 0.5),
                                      picks[i][0]))
            picks.pop(drop)
        if not picks:
            return ""
        text = render(picks)
        if len(text) > max_chars:  # 单条就超长的极端情况：硬截断
            text = text[:max_chars - 1] + "…"
        return text
    except Exception:
        return ""


def prune(max_age_days: int = 90, max_events: int = 2000) -> int:
    """淘汰旧事件，返回删除条数。永不抛异常。"""
    try:
        with _lock:
            _load()
            removed = _prune_locked(int(max_age_days), int(max_events))
            if removed:
                _save()
            return removed
    except Exception:
        return 0
