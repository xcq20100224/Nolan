# -*- coding: utf-8 -*-
"""
bench_reliability.py —— Gap2 可靠性基准：注入式故障测试（纯 mock，无 GUI）

第一性原理：可靠性逻辑（分类、决策、预算）是纯函数，物理原因可以用
「证据字典」注入模拟——不需要真机 GUI 就能验证三件事：
  1. 分类准确率：各类故障现场注入后，classify 是否归约到正确类别
     （含因果层级优先级：焦点丢失 > 应用未响应 > 其余）；
  2. 对策选择正确性：decide 是否给出该类别绑定的策略、退避与如实提示，
     预算耗尽 / 未知类别是否 give_up 退场；
  3. 结构化重试控制流：注入故障序列回放，验证类别预算、全局总预算
     两道闸门确实终止重试（自愈系统绝不死循环）。

运行：python jarvis/bench_reliability.py
退出码：0 = 全部通过；1 = 存在失败项。零第三方依赖，任何机器可跑。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reliability as rel  # noqa: E402

_passed = 0
_failed = 0


def _check(name: str, got, want) -> None:
    """单条断言：打印 PASS/FAIL 并累计计数。"""
    global _passed, _failed
    if got == want:
        _passed += 1
        print("[bench] PASS  %s" % name)
    else:
        _failed += 1
        print("[bench] FAIL  %s -> 期望 %r，实际 %r" % (name, want, got))


# ---------------------------------------------------------------------------
# 一、分类准确率：注入各类故障现场证据 -> 期望类别
# ---------------------------------------------------------------------------

def bench_classify() -> None:
    print("[bench] ==== 一、错误分类（注入证据 -> 类别）====")
    cases = [
        # (用例名, 注入证据, 期望类别)
        ("空证据 -> 未知", {}, rel.UNKNOWN),
        ("非字典证据 -> 未知", None, rel.UNKNOWN),

        # 焦点丢失：因果层级最优先（窗口没对着目标，其余证据不可信）
        ("焦点丢失（点击具名目标）",
         {"action": "left_click", "text": "播放", "hint_in_foreground": False,
          "controls_before": 10, "controls_after": 10,
          "named_found_after": True, "named_xy_after": (100, 100),
          "clicked_xy": (100, 100)},
         rel.FOCUS_LOST),
        ("焦点丢失压过控件树消失（优先级）",
         {"action": "left_click", "text": "确定", "hint_in_foreground": False,
          "controls_before": 5, "controls_after": 0},
         rel.FOCUS_LOST),
        ("焦点丢失压过文本校验（优先级）",
         {"action": "type", "text": "hello", "hint_in_foreground": False},
         rel.FOCUS_LOST),

        # 应用未响应：控件树从有到无
        ("控件树从有到无 -> 应用未响应",
         {"action": "left_click", "text": "确定", "hint_in_foreground": True,
          "controls_before": 8, "controls_after": 0},
         rel.APP_NOT_RESPONDING),
        ("控件树前后都为空不算无响应（一直枚举不到）-> 坐标漂移",
         {"action": "left_click", "text": "确定", "hint_in_foreground": True,
          "controls_before": 0, "controls_after": 0},
         rel.COORD_DRIFT),

        # 文本校验不符：type 动作复核未生效
        ("输入未生效 -> 文本校验不符",
         {"action": "type", "text": "hello", "verify_reason": "输入框里未出现文字",
          "controls_before": 5, "controls_after": 5},
         rel.TEXT_MISMATCH),
        ("应用未响应压过文本校验（优先级）",
         {"action": "type", "text": "hello",
          "controls_before": 5, "controls_after": 0},
         rel.APP_NOT_RESPONDING),

        # 目标未出现：控件树确认查无此物
        ("具名点击目标不在控件树 -> 目标未出现",
         {"action": "left_click", "text": "播放", "hint_in_foreground": True,
          "controls_before": 6, "controls_after": 6, "named_found_after": False},
         rel.TARGET_MISSING),
        ("双击目标不在控件树 -> 目标未出现",
         {"action": "double_click", "text": "歌曲A",
          "controls_before": 4, "controls_after": 4, "named_found_after": False},
         rel.TARGET_MISSING),

        # 坐标漂移：目标在但位置偏移超阈值
        ("控件挪位 300px -> 坐标漂移",
         {"action": "left_click", "text": "确定",
          "controls_before": 6, "controls_after": 6,
          "named_found_after": True, "named_xy_after": (500, 300),
          "clicked_xy": (200, 200)},
         rel.COORD_DRIFT),
        ("无控件证据的具名点击 -> 坐标漂移（UIA 缺席时的最可能原因）",
         {"action": "left_click", "text": "播放", "clicked_xy": (100, 100)},
         rel.COORD_DRIFT),

        # 超时：点对了位置没反应 / 无空间证据的动作
        ("小位移 10px 不算漂移 -> 超时",
         {"action": "left_click", "text": "确定",
          "controls_before": 6, "controls_after": 6,
          "named_found_after": True, "named_xy_after": (205, 210),
          "clicked_xy": (200, 200)},
         rel.TIMEOUT),
        ("纯坐标点击无证据 -> 超时（保守）",
         {"action": "left_click", "clicked_xy": (100, 100)},
         rel.TIMEOUT),
        ("按键未生效 -> 超时",
         {"action": "key", "keys": "enter"},
         rel.TIMEOUT),
        ("滚动未生效 -> 超时",
         {"action": "scroll", "text": "down"},
         rel.TIMEOUT),
    ]
    ok = 0
    for name, ev, want in cases:
        got = rel.classify(ev)
        if got == want:
            ok += 1
        _check("分类：%s" % name, got, want)
    print("[bench] 分类准确率：%d/%d" % (ok, len(cases)))


# ---------------------------------------------------------------------------
# 二、对策选择正确性：类别 -> 策略/退避/提示；预算与退场
# ---------------------------------------------------------------------------

def bench_decide() -> None:
    print("[bench] ==== 二、对策选择（类别 -> 策略 + 退场条件）====")

    strategy_table = [
        (rel.FOCUS_LOST, "refocus"),
        (rel.TARGET_MISSING, "wait_recheck"),
        (rel.APP_NOT_RESPONDING, "wait_recheck"),
        (rel.COORD_DRIFT, "relocate"),
        (rel.TEXT_MISMATCH, "retype"),
        (rel.TIMEOUT, "backoff_retry"),
    ]
    for cat, want_strategy in strategy_table:
        d = rel.decide(cat, rel.RetryLedger(),
                       {"text": "确定", "named_xy_after": (500, 300),
                        "clicked_xy": (200, 200)})
        _check("对策：%s -> %s" % (cat, want_strategy),
               d["action"], want_strategy)
        _check("对策：%s 提示非空" % cat, bool(d["hint"]), True)
        _check("对策：%s 退避非负" % cat, d["backoff"] >= 0, True)

    # 提示内容如实性抽查：每条提示必须说清系统做了什么/要求什么
    d = rel.decide(rel.FOCUS_LOST, rel.RetryLedger(), {})
    _check("焦点丢失提示含「置前」", "置前" in d["hint"], True)
    d = rel.decide(rel.COORD_DRIFT, rel.RetryLedger(), {"text": "播放"})
    _check("坐标漂移提示含目标名与按名定位",
           ("播放" in d["hint"]) and ("按名" in d["hint"]), True)
    d = rel.decide(rel.TEXT_MISMATCH, rel.RetryLedger(), {"text": "hello"})
    _check("文本校验提示含「全选」与输入内容",
           ("全选" in d["hint"]) and ("hello" in d["hint"]), True)
    d = rel.decide(rel.TARGET_MISSING, rel.RetryLedger(), {"text": "确定"})
    _check("目标未出现提示含目标名", "确定" in d["hint"], True)

    # 退场条件：未知类别不占预算直接 give_up
    d = rel.decide(rel.UNKNOWN, rel.RetryLedger(), {})
    _check("未知类别 -> give_up", d["action"], "give_up")
    _check("未知类别 give_up 原因非空", bool(d["reason"]), True)

    # 类别预算耗尽 -> give_up（文本校验预算 1）
    ledger = rel.RetryLedger()
    ledger.note(rel.TEXT_MISMATCH)
    d = rel.decide(rel.TEXT_MISMATCH, ledger, {"text": "x"})
    _check("文本校验预算(1)耗尽 -> give_up", d["action"], "give_up")

    # 全局总预算耗尽 -> give_up（总预算 2 的小账本，各类别预算均 > 2）
    ledger = rel.RetryLedger(total_budget=2)
    ledger.note(rel.TIMEOUT)
    ledger.note(rel.TIMEOUT)
    d = rel.decide(rel.TARGET_MISSING, ledger, {"text": "确定"})
    _check("全局总预算耗尽 -> give_up", d["action"], "give_up")

    # 决策不记账：decide 本身不消耗预算（记账是调用方 note 的职责）
    ledger = rel.RetryLedger()
    rel.decide(rel.TIMEOUT, ledger, {})
    _check("decide 不消耗预算", ledger.can_retry(rel.TIMEOUT), True)


# ---------------------------------------------------------------------------
# 三、重试账本：两道闸门与退避序列
# ---------------------------------------------------------------------------

def bench_ledger() -> None:
    print("[bench] ==== 三、重试账本（类别预算 + 全局总预算 + 退避）====")

    ledger = rel.RetryLedger()
    _check("新账本可重试超时类", ledger.can_retry(rel.TIMEOUT), True)
    _check("新账本摘要为「无」", ledger.summary(), "无")

    # 退避逐次加长（超时类 1.5 -> 3.0 -> 钳在 3.0）
    b0 = ledger.backoff(rel.TIMEOUT)
    ledger.note(rel.TIMEOUT)
    b1 = ledger.backoff(rel.TIMEOUT)
    ledger.note(rel.TIMEOUT)
    b2 = ledger.backoff(rel.TIMEOUT)
    _check("超时退避首档 1.5 秒", b0, 1.5)
    _check("超时退避逐次不减", b1 >= b0 and b2 >= b1, True)
    _check("超时预算(2)耗尽", ledger.can_retry(rel.TIMEOUT), False)
    _check("账本摘要如实计数", ledger.summary(), "超时×2")

    # 类别间预算独立：超时耗尽不影响目标未出现
    _check("类别预算互相独立", ledger.can_retry(rel.TARGET_MISSING), True)

    # 全局总预算：小账本上各类合计超限即锁死
    ledger = rel.RetryLedger(total_budget=3)
    for cat in (rel.FOCUS_LOST, rel.TARGET_MISSING, rel.COORD_DRIFT):
        ledger.note(cat)
    _check("全局总预算(3)用满后全部锁死",
           ledger.can_retry(rel.TIMEOUT) or ledger.can_retry(rel.FOCUS_LOST),
           False)

    # 未知类别不消耗预算
    ledger = rel.RetryLedger(total_budget=1)
    ledger.note(rel.UNKNOWN)
    _check("未知类别记账不消耗总预算", ledger.can_retry(rel.TIMEOUT), True)


# ---------------------------------------------------------------------------
# 四、廉价复核：UIA 阳性确认（注入假控件树，无 GUI）
# ---------------------------------------------------------------------------

def bench_uia_verify() -> None:
    print("[bench] ==== 四、廉价复核（阳性确认，注入假控件树）====")

    def dump_with(*names):
        return lambda: [{"name": n, "rect": (0, 0, 10, 10), "enabled": True}
                        for n in names]

    _check("预期「确定」出现在控件树 -> 确认生效",
           rel.uia_verify({"action": "left_click", "text": "文件"},
                          "弹出「确定」按钮", dump_with("取消", "确定")),
           True)
    _check("预期元素不在控件树 -> 不确定（交截图复核，非判负）",
           rel.uia_verify({"action": "left_click", "text": "文件"},
                          "弹出「确定」按钮", dump_with("取消", "返回")),
           False)
    _check("type 输入内容作为子串回显进控件名 -> 确认生效",
           rel.uia_verify({"action": "type", "text": "hello"},
                          "输入框里出现文字 hello", dump_with("hello world")),
           True)
    _check("type 输入内容未回显 -> 不确定",
           rel.uia_verify({"action": "type", "text": "hello"},
                          "输入框里出现文字 hello", dump_with("文本编辑器")),
           False)
    _check("空预期 -> 不确定",
           rel.uia_verify({"action": "left_click"}, "", dump_with("确定")),
           False)
    _check("无可提取关键词 -> 不确定",
           rel.uia_verify({"action": "scroll"}, "页面向下滚动",
                          dump_with("页面")),
           False)
    _check("枚举回调抛异常 -> 不确定（不成为新故障点）",
           rel.uia_verify({"action": "left_click"}, "弹出「确定」",
                          lambda: (_ for _ in ()).throw(RuntimeError("COM 失败"))),
           False)
    _check("枚举返回 None -> 不确定",
           rel.uia_verify({"action": "left_click"}, "弹出「确定」",
                          lambda: None),
           False)


# ---------------------------------------------------------------------------
# 五、注入式故障场景回放：结构化重试控制流（两道闸门终止性证明）
# ---------------------------------------------------------------------------

def _replay(evidence_seq, total_budget=6):
    """模拟 perform 的处置循环：注入故障证据序列，返回 (类别, 策略) 轨迹。"""
    ledger = rel.RetryLedger(total_budget=total_budget)
    trace = []
    for ev in evidence_seq:
        cat = rel.classify(ev)
        d = rel.decide(cat, ledger, ev)
        if d["action"] == "give_up":
            trace.append((cat, "give_up"))
            break
        ledger.note(cat)
        trace.append((cat, d["action"]))
    return trace


def bench_scenarios() -> None:
    print("[bench] ==== 五、注入式故障场景回放（终止性与对策序列）====")

    # 场景 A：焦点反复被抢（如弹窗横跳）——refocus 两次后类别预算耗尽退场
    ev_focus = {"action": "left_click", "text": "播放",
                "hint_in_foreground": False}
    trace = _replay([ev_focus] * 5)
    _check("场景A 焦点抖动：refocus×2 后 give_up",
           [t[1] for t in trace], ["refocus", "refocus", "give_up"])
    _check("场景A 轨迹类别全是焦点丢失",
           [t[0] for t in trace], [rel.FOCUS_LOST] * 3)

    # 场景 B：控件加载慢——退避序列逐次加长，预算耗尽退场
    ev_missing = {"action": "left_click", "text": "播放",
                  "hint_in_foreground": True,
                  "controls_before": 6, "controls_after": 6,
                  "named_found_after": False}
    ledger = rel.RetryLedger()
    backs = []
    for _ in range(4):
        cat = rel.classify(ev_missing)
        d = rel.decide(cat, ledger, ev_missing)
        if d["action"] == "give_up":
            backs.append("give_up")
            break
        backs.append(d["backoff"])
        ledger.note(cat)
    _check("场景B 目标未出现：退避 1.0 -> 2.0 -> give_up",
           backs, [1.0, 2.0, "give_up"])

    # 场景 C：混合故障每类一次——全局总预算(6)用满后第 7 次锁死
    mixed = [
        {"action": "left_click", "text": "播放", "hint_in_foreground": False},
        {"action": "left_click", "text": "播放", "hint_in_foreground": True,
         "controls_before": 6, "controls_after": 6, "named_found_after": False},
        {"action": "left_click", "text": "确定", "hint_in_foreground": True,
         "controls_before": 6, "controls_after": 6,
         "named_found_after": True, "named_xy_after": (500, 300),
         "clicked_xy": (200, 200)},
        {"action": "type", "text": "hello", "hint_in_foreground": True,
         "controls_before": 5, "controls_after": 5},
        {"action": "key", "keys": "enter", "hint_in_foreground": True},
        {"action": "left_click", "text": "确定", "hint_in_foreground": True,
         "controls_before": 8, "controls_after": 0},
    ]
    trace = _replay(mixed + [{"action": "key", "keys": "enter",
                              "hint_in_foreground": True}])
    _check("场景C 混合故障：6 类各处置一次后总预算锁死",
           [t[1] for t in trace],
           ["refocus", "wait_recheck", "relocate", "retype",
            "backoff_retry", "wait_recheck", "give_up"])
    _check("场景C 前 6 步类别与注入一致",
           [t[0] for t in trace][:6],
           [rel.FOCUS_LOST, rel.TARGET_MISSING, rel.COORD_DRIFT,
            rel.TEXT_MISMATCH, rel.TIMEOUT, rel.APP_NOT_RESPONDING])

    # 场景 D：证据缺失的保守路径——无前台证据、无控件证据的坐标点击
    # 一律按超时退避，绝不误判成需要物理修复的类别
    ev_blind = {"action": "left_click", "clicked_xy": (100, 100)}
    trace = _replay([ev_blind] * 4)
    _check("场景D 无证据点击：一律超时退避后 give_up",
           [t[1] for t in trace],
           ["backoff_retry", "backoff_retry", "give_up"])


def main() -> int:
    print("[bench] Gap2 可靠性基准开始（纯 mock，无 GUI）")
    bench_classify()
    bench_decide()
    bench_ledger()
    bench_uia_verify()
    bench_scenarios()
    print("[bench] ========================================")
    print("[bench] 总计 %d 通过 / %d 失败" % (_passed, _failed))
    if _failed:
        print("[bench] 结果：FAIL")
        return 1
    print("[bench] 结果：ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
