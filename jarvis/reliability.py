# -*- coding: utf-8 -*-
"""
reliability.py —— Nolan 的「失败分类学」与「结构化重试」（Gap2 可靠性深水区）

第一性原理：GUI 自动化失败不是随机噪声，而是有限几种可指认的物理原因——
窗口没抢到焦点、目标控件还没加载完、坐标随窗口/布局漂移、文字没输进去、
界面响应慢、应用短暂无响应。每种物理原因都有对应的物理对策，对策对了
一次就修好，对策错了「再来一遍」一万次也没用（盲重试的本质就是没有分类）。

因此本模块把「复核未生效」这一模糊信号归约到有限类别，每类绑定：
  对策（strategy）——直接消除该物理原因的动作；
  独立重试预算（budget）——同一物理原因修不好 N 次，说明判断有误，停手；
  退避序列（backoff）——给界面/应用留出物理响应时间，逐次加长；
并设全局总预算封顶——自愈系统绝不能把「不死」做成「不死循环」。

设计边界（可靠性模块自身的可靠性）：
  * 零 GUI 依赖：不 import pyautogui/uia/comtypes，证据由调用方收集注入，
    分类与决策是纯函数——可在无真机的环境用 mock 全量回归（bench_reliability）；
  * 保守原则：廉价复核（uia_verify）只做「阳性确认」，绝不做「阴性判决」——
    控件树里看不到不代表屏幕上没有（自绘界面/网页内容 UIA 覆盖不全）；
  * 未知即退场：证据不足以归类时返回 UNKNOWN 且不消耗任何预算，
    调用方退回旧逻辑——增强路径失效时默认路径行为一字不变。

接口契约：
    classify(evidence: dict) -> str                 证据字典 -> 失败类别
    decide(category, ledger, evidence=None) -> dict 类别+账本 -> 对策决策
    uia_verify(action, expect, dump) -> bool        廉价复核（True=确认生效）
    RetryLedger(total_budget=6)                     结构化重试账本

证据字典字段（全部可选，缺什么证据就没什么判断依据）：
    action              动作名（left_click/double_click/type/key/scroll）
    text                动作携带的目标文字（按钮名/输入内容）
    expect              VLM 填写的预期效果
    verify_reason       截图复核给出的未生效理由
    hint_in_foreground  目标窗口是否在前台（True/False；无 hint 或未知则缺省）
    controls_before     动作执行前枚举到的控件数
    controls_after      复核时刻重新枚举到的控件数（未枚举则缺省）
    named_found_after   复核时刻按动作 text 在控件树中是否找到目标（True/False）
    named_xy_after      找到时目标的当前中心坐标（物理像素）
    clicked_xy          实际点击落点（物理像素）
    keys                key 动作的按键内容
"""

import re

# ---------------------------------------------------------------------------
# 失败类别（物理原因）
# ---------------------------------------------------------------------------

FOCUS_LOST = "focus_lost"                # 焦点丢失：目标窗口被抢前台，动作打在了别的窗口上
TARGET_MISSING = "target_missing"        # 目标未出现：控件尚未加载完，控件树里查无此物
COORD_DRIFT = "coord_drift"              # 坐标漂移：控件还在但挪了位置，旧坐标点空
TEXT_MISMATCH = "text_mismatch"          # 文本校验不符：输入的文字没有进入目标区域
TIMEOUT = "timeout"                      # 超时：位置点对了但界面响应慢，预期效果尚未渲染
APP_NOT_RESPONDING = "app_not_responding"  # 应用未响应：控件树从有到无，应用卡死或窗口销毁
UNKNOWN = "unknown"                      # 未知：证据不足以归约到已知物理原因

# 类别中文名（日志与报告用）
CATEGORY_CN = {
    FOCUS_LOST: "焦点丢失",
    TARGET_MISSING: "目标未出现",
    COORD_DRIFT: "坐标漂移",
    TEXT_MISMATCH: "文本校验不符",
    TIMEOUT: "超时",
    APP_NOT_RESPONDING: "应用未响应",
    UNKNOWN: "未知",
}

