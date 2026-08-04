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
    def generate_initiative(context: dict, llm_caller) -> str | None
        生成一条自然的主动消息；无素材且无模型时返回 None（沉默也是合法决策）。
        context 在 should_initiate 的基础上可携带：
            profile       str        记忆画像（memory_v2.profile_summary 的产物）
            due_messages  list[str]  triggers.check_due 的到期消息
        llm_caller(prompt: str) -> str 由主控注入（如 brain 的 GLM 调用的
        单参数包装）；异常或返回空时回退到模板消息。

调参入口：以下模块常量即为「主动性人格」的旋钮，改数值即改性格。
"""

import time
from datetime import datetime

# == 主动性人格旋钮（模块常量，调这里）==

MAX_INITIATIVES_PER_HOUR = 1      # 速率限制：每小时最多主动开口次数
QUIET_START_HOUR = 23             # 安静时段起点（含）：23:00 后不打扰
QUIET_END_HOUR = 8                # 安静时段终点（不含）：8:00 前不打扰
MIN_IDLE_AFTER_USER_SEC = 300     # 用户刚交互过 5 分钟内不打扰（他正在用，别抢话）

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

    # 闸门三：用户刚活动过——他正在交互，主动开口就是抢话
    last_user = _to_dt(context.get("last_user_activity"))
    if last_user is not None:
        if (now - last_user).total_seconds() < MIN_IDLE_AFTER_USER_SEC:
            return False

    return True


def _fallback_message(profile: str, due_messages: list) -> str | None:
    """无模型时的模板兜底：有到期事件就报事件，否则沉默（没话找话是大忌）。"""
    if due_messages:
        return "先生，" + "；".join(str(m).strip("。") for m in due_messages[:3]) + "。"
    return None


def generate_initiative(context: dict, llm_caller) -> str | None:
    """生成一条自然的主动消息；生成成功即记账（供 should_initiate 速率限制）。

    素材三选一优先级：触发器到期事件 > 记忆画像 > 时间 context。
    llm_caller 异常或返回空 → 模板兜底；连素材都没有 → None（沉默）。
    """
    global _last_initiative_at
    context = context or {}
    now = _to_dt(context.get("now")) or datetime.now()
    profile = (context.get("profile") or "").strip()
    due_messages = list(context.get("due_messages") or [])

    # 连素材都没有：不打扰模型，直接沉默
    if not profile and not due_messages:
        return None

    # 时间 context：GLM 不知道此刻几点，显式锚定（与 brain 的日期锚点同理）
    _WEEK_CN = "一二三四五六日"
    time_hint = "现在是星期%s %02d:%02d。" % (_WEEK_CN[now.weekday()], now.hour, now.minute)
    material = []
    if due_messages:
        material.append("刚到期的事项：" + "；".join(str(m) for m in due_messages[:5]))
    if profile:
        material.append("你记得的先生：" + profile)
    prompt = (
        "你是 Nolan，先生的私人 AI 管家，风格正式、简练、得体，称呼用户为「先生」。"
        "现在你要主动开口说一句话（不是回答问题）。\n"
        "要求：一两句话以内；必须基于下面给出的素材，绝不编造事项；"
        "语气自然，像管家恰到好处的提醒，而不是系统通知；不用表情符号。\n"
        + time_hint + "\n" + "\n".join(material)
    )

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
        msg = _fallback_message(profile, due_messages)

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

    print("🎉 proactive 自测全过：三重闸门/生成/记账/兜底/沉默")
