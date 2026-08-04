# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 结构化长期记忆模块（memory_v2.py）· Gap3 认知
职责：把「先生是怎样的人」沉淀成可检索、可注入、可遗忘的结构化记忆。

第一性原理：管家的记忆不是聊天日志的堆积，而是对主人的「理解」——
少量高纯度的事实（偏好/习惯/事实/人物/日程），每条都可追溯来源、
可统计被想起的次数。单机单用户场景下，JSON 文件足够：可读、可人工
检查、可用记事本直接修改；不上 SQLite，不上向量库——
百级条目的检索，关键词 + 时间 + 频率加权已足够精准，复杂度是可靠性的敌人。

与旧 memory.py（逐行口语文本，供「记住/回忆/忘掉」语音指令）并行存在：
旧模块是「先生亲口下令的记忆」，本模块是「自动萃取 + 画像注入」，
由主控择一或并存集成，本模块不反向依赖任何已有模块。

存储：jarvis\\data\\memory.json（__file__ 定位，目录自动创建，原子写入）
数据模型（每条记忆）：
    {
        "id":            "m_1736000000000_1",
        "category":      "preference" | "habit" | "fact" | "person" | "schedule",
        "content":       "先生喜欢黑咖啡，不加糖",
        "created_at":    "2026-01-04T09:30:00",
        "last_recalled": "2026-01-05T10:00:00",
        "recall_count":  3,
        "source":        "user" | "extract" | "system"
    }

接口契约（签名一字不差，主控按此集成）：
    def remember(content: str, category: str = "fact", source: str = "user") -> dict
        写入一条记忆；相似内容自动合并（更新 recall 计数与内容），返回落库条目。
    def recall(query: str, limit: int = 5) -> list[dict]
        关键词 + 时间衰减 + 频率加权检索，返回得分最高的 limit 条（并记账：被想起）。
    def profile_summary(max_chars: int = 400) -> str
        生成注入 system prompt 的用户画像摘要，长度恒不超过 max_chars。
    def extract_from_turn(user_msg: str, assistant_reply: str, llm_caller=None) -> list[dict]
        用 GLM 从一轮对话萃取值得记住的事实，返回 [{content, category, source}]；
        克制原则——闲聊不存，抽不到返回 []。llm_caller 可注入（测试 mock），
        缺省走本模块内置的 GLM 调用。
    def forget(query: str) -> int
        删除内容含 query 的记忆，返回删除条数。
    def stats() -> dict
        返回 {total, by_category, store_path} 统计。

便捷查询（H2 加法，纯增量，不改既有行为）：
    def habits() -> list[dict]
        只读返回全部 habit 类记忆副本（供 proactive 模式识别佐证），不记账 recall。
"""

import json
import os
import re
import threading
import time
from datetime import datetime

# == 常量与配置 ==

_DIR = os.path.dirname(os.path.abspath(__file__))
_STORE = os.path.join(_DIR, "data", "memory.json")
_LOCK = threading.Lock()

# 合法类别：萃取模型乱归类时回退到 fact
_CATEGORIES = ("preference", "habit", "fact", "person", "schedule")

# 相似度合并阈值：字符二元组 Jaccard ≥ 0.6 视为同一记忆（宁可合错，不可重积）
_MERGE_THRESHOLD = 0.6

# recall 加权系数：关键词命中是主信号，时间与频率是修正项
_W_KEYWORD = 3.0
_W_RECENCY = 1.0
_W_FREQ = 0.5
# 时间衰减半衰期（天）：一个月没被想起的记忆，时间分减半
_RECENCY_HALFLIFE_DAYS = 30.0


# == 内部读写原语（全部 try/except 兜底，文件损坏/不存在不崩溃）==

def _read() -> list:
    """读出全部记忆条目；文件不存在或损坏返回空列表。"""
    try:
        if not os.path.exists(_STORE):
            return []
        with open(_STORE, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(items: list) -> bool:
    """整体写回（临时文件 + 原子替换，防断电半写文件）；写失败返回 False。"""
    try:
        os.makedirs(os.path.dirname(_STORE), exist_ok=True)
        tmp = _STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STORE)
        return True
    except Exception:
        return False


def _now_iso() -> str:
    """当前本地时间的 ISO 字符串（秒级，人工可读）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(s: str) -> datetime | None:
    """解析 ISO 时间串；解析失败返回 None（视为年代久远）。"""
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _age_days(item: dict) -> float:
    """条目距今天数（按 last_recalled，其次 created_at）；无法解析返回很大值。"""
    ts = _parse_iso(item.get("last_recalled") or item.get("created_at") or "")
    if ts is None:
        return 3650.0
    return max((datetime.now() - ts).total_seconds() / 86400.0, 0.0)