# ---------------------------------------------------------------------------
# 对策表：类别 -> 物理对策 + 独立重试预算 + 退避序列
#
# 每条对策都必须能说出它消除的物理原因：
#   refocus       焦点丢失    -> 把目标窗口重新置前（动作才有落点）
#   wait_recheck  目标未出现  -> 等待加载后重新枚举控件（等待本身就是解药）
#   wait_recheck  应用未响应  -> 更长退避等待应用恢复（无响应只能等，戳它更糟）
#   relocate      坐标漂移    -> 重新定位：提示下一步按名定位（名称是 invariant，坐标不是）
#   retype        文本校验不符 -> 全选重输（幂等：误报时重输同文结果不变）
#   backoff_retry 超时        -> 退避等待后让 VLM 重看（给界面渲染时间）
# ---------------------------------------------------------------------------

_POLICIES = {
    FOCUS_LOST:         {"strategy": "refocus",       "budget": 2, "backoff": (0.5, 1.0)},
    APP_NOT_RESPONDING: {"strategy": "wait_recheck",  "budget": 2, "backoff": (2.0, 4.0)},
    TARGET_MISSING:     {"strategy": "wait_recheck",  "budget": 2, "backoff": (1.0, 2.0)},
    COORD_DRIFT:        {"strategy": "relocate",      "budget": 2, "backoff": (0.5, 1.0)},
    TEXT_MISMATCH:      {"strategy": "retype",        "budget": 1, "backoff": (0.5,)},
    TIMEOUT:            {"strategy": "backoff_retry", "budget": 2, "backoff": (1.5, 3.0)},
}

# 全局总预算：一次任务内所有类别的对策调用总数上限，防「自愈」变「死循环」
_TOTAL_BUDGET = 6

# 坐标漂移判定阈值（物理像素）：控件当前中心与点击落点距离超过此值才算漂移，
# 小于此值视为「点对了但没反应」——那是超时的物理表现，不是漂移
_DRIFT_THRESHOLD = 30


# ---------------------------------------------------------------------------
# 分类器：证据 -> 唯一类别（纯函数，优先级即「物理原因的因果层级」）
# ---------------------------------------------------------------------------

def classify(evidence: dict) -> str:
    """
    把一次「复核未生效」的现场证据归约到唯一失败类别。

    判定顺序即因果层级——上游原因不除，下游证据全部不可信：
      1. 焦点丢失：窗口都没对着目标，控件树/坐标证据全是别家窗口的，最优先；
      2. 应用未响应：控件树从有到无（卡死/销毁），其余判断失去基础；
      3. 文本校验不符：type 动作复核未生效，物理表现就是字没进去；
      4. 点击类细分：控件树查无目标 -> 目标未出现；
         目标在但位置偏移超阈值 -> 坐标漂移；位置吻合却没反应 -> 超时；
      5. key/scroll 等无空间证据的动作：未生效只能是界面慢 -> 超时；
      6. 完全无证据 -> UNKNOWN（不猜，调用方退回旧逻辑）。
    """
    if not isinstance(evidence, dict) or not evidence:
        return UNKNOWN

    # 1) 焦点丢失：目标窗口不在前台——最上游的物理原因，其余证据不可信
    if evidence.get("hint_in_foreground") is False:
        return FOCUS_LOST

    # 2) 应用未响应：控件树从有到无——应用卡死或窗口被销毁的直接物理证据
    if evidence.get("controls_before", 0) > 0 \
            and evidence.get("controls_after") == 0:
        return APP_NOT_RESPONDING

    action = str(evidence.get("action", "") or "")
    text = str(evidence.get("text", "") or "").strip()

    # 3) 文本校验不符：输入动作复核未生效——字没进目标区域
    #    （焦点丢失已在 1) 排除，剩下最可能是粘贴失败/输入法拦截/焦点在别处）
    if action == "type":
        return TEXT_MISMATCH

    # 4) 点击类：用控件树证据细分
    if action in ("left_click", "double_click"):
        named_found = evidence.get("named_found_after")
        if text:
            if named_found is False:
                # 控件树确认目标不存在：控件没加载完，或目标根本不在此界面
                return TARGET_MISSING
            if named_found is True:
                # 目标仍在：比对「当前位置」与「点击落点」判定是否漂移
                clicked = evidence.get("clicked_xy")
                now = evidence.get("named_xy_after")
                if clicked and now and _distance(clicked, now) > _DRIFT_THRESHOLD:
                    return COORD_DRIFT
                # 点对了位置却没反应：界面响应慢，属超时
                return TIMEOUT
            # 有目标文字但无 UIA 证据（UIA 不可用/枚举失败）：
            # 具名点击失手最常见的原因是 VLM 目测坐标偏差，按坐标漂移处置——
            # 其对策（按名定位提示）恰好同时覆盖「目标其实在但坐标错了」
            return COORD_DRIFT
        # 纯坐标点击且无任何控件证据：保守按超时（等待永远不添乱）
        return TIMEOUT

    # 5) 按键/滚动：无空间证据可分，未生效即界面未及时响应
    if action in ("key", "scroll"):
        return TIMEOUT

    # 6) 证据残缺：有动作但归约不到已知原因时按超时（最保守的可恢复假设），
    #    连动作都没有才算未知
    return TIMEOUT if action else UNKNOWN


