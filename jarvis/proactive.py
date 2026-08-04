# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 主动性引擎（proactive.py）· Gap4 自主
职责：决定「此刻该不该主动开口」，并生成一句自然的主动消息。

第一性原理：响应式助手是「问一句答一句」的金鱼；真正的管家会在
恰当的时刻主动开口——但主动性的第一约束不是「说什么」，而是
「何时闭嘴」。打扰的代价远高于沉默的代价，所以本模块的默认姿态
是克制：速率限制、安静时段、用户刚活动过——三重闸门全部放行
才允许开口，且开口内容必须基于记忆与触发器的事实，绝不没话找话。

边界：本模块只做「决策 + 生成」，不做任何 IO 投递——
消息交给谁、怎么播报、入哪个队列，全部是主控/server 的事。

对外接口契约（签名一字不差，主控按此集成）：
    def should_initiate(context: dict) -> bool
        判断现在是否适合主动开口。context 键（均可选）：
            now                 datetime | epoch 秒（缺省取当前时间，测试可注入）
            last_user_activity  datetime | epoch 秒 | None（用户最近一次交互）
            last_initiative     datetime | epoch 秒 | None（上次主动开口；
                                缺省用模块内部记录，由 generate_initiative 自动记账）
            high_confidence_anticipation  bool（H2：当前开口理由是高置信预判时置 True，
                                仅放宽「用户刚交互不打扰」一道闸门到 2 分钟，其余不动）
    def generate_initiative(context: dict, llm_caller) -> str | None
        生成一条自然的主动消息；无素材且无模型时返回 None（沉默也是合法决策）。
        context 在 should_initiate 的基础上可携带：
            profile       str        记忆画像（memory_v2.profile_summary 的产物）
            due_messages  list[str]  triggers.check_due 的到期消息
            anticipation  str        H2 当日预判素材（主控算好直接给）
            patterns      list[dict] H2 或直接给模式列表，由本函数现场判定预判素材
            today_events  list[dict] H2 今日已发生事件（配合 patterns 判定，免重复读盘）
        llm_caller(prompt: str) -> str 由主控注入（如 brain 的 GLM 调用的
        单参数包装）；异常或返回空时回退到模板消息。

H2 理解驱动新增（预判 = 从时间序列提取「周期结构」，再问「按惯例此刻该发生什么」）：
    def detect_patterns(days=14, now=None, timeline_fn=None, habits_fn=None) -> list[dict]
        纯统计模式识别（不调 LLM）：同类事件在相似时段（±1 小时）出现 ≥3 次
        且跨 ≥2 天 → 模式；样本不足返回 []（统计模式 ≠ 真理解，沉默是对的）。
    def high_confidence(pattern: dict) -> bool
        是否高置信预判（confidence ≥ 0.8 且 occurrences ≥ 5）。
    def current_anticipation(context: dict, patterns: list, events_fn=None) -> str | None
        此刻命中某模式惯常时段且今天尚未发生 → 返回预判素材，否则 None。