# == 文本归一化与相似度 ==

def _normalize(text: str) -> str:
    """归一化：去空白与标点、转小写——相似度计算只看实义字符。"""
    return re.sub(r"[\s，。！？、；：,.!?;:'\"\"''（）()【】\[\]]+", "", (text or "")).lower()


def _bigrams(text: str) -> set:
    """字符二元组集合（中文无空格分词，bigram 是最朴素有效的相似度单位）。"""
    t = _normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similarity(a: str, b: str) -> float:
    """两条内容的相似度 ∈ [0,1]：bigram Jaccard，含包含关系加成。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    jaccard = len(sa & sb) / len(sa | sb)
    # 短句被长句完整包含且长度比悬殊不大时，视为同义的详略表述
    na, nb = _normalize(a), _normalize(b)
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if shorter and shorter in longer and len(shorter) >= 4:
        jaccard = max(jaccard, 0.5 + 0.5 * len(shorter) / max(len(longer), 1))
    return jaccard


def _new_id(items: list) -> str:
    """生成不冲突的记忆 id：毫秒时间戳 + 序号。"""
    return "m_%d_%d" % (int(time.time() * 1000), len(items) + 1)


# == 契约接口 ==

def remember(content: str, category: str = "fact", source: str = "user") -> dict:
    """写入一条记忆；相似内容自动合并而非重复堆积，返回落库条目。

    合并策略：命中相似旧条目时保留更长（信息量更大）的表述，
    更新 last_recalled 与 recall_count——「又被提起」本身就是重要性信号。
    """
    content = (content or "").strip()
    if not content:
        return {}
    if category not in _CATEGORIES:
        category = "fact"
    with _LOCK:
        items = _read()
        # 去重：与既有最相似的一条比对
        best, best_sim = None, 0.0
        for it in items:
            sim = _similarity(content, it.get("content", ""))
            if sim > best_sim:
                best, best_sim = it, sim
        if best is not None and best_sim >= _MERGE_THRESHOLD:
            old = best.get("content", "")
            if len(_normalize(content)) > len(_normalize(old)):
                best["content"] = content  # 新表述更详细，替换之
            best["last_recalled"] = _now_iso()
            best["recall_count"] = int(best.get("recall_count", 0)) + 1
            if best.get("category") == "fact" and category != "fact":
                best["category"] = category  # 更具体的归类可升级
            _write(items)
            return dict(best)
        entry = {
            "id": _new_id(items),
            "category": category,
            "content": content,
            "created_at": _now_iso(),
            "last_recalled": _now_iso(),
            "recall_count": 0,
            "source": source or "user",
        }
        items.append(entry)
        _write(items)
        return dict(entry)


def recall(query: str, limit: int = 5) -> list[dict]:
    """关键词 + 时间衰减 + 频率加权检索，返回得分最高的 limit 条。

    评分 = 3.0×关键词重合度 + 1.0×时间新鲜度（半衰期 30 天）+ 0.5×频率分。
    被命中的条目记账：last_recalled 刷新、recall_count +1（越常用越容易被想起）。
    """
    query = (query or "").strip()
    with _LOCK:
        items = _read()
        if not items:
            return []
        qg = _bigrams(query)
        # 单字查询（归一化后不足 2 字）走子串匹配：单字 bigram 与内容的
        # 二字 bigram 集合永远不相交，必须单独处理，否则「茶」永远零命中
        single_char = bool(query) and len(_normalize(query)) < 2
        scored = []
        for it in items:
            kw = 0.0
            content = it.get("content", "")
            if single_char:
                kw = 1.0 if _normalize(query) in _normalize(content) else 0.0
            elif qg:
                cg = _bigrams(content)
                kw = len(qg & cg) / len(qg) if cg else 0.0
            elif query:
                kw = 1.0 if query in content else 0.0
            recency = 0.5 ** (_age_days(it) / _RECENCY_HALFLIFE_DAYS)
            freq = min(int(it.get("recall_count", 0)), 10) / 10.0
            score = _W_KEYWORD * kw + _W_RECENCY * recency + _W_FREQ * freq
            scored.append((score, kw, it))
        # 空查询时按分数取全库 top；有查询时零关键词命中的不给（防答非所问）
        scored.sort(key=lambda t: t[0], reverse=True)
        hits = [t for t in scored if not qg or t[1] > 0.0][:max(int(limit), 1)]
        now = _now_iso()
        out = []
        for _, _, it in hits:
            it["last_recalled"] = now
            it["recall_count"] = int(it.get("recall_count", 0)) + 1
            out.append(dict(it))
        if hits:
            _write(items)
        return out


def profile_summary(max_chars: int = 400) -> str:
    """生成注入 system prompt 的用户画像摘要；长度恒不超过 max_chars。

    按类别分组、每类取最近被想起的若干条；超长时逐条截尾，硬约束不突破。
    无记忆时返回空字符串（主控拼 prompt 时自然跳过）。
    """
    max_chars = max(int(max_chars), 0)
    if max_chars == 0:
        return ""
    with _LOCK:
        items = _read()
    if not items:
        return ""
    groups: dict = {}
    order: list = []
    for it in sorted(items, key=lambda x: x.get("last_recalled", ""), reverse=True):
        cat = it.get("category", "fact")
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        if len(groups[cat]) < 3:  # 每类最多 3 条，防单类淹没画像
            groups[cat].append(it.get("content", ""))
    _CAT_CN = {"preference": "偏好", "habit": "习惯", "fact": "事实",
               "person": "人物", "schedule": "日程"}
    parts = ["%s：%s" % (_CAT_CN.get(c, c), "；".join(groups[c])) for c in order]
    summary = "关于先生的记忆：" + "。".join(parts) + "。"
    if len(summary) <= max_chars:
        return summary
    # 硬截断：逐字符裁到 max_chars-1 再加省略号，恒不超长
    return summary[:max_chars - 1] + "…"


def forget(query: str) -> int:
    """删除内容含 query 的全部记忆，返回删除条数。"""
    query = (query or "").strip()
    if not query:
        return 0
    with _LOCK:
        items = _read()
        kept = [it for it in items if query not in it.get("content", "")]
        removed = len(items) - len(kept)
        if removed:
            _write(kept)
        return removed


def stats() -> dict:
    """记忆库统计：{total, by_category, store_path}。"""
    with _LOCK:
        items = _read()
    by_cat: dict = {}
    for it in items:
        by_cat[it.get("category", "fact")] = by_cat.get(it.get("category", "fact"), 0) + 1
    return {"total": len(items), "by_category": by_cat, "store_path": _STORE}


# == 便捷查询（H2 加法：只读，不改写任何记账字段）==

def habits() -> list:
    """返回全部 habit 类记忆的副本，按最近被想起排序。

    第一性原理：模式识别（proactive.detect_patterns）需要的是「翻阅」习惯
    来为统计模式做佐证，而不是「想起」它们——故本函数只读，
    不刷新 last_recalled、不累加 recall_count，绝不污染 recall 的频率信号。
    """
    with _LOCK:
        items = [dict(it) for it in _read() if it.get("category") == "habit"]
    items.sort(key=lambda x: x.get("last_recalled", ""), reverse=True)
    return items


# == GLM 萃取（extract_from_turn 的默认实现，测试时经 llm_caller 注入 mock）==

def _load_llm_config() -> dict:
    """读 llm_config.json（与 brain 同目录同风格）；缺项返回 {}。"""
    try:
        path = os.path.join(_DIR, "llm_config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return {k: v for k, v in cfg.items() if isinstance(v, str) and v} \
            if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _default_llm_caller(prompt: str) -> str | None:
    """内置 GLM 调用：纯文本进、纯文本出；任何失败返回 None（萃取失败不炸对话）。"""
    try:
        import httpx
        cfg = _load_llm_config()
        if not cfg.get("api_key") or not cfg.get("base_url"):
            return None
        payload = {
            "model": cfg.get("model", "glm-5.2"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,  # 萃取是抽取任务，低温防发挥
        }
        extra = cfg.get("extra_body")
        if extra:
            try:
                payload.update(json.loads(extra))
            except ValueError:
                pass
        resp = httpx.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer " + cfg["api_key"]},
            timeout=30.0)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("[memory_v2] GLM 萃取调用失败: %s: %s" % (type(e).__name__, e))
        return None


_EXTRACT_PROMPT = (
    "你是记忆萃取器。从下面一轮对话中，抽取「值得长期记住的关于用户的事实」。\n"
    "只抽取明确表达的：偏好（喜欢/讨厌）、习惯（每天/通常）、事实（职业/住址/设备）、"
    "人物（家人朋友及其关系）、日程（确定的时间安排）。\n"
    "克制原则：闲聊、客套、一次性请求、不确定的推测——一律不抽。抽不到就返回空数组。\n"
    "严格返回 JSON 数组，不要任何多余文字，元素格式：\n"
    '[{"content": "事实的一句话陈述", "category": "preference|habit|fact|person|schedule"}]\n'
    "用户说：%s\n"
    "助手答：%s"
)


def extract_from_turn(user_msg: str, assistant_reply: str, llm_caller=None) -> list:
    """用 GLM 从一轮对话萃取值得记住的事实，返回 [{content, category, source}]。

    llm_caller(prompt) -> str 可注入（测试 mock / 主控复用 brain 的调用），
    缺省用本模块内置 GLM 调用。任何环节失败返回 []——萃取是增强而非主链路。
    """
    user_msg = (user_msg or "").strip()
    assistant_reply = (assistant_reply or "").strip()
    if not user_msg:
        return []
    caller = llm_caller or _default_llm_caller
    try:
        raw = caller(_EXTRACT_PROMPT % (user_msg, assistant_reply))
    except Exception as e:
        print("[memory_v2] 萃取 llm_caller 异常: %s: %s" % (type(e).__name__, e))
        return []
    if not raw:
        return []
    # 防御式解析：模型可能包 markdown 代码块或夹带废话，截取首个 [ 到末个 ]
    try:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        data = json.loads(raw[start:end + 1])
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            content = str(d.get("content", "")).strip()
            if not content:
                continue
            category = d.get("category", "fact")
            if category not in _CATEGORIES:
                category = "fact"
            out.append({"content": content, "category": category, "source": "extract"})
        return out
    except Exception:
        return []


# == 独立自检（不依赖网络，GLM 萃取经 mock 验证）==

if __name__ == "__main__":
    import tempfile
    _STORE = os.path.join(tempfile.mkdtemp(), "memory.json")  # 隔离自测存储

    print("memory_v2.py 独立自检：")
    e1 = remember("先生喜欢黑咖啡，不加糖", category="preference")
    print("  remember ->", e1["id"], e1["category"])
    e2 = remember("先生喜欢黑咖啡", category="preference")  # 相似 → 应合并
    assert e2["id"] == e1["id"], "相似内容未合并"
    assert e2["recall_count"] == 1
    remember("先生每天早上七点起床", category="habit")
    remember("先生的妻子叫小林", category="person")

    hits = recall("咖啡", limit=5)
    assert hits and "咖啡" in hits[0]["content"], hits
    print("  recall(咖啡) ->", hits[0]["content"])

    s = profile_summary(max_chars=400)
    assert len(s) <= 400, len(s)
    print("  profile_summary ->", s[:60], "...")

    mock_caller = lambda p: '[{"content": "先生偏好简洁的回答", "category": "preference"}]'
    items = extract_from_turn("以后回答简短点", "好的先生。", llm_caller=mock_caller)
    assert items and items[0]["category"] == "preference", items
    assert extract_from_turn("嗯", "好的。", llm_caller=lambda p: "[]") == []
    print("  extract_from_turn(mock) ->", items)

    n = forget("起床")
    assert n == 1, n
    print("  forget(起床) ->", n, "  stats ->", stats())
    print("🎉 memory_v2 自测全过：写入/合并/检索/画像/萃取/遗忘/统计")