def _distance(p1: tuple, p2: tuple) -> float:
    """两物理像素点的欧氏距离；输入异常返回 0（不误判漂移）。"""
    try:
        return ((float(p1[0]) - float(p2[0])) ** 2
                + (float(p1[1]) - float(p2[1])) ** 2) ** 0.5
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 结构化重试账本：每类独立预算 + 全局总预算，防死循环的硬闸门
# ---------------------------------------------------------------------------

class RetryLedger:
    """
    一次任务的结构化重试账本。

    两道闸门：
      1. 类别预算：同一物理原因修 budget 次还修不好，说明分类或对策有误，
         继续重试只是在消耗主人耐心——停手退场；
      2. 全局总预算：所有类别的对策调用总数封顶——防止「每类都没超，
         合起来转了几十圈」的变相死循环。
    账本只计数不动作：note 记账、can_retry 问预算、backoff 取当前退避秒数。
    """

    def __init__(self, total_budget: int = _TOTAL_BUDGET):
        self._total_budget = max(0, int(total_budget))
        self._spent_total = 0
        self._spent = {}  # 类别 -> 已用次数

    def can_retry(self, category: str) -> bool:
        """该类别是否还有重试预算（含全局总预算检查）。"""
        policy = _POLICIES.get(category)
        if policy is None:
            return False  # UNKNOWN 等无策略类别永不占预算
        if self._spent_total >= self._total_budget:
            return False
        return self._spent.get(category, 0) < policy["budget"]

    def note(self, category: str) -> None:
        """为一次对策调用记账。"""
        if category in _POLICIES:
            self._spent[category] = self._spent.get(category, 0) + 1
            self._spent_total += 1

    def backoff(self, category: str) -> float:
        """
        该类别下一次对策的退避秒数：按已用次数索引退避序列，
        超界钳在末档——退避只增不减，给界面越来越长的物理响应时间。
        """
        policy = _POLICIES.get(category)
        if not policy:
            return 0.0
        seq = policy["backoff"]
        idx = min(self._spent.get(category, 0), len(seq) - 1)
        return float(seq[idx])

    def summary(self) -> str:
        """账本摘要（失败报告与日志用），如「焦点丢失×2、超时×1」。"""
        if not self._spent:
            return "无"
        return "、".join(
            "%s×%d" % (CATEGORY_CN.get(k, k), v)
            for k, v in sorted(self._spent.items()))


# ---------------------------------------------------------------------------
# 决策：类别 + 账本 -> 对策指令（纯函数，执行由调用方负责）
# ---------------------------------------------------------------------------

def decide(category: str, ledger: RetryLedger, evidence: dict | None = None) -> dict:
    """
    按类别与账本给出对策决策：
      {"category", "action": 策略名, "backoff": 退避秒数, "hint": 给 VLM 的中文提示,
       "reason": 退场原因（仅 give_up 时非空）}
    无策略（UNKNOWN）或预算耗尽时 action 为 "give_up"——
    自愈的边界：修不好就如实上报，绝不硬撑。
    hint 是写给下一步 VLM 的处置说明（会被 eyes 追加进动作历史），
    内容必须如实描述系统已做的物理处置，不许夸大。
    """
    policy = _POLICIES.get(category)
    if policy is None:
        return {"category": category, "action": "give_up", "backoff": 0.0,
                "hint": "", "reason": "无对应策略（未知类别）"}
    if ledger is None or not ledger.can_retry(category):
        return {"category": category, "action": "give_up", "backoff": 0.0,
                "hint": "", "reason": "重试预算已耗尽"}
    ev = evidence or {}
    return {"category": category,
            "action": policy["strategy"],
            "backoff": ledger.backoff(category),
            "hint": _build_hint(category, ev),
            "reason": ""}