调参入口：以下模块常量即为「主动性人格」的旋钮，改数值即改性格。
"""

import re
import time
from collections import Counter
from datetime import datetime, timedelta

# == 主动性人格旋钮（模块常量，调这里）==

MAX_INITIATIVES_PER_HOUR = 1      # 速率限制：每小时最多主动开口次数
QUIET_START_HOUR = 23             # 安静时段起点（含）：23:00 后不打扰
QUIET_END_HOUR = 8                # 安静时段终点（不含）：8:00 前不打扰
MIN_IDLE_AFTER_USER_SEC = 300     # 用户刚交互过 5 分钟内不打扰（他正在用，别抢话）

# == H2 模式识别旋钮（预判人格）==

PATTERN_LOOKBACK_DAYS = 14            # 默认回望窗口：两周足以覆盖「每天/每周」级惯例
PATTERN_MIN_OCCURRENCES = 3           # 同类事件在相似时段出现 ≥3 次才算模式
PATTERN_MIN_DAYS_SPAN = 2             # 且至少跨 2 个不同的日子——同一天三连发是冲动不是惯例
PATTERN_HOUR_WINDOW = 1               # 「相似时段」容差：惯常时刻 ±1 小时
HIGH_CONFIDENCE_MIN = 0.8             # 高置信预判的置信度下限
HIGH_CONFIDENCE_MIN_OCCURRENCES = 5   # 高置信预判的出现次数下限
MIN_IDLE_AFTER_USER_HIGH_CONF_SEC = 120  # 高置信预判放宽：主人刚走 2 分钟即可开口

# 聚类停用词：高频但无区分度的二元组，防止「先生」「任务」把一切聚成一团
_TOKEN_STOPWORDS = {"先生", "任务", "对话", "结果", "出错", "事件",
                    "一下", "这个", "那个", "可以", "就是"}

# 模块内部记账：上次主动开口的 epoch 秒（context 未携带 last_initiative 时用）
_last_initiative_at: float = 0.0


# == 时间归一化 ==

def _to_dt(value) -> datetime | None:
    """datetime / epoch 秒 / None 统一归一为 datetime；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(float(value))
    except Exception:
        return None


def _in_quiet_hours(now: datetime) -> bool:
    """是否处于安静时段（跨午夜区间 23:00–8:00）。"""
    h = now.hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


# == H2 模式识别：从时间序列提取周期结构（纯统计，不调 LLM）==