def _build_hint(category: str, ev: dict) -> str:
    """生成写给下一步 VLM 的处置说明（如实陈述系统的物理处置与下一步要求）。"""
    text = str(ev.get("text", "") or "").strip()
    if category == FOCUS_LOST:
        return ("系统检测到目标窗口刚才不在前台（焦点被其他窗口抢走），"
                "已重新置前；请重新观察界面后再继续操作")
    if category == APP_NOT_RESPONDING:
        return ("系统检测到应用的控件树暂时读取不到（应用可能正在加载或短暂无响应），"
                "已退避等待；请重新观察，若反复如此请 fail 并如实报告应用无响应")
    if category == TARGET_MISSING:
        return ("系统在控件树中没有找到「%s」（目标可能尚未加载完成），已等待；"
                "下一步请重新观察，若仍找不到请滚动页面或换其他入口，"
                "不要重复点击同一位置" % text)
    if category == COORD_DRIFT:
        if text:
            return ("上次点击未生效且控件位置已漂移：下一步 left_click 时把 text "
                    "填「%s」，系统会按名称精确定位控件中心，不要凭旧坐标点击" % text)
        return ("上次按坐标点击未生效：下一步请先在控件清单中找到目标控件的名称，"
                "left_click 时把该名称填进 text 按名定位，不要再用裸坐标碰运气")
    if category == TEXT_MISMATCH:
        return ("输入的文字未生效，系统已自动全选并重新输入「%s」；"
                "请观察文本是否出现，若仍无文本请先点击输入框确保焦点在其中" % text)
    if category == TIMEOUT:
        return ("界面可能响应缓慢，预期效果尚未出现，系统已退避等待；"
                "请重新观察当前状态再继续，不要急于重复同一动作")
    return ""


# ---------------------------------------------------------------------------
# 廉价复核：UIA 控件树能答的题，不花截图 + VLM 往返的钱
# ---------------------------------------------------------------------------

def uia_verify(action: dict, expect: str, dump) -> bool:
    """
    廉价复核（只做阳性确认）：
    预期效果中的关键词（引号内文字 / type 动作的输入内容）出现在重新枚举的
    控件名中 -> True（动作确已生效，调用方可跳过截图+VLM 复核）；
    其他任何情况 -> False（不确定，调用方走截图复核——注意这不是「未生效」，
    自绘界面与网页内容 UIA 覆盖不全，阴性结论必须留给截图）。

    dump 是注入的控件枚举回调（() -> list[dict]），便于无真机测试；
    任何异常按 False 处理——廉价复核是省钱手段，绝不成为新的故障点。
    """
    try:
        keywords = _expect_keywords(action, expect)
        if not keywords:
            return False
        controls = dump() or []
        names = "\n".join(
            str(c.get("name", "") or "").lower()
            for c in controls if isinstance(c, dict))
        return any(k in names for k in keywords)
    except Exception:
        return False


def _expect_keywords(action: dict, expect: str) -> list:
    """
    从预期描述中提取可在控件名中查证的阳性关键词（去重保序）：
      1. 引号包裹的文字（「确定」、“播放”、"OK"），长度 2~30；
      2. type 动作的输入内容本身（地址栏/搜索框/单元格常把输入回显进控件名）。
    """
    kws = []
    for m in re.finditer(r"[「\"“]([^「」\"“”]{2,30})[」\"”]", expect or ""):
        kws.append(m.group(1).strip().lower())
    if isinstance(action, dict) and action.get("action") == "type":
        t = str(action.get("text", "") or "").strip().lower()
        if 2 <= len(t) <= 30:
            kws.append(t)
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out