def _parse_event_ts(value) -> datetime | None:
    """事件时间戳归一化：datetime / ISO 字符串 / epoch 秒 → datetime；失败 None。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return _to_dt(value)


def _summary_tokens(summary: str) -> set:
    """把事件摘要切成粗聚类 token：中文取字符二元组，英文/数字取整词。

    第一性原理：中文没有空格分词，bigram 是最朴素有效的相似度单位
    （与 memory_v2 同款取舍）；不上分词器、不上 embedding——
    百级事件的聚类，确定性比精巧重要，可测可复现优先。
    """
    text = str(summary or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    cjk = re.sub(r"[^一-鿿]", "", text)
    tokens |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return tokens - _TOKEN_STOPWORDS


def _default_timeline_fn(days: int) -> list:
    """默认事件源：episodic.timeline。惰性 import，保持本模块可独立加载、可 mock。"""
    try:
        import episodic
        return episodic.timeline(days=days, limit=2000)
    except Exception:
        return []


def _default_events_today_fn() -> list:
    """默认今日事件源：近 1 天时间线（由调用方按日期再过滤）。惰性 import。"""
    try:
        import episodic
        return episodic.timeline(days=1, limit=500)
    except Exception:
        return []


def _default_habits_fn() -> list:
    """默认习惯来源：memory_v2.habits（只读查询）。惰性 import。"""
    try:
        import memory_v2
        return memory_v2.habits()
    except Exception:
        return []


def _lcs(a: str, b: str) -> str:
    """两串的最长公共子串（摘要 ≤120 字，小 DP 足够，确定性输出）。"""
    best = ""
    prev = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        curr = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                curr[j] = prev[j - 1] + 1
                if curr[j] > len(best):
                    best = a[i - curr[j]:i]
        prev = curr
    return best


def _display_keyword(summaries: list, fallback: str) -> str:
    """从同簇摘要提炼人类可读关键词：去停用词后的最长公共子串（≥2 字）。

    bigram token（如「写日」）适合机器匹配却不适合念给主人听；
    公共子串（如「写日报」）才是管家该说出口的词。提炼不出则回退 token。
    """
    cleaned = []
    for s in summaries:
        t = str(s or "")
        for sw in _TOKEN_STOPWORDS:
            t = t.replace(sw, "")
        cleaned.append(t)
    if not cleaned:
        return fallback
    kw = cleaned[0]
    for s in cleaned[1:]:
        kw = _lcs(kw, s)
    kw = kw.strip("，。,.、;；:： \t")
    return kw if len(kw) >= 2 else fallback


def detect_patterns(days: int = PATTERN_LOOKBACK_DAYS, now=None,
                    timeline_fn=None, habits_fn=None) -> list:
    """从近 days 天的经历里提取重复模式（纯统计，不调 LLM，便宜、可测、确定性）。

    第一性原理：预判 = 从时间序列提取「周期结构」——同类事件（摘要关键词
    粗聚）若在相似时段（±1 小时）反复出现（≥3 次、跨 ≥2 天），它大概率
    是惯例而非巧合。诚实边界：统计模式 ≠ 真理解，样本不足时返回 [] 是
    正确答案——沉默优于编造，猜错的打扰比不打扰更伤信任。

    返回 [{kind, keyword, match_tokens, usual_hour, occurrences, days_span,
           confidence, habit_backed}]，按置信度降序。
      keyword      人类可读关键词（供管家说出口）
      match_tokens 机器匹配 token 集（供「今天是否已发生」判定）
      confidence   0.5+0.06×次数（封顶 1.0）× 时间一致性，habit 记忆佐证 +0.1
    timeline_fn(days) / habits_fn() / now 均可注入（测试与主控复用）；
    缺省分别走 episodic.timeline / memory_v2.habits / 当前时间。
    """
    now = _to_dt(now) or datetime.now()
    days = max(1, int(days))
    cutoff = now - timedelta(days=days)

    try:
        raw = (timeline_fn or _default_timeline_fn)(days) or []
    except Exception:
        raw = []
    events = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        ts = _parse_event_ts(e.get("ts"))
        summary = str(e.get("summary") or "").strip()
        if ts is None or ts < cutoff or not summary:
            continue
        events.append({"ts": ts, "kind": str(e.get("kind") or "conversation"),
                       "summary": summary, "tokens": _summary_tokens(summary)})
    if len(events) < PATTERN_MIN_OCCURRENCES:
        return []  # 样本不足：沉默是对的

    try:
        habits = (habits_fn or _default_habits_fn)() or []
    except Exception:
        habits = []
    habit_texts = [str(h.get("content", "")) for h in habits if isinstance(h, dict)]

    # 关键词粗聚：按 token 频次降序认领事件，已认领的不再参与后续聚类（防重复模式）
    freq = Counter()
    for e in events:
        for t in e["tokens"]:
            freq[t] += 1
    candidates = sorted((t for t, c in freq.items() if c >= PATTERN_MIN_OCCURRENCES),
                        key=lambda t: (-freq[t], t))

    claimed = set()
    patterns = []
    for token in candidates:
        group = [(i, e) for i, e in enumerate(events)
                 if i not in claimed and token in e["tokens"]]
        if len(group) < PATTERN_MIN_OCCURRENCES:
            continue
        # 时间聚类：枚举锚点小时，取 ±1 小时窗口内成员最多的一簇（确定性 tie-break：最小锚点小时优先）
        best_members = []
        for h in sorted({e["ts"].hour for _, e in group}):
            members = [(i, e) for i, e in group
                       if abs(e["ts"].hour - h) <= PATTERN_HOUR_WINDOW]
            if len(members) > len(best_members):
                best_members = members
        if len(best_members) < PATTERN_MIN_OCCURRENCES:
            continue
        days_span = len({e["ts"].date() for _, e in best_members})
        if days_span < PATTERN_MIN_DAYS_SPAN:
            continue  # 同一天三连发是冲动不是惯例
        claimed |= {i for i, _ in best_members}

        occ = len(best_members)
        member_summaries = sorted(e["summary"] for _, e in best_members)
        keyword = _display_keyword(member_summaries, token)
        habit_backed = any(token in ht or keyword in ht for ht in habit_texts)
        # 置信度 = 次数分 × 时间一致性（窗口内/全组），habit 记忆佐证 +0.1
        consistency = occ / len(group)
        conf = min(1.0, 0.5 + 0.06 * occ) * consistency
        if habit_backed:
            conf = min(1.0, conf + 0.1)
        hours = sorted(e["ts"].hour for _, e in best_members)
        common = set.intersection(*(e["tokens"] for _, e in best_members)) \
            if best_members else {token}
        patterns.append({
            "kind": Counter(e["kind"] for _, e in best_members).most_common(1)[0][0],
            "keyword": keyword,
            "match_tokens": sorted(common) or [token],
            "usual_hour": hours[len(hours) // 2],  # 中位小时（偶数取上中位，确定性）
            "occurrences": occ,
            "days_span": days_span,
            "confidence": round(conf, 2),
            "habit_backed": habit_backed,
        })

    patterns.sort(key=lambda p: (-p["confidence"], -p["occurrences"], p["keyword"]))
    return patterns


def high_confidence(pattern: dict) -> bool:
    """是否「高置信预判」（confidence ≥ 0.8 且 occurrences ≥ 5）。

    只有高置信预判才有资格放宽「用户刚交互不打扰」闸门——
    统计信号足够强时，主人刚走又想起事是自然的；弱信号不配破例。
    """
    try:
        return (float(pattern.get("confidence", 0.0)) >= HIGH_CONFIDENCE_MIN
                and int(pattern.get("occurrences", 0)) >= HIGH_CONFIDENCE_MIN_OCCURRENCES)
    except Exception:
        return False


def current_anticipation(context: dict, patterns: list, events_fn=None) -> str | None:
    """此刻是否存在「按惯例该发生却还没发生」的事；有则返回预判素材，无则 None。

    命中条件（全部满足）：
      1. 当前时刻落在某模式的惯常时段（usual_hour ±1 小时）内；
      2. 今天的事件记录里还没有该模式的踪迹——已经发生就不必多嘴。
    多个模式同时命中时取置信度最高者：一次只说一件事，喋喋不休不是管家。

    context 键：now（可注入）；today_events（可选，主控已有今日时间线时
    直接传入，免重复读盘）。events_fn 可注入（测试），缺省走 episodic。
    """
    context = context or {}
    now = _to_dt(context.get("now")) or datetime.now()
    if not patterns:
        return None
    hits = [p for p in patterns
            if abs(now.hour - int(p.get("usual_hour", -99))) <= PATTERN_HOUR_WINDOW]
    if not hits:
        return None

    events = context.get("today_events")
    if events is None:
        try:
            events = (events_fn or _default_events_today_fn)() or []
        except Exception:
            events = []
    today = now.date()
    done_summaries = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = _parse_event_ts(e.get("ts"))
        if ts is None or ts.date() != today:
            continue
        done_summaries.append(str(e.get("summary") or ""))

    for p in sorted(hits, key=lambda p: -float(p.get("confidence", 0.0))):
        tokens = p.get("match_tokens") or [p.get("keyword", "")]
        if any(t and t in s for s in done_summaries for t in tokens):
            continue  # 今天已发生，不重复预判
        return ("主人通常 %d 点左右处理「%s」相关的事（近期已出现 %d 次），"
                "今天还没有相关记录"
                % (int(p["usual_hour"]), p["keyword"], int(p["occurrences"])))
    return None


# == 契约接口 ==

def should_initiate(context: dict) -> bool:
    """判断现在是否适合主动开口：三重闸门（安静时段/速率限制/用户活跃）全部放行才 True。

    决策顺序即优先级：先问「会不会吵到他睡觉」，再问「是不是说得太勤」，
    最后问「他是不是正在跟我说话」。任一闸门拒绝即 False，绝不勉强开口。
    """
    context = context or {}
    now = _to_dt(context.get("now")) or datetime.now()

    # 闸门一：安静时段——深夜与清晨不打扰
    if _in_quiet_hours(now):
        return False

    # 闸门二：速率限制——距上次主动开口不足一小时配额则闭嘴
    last_init = _to_dt(context.get("last_initiative"))
    if last_init is None and _last_initiative_at:
        last_init = datetime.fromtimestamp(_last_initiative_at)
    if last_init is not None:
        min_interval = 3600.0 / max(MAX_INITIATIVES_PER_HOUR, 1)
        if (now - last_init).total_seconds() < min_interval:
            return False

    # 闸门三：用户刚活动过——他正在交互，主动开口就是抢话。
    # 唯一例外：高置信预判（confidence≥0.8 且出现≥5 次）放宽到 2 分钟——
    # 统计信号足够强时，主人刚走又想起事是自然的。
    # 注意：只放宽这一道闸门；安静时段与速率限制对预判类一律不放松——
    # 深夜再确定的事也不吵他睡觉，说得太勤再高的置信度也得排队。
    last_user = _to_dt(context.get("last_user_activity"))
    if last_user is not None:
        idle_needed = (MIN_IDLE_AFTER_USER_HIGH_CONF_SEC
                       if context.get("high_confidence_anticipation")
                       else MIN_IDLE_AFTER_USER_SEC)
        if (now - last_user).total_seconds() < idle_needed:
            return False

    return True


def _fallback_message(profile: str, due_messages: list,
                      anticipation: str = None) -> str | None:
    """无模型时的模板兜底：到期事件 > 当日预判 > 沉默（没话找话是大忌）。"""
    if due_messages:
        return "先生，" + "；".join(str(m).strip("。") for m in due_messages[:3]) + "。"
    if anticipation:
        return "先生，" + anticipation.rstrip("。") + "——需要我搭把手吗？"
    return None


def generate_initiative(context: dict, llm_caller) -> str | None:
    """生成一条自然的主动消息；生成成功即记账（供 should_initiate 速率限制）。

    素材三选一优先级（H2）：触发器到期消息 > 当日预判 > 画像寒暄。
    预判素材可两路进入：context["anticipation"] 直接给，或 context["patterns"]
    由本函数现场调 current_anticipation 判定。
    预判存在时 prompt 明确指示「以管家口吻自然提起惯例并主动提出帮忙」。
    llm_caller 异常或返回空 → 模板兜底；连素材都没有 → None（沉默）。
    """
    global _last_initiative_at
    context = context or {}
    now = _to_dt(context.get("now")) or datetime.now()
    profile = (context.get("profile") or "").strip()
    due_messages = list(context.get("due_messages") or [])

    # 当日预判素材：主控直接给，或由模式列表现场判定；判定异常静默降级
    anticipation = (context.get("anticipation") or "").strip() or None
    if anticipation is None and context.get("patterns"):
        try:
            anticipation = current_anticipation(context, context["patterns"])
        except Exception as e:
            print("[proactive] 预判素材判定异常: %s: %s" % (type(e).__name__, e))
            anticipation = None

    # 连素材都没有：不打扰模型，直接沉默
    if not profile and not due_messages and not anticipation:
        return None

    # 时间 context：GLM 不知道此刻几点，显式锚定（与 brain 的日期锚点同理）
    _WEEK_CN = "一二三四五六日"
    time_hint = "现在是星期%s %02d:%02d。" % (_WEEK_CN[now.weekday()], now.hour, now.minute)
    material = []
    if due_messages:
        material.append("刚到期的事项：" + "；".join(str(m) for m in due_messages[:5]))
    elif anticipation:
        material.append("惯例预判：" + anticipation)
    elif profile:
        material.append("你记得的先生：" + profile)
    prompt = (
        "你是 Nolan，先生的私人 AI 管家，风格正式、简练、得体，称呼用户为「先生」。"
        "现在你要主动开口说一句话（不是回答问题）。\n"
        "要求：一两句话以内；必须基于下面给出的素材，绝不编造事项；"
        "语气自然，像管家恰到好处的提醒，而不是系统通知；不用表情符号。\n")
    if anticipation and not due_messages:
        prompt += ("素材是一条「惯例预判」：请以管家口吻自然地提起这个惯例"
                   "（不要像念统计报表），并主动提出帮忙。\n")
    prompt += time_hint + "\n" + "\n".join(material)

    msg = None
    if llm_caller is not None:
        try:
            raw = llm_caller(prompt)
            if isinstance(raw, str) and raw.strip():
                msg = raw.strip()
        except Exception as e:
            # 失败透明化：模型出错写日志，回退模板，绝不让主动性炸掉主循环
            print("[proactive] llm_caller 异常: %s: %s" % (type(e).__name__, e))

    if msg is None:
        msg = _fallback_message(profile, due_messages, anticipation)

    if msg:
        _last_initiative_at = time.time()  # 记账：开口即占用速率配额
    return msg


# == 模块自测（不碰网络/IO 投递） ==

if __name__ == "__main__":
    from datetime import timedelta

    base = datetime.now().replace(hour=10, minute=0, second=0)  # 上午十点，非安静时段
    # 通过场景：两小时没开口、用户十分钟没活动
    ctx_ok = {"now": base,
              "last_initiative": base - timedelta(hours=2),
              "last_user_activity": base - timedelta(minutes=10)}
    assert should_initiate(ctx_ok) is True
    # 拒绝一：安静时段（23:30）
    assert should_initiate({"now": base.replace(hour=23, minute=30)}) is False
    # 拒绝二：速率限制（十分钟前刚说过）
    assert should_initiate({"now": base,
                            "last_initiative": base - timedelta(minutes=10)}) is False
    # 拒绝三：用户一分钟前刚交互
    assert should_initiate({"now": base,
                            "last_user_activity": base - timedelta(minutes=1)}) is False

    # 生成：mock 模型基于素材说话
    msg = generate_initiative(
        {"now": base, "profile": "偏好：黑咖啡", "due_messages": ["条件触发：该喝水了"]},
        llm_caller=lambda p: "先生，该喝水了。另外，您的黑咖啡我记下了。")
    assert msg and "先生" in msg, msg
    # 生成后记账：模块内部速率限制立即生效
    assert should_initiate({"now": datetime.now()}) is False
    # 模型挂掉 → 模板兜底报事件；无素材 → 沉默
    def _boom(p):
        raise RuntimeError("模型不在线")
    msg2 = generate_initiative({"due_messages": ["条件触发：带伞"]}, _boom)
    assert msg2 and "带伞" in msg2, msg2
    assert generate_initiative({}, llm_caller=lambda p: "x") is None

    # == H2 模式识别自测（全注入，不碰真实 episodic/memory 存储）==
    _last_initiative_at = 0.0
    fake_events = [
        {"ts": (base - timedelta(days=d)).replace(hour=9, minute=5).isoformat(),
         "kind": "task", "summary": "先生写日报", "salience": 0.5}
        for d in range(1, 6)  # 连续 5 天 9 点写日报
    ]
    pats = detect_patterns(now=base, timeline_fn=lambda days: fake_events)
    assert pats and pats[0]["usual_hour"] == 9 and pats[0]["occurrences"] == 5, pats
    assert "日报" in pats[0]["keyword"], pats[0]
    assert high_confidence(pats[0]) is True
    # 此刻 9:30 且今天还没写 → 预判素材；今天已写 → 不再多嘴
    ant = current_anticipation(
        {"now": base.replace(hour=9, minute=30), "today_events": []}, pats)
    assert ant and "日报" in ant, ant
    done = [{"ts": base.replace(hour=9, minute=10).isoformat(),
             "kind": "task", "summary": "先生写了日报"}]
    assert current_anticipation(
        {"now": base.replace(hour=9, minute=30), "today_events": done}, pats) is None
    # 2 次不成模式；预判素材驱动生成；高置信放宽「刚交互」闸门
    assert detect_patterns(now=base, timeline_fn=lambda days: fake_events[:2]) == []
    captured = []
    msg3 = generate_initiative(
        {"now": base.replace(hour=9, minute=30), "patterns": pats, "today_events": []},
        llm_caller=lambda p: (captured.append(p), "先生，到写日报的时间了，需要我整理素材吗？")[1])
    assert msg3 and "日报" in msg3 and "主动提出帮忙" in captured[0]
    _last_initiative_at = 0.0
    assert should_initiate({"now": base,
                            "last_user_activity": base - timedelta(minutes=3),
                            "high_confidence_anticipation": True}) is True
    assert should_initiate({"now": base,
                            "last_user_activity": base - timedelta(minutes=3)}) is False

    print("🎉 proactive 自测全过：三重闸门/生成/记账/兜底/沉默/模式识别/预判/高置信放宽")
